"""CSV の文字コードの決まりを 1 か所にまとめたモジュール(services/csv_encoding — D7-05)。

取り込み・追記監視・点検・推論が同じ関数を使う。従来の名前(importer の
``_split_encodings`` / ``_codec_name`` / ``_TAILABLE_CODECS``)も残す。
"""

from __future__ import annotations

import pytest

import monitor_app.services.importer as importer_mod
from monitor_app import MonitorConfig, TableDef
from monitor_app.checks import run_checks
from monitor_app.db.engine import Database
from monitor_app.db.registry import TableRegistry
from monitor_app.exceptions import CsvEncodingError
from monitor_app.services import csv_encoding
from monitor_app.services.importer import CsvImporter
from monitor_app.settings.infer import _encoding_label
from monitor_app.settings.runtime import AppSettings


def test_candidates_follow_the_auto_rule_and_dedupe_aliases():
    assert csv_encoding.candidates("auto", "cp932") == ["utf-8-sig", "cp932"]
    assert csv_encoding.candidates("AUTO", "utf_8_sig, euc-jp,cp932") == [
        "utf-8-sig",
        "euc-jp",
        "cp932",
    ]
    assert csv_encoding.candidates("cp932", "euc-jp") == ["cp932"]
    with pytest.raises(CsvEncodingError, match="nope"):
        csv_encoding.candidates("auto", "nope")
    # 点検用(strict=False)は使えない名前を黙って除く
    assert csv_encoding.candidates("auto", "nope, cp932", strict=False) == [
        "utf-8-sig",
        "cp932",
    ]
    assert csv_encoding.candidates("nope", None, strict=False) == []


@pytest.mark.parametrize(
    ("name", "utf8", "label"),
    [
        ("utf-8", True, "UTF-8"),
        ("utf8", True, "UTF-8"),
        ("UTF_8_SIG", True, "UTF-8"),
        ("cp932", False, "cp932"),
        ("nope", False, "nope"),
    ],
)
def test_utf8_test_and_label_accept_aliases(name, utf8, label):
    assert csv_encoding.is_utf8(name) is utf8
    assert csv_encoding.label(name) == label
    # 推論(serve の表示・config.py のコメント)も同じ表示
    assert _encoding_label(name) == label


def test_importer_keeps_the_old_names():
    assert importer_mod._split_encodings(" a, ,b ") == ["a", "b"]
    assert importer_mod._codec_name("utf8") == "utf-8"
    assert importer_mod._TAILABLE_CODECS is csv_encoding.TAILABLE_CODECS
    assert csv_encoding.is_tailable(["utf-8-sig", "cp932"])
    assert not csv_encoding.is_tailable(["utf-16"])
    assert not csv_encoding.is_tailable(["nope"])


def test_line_evidence_counts_readable_and_unreadable_non_ascii_lines():
    data = "id,place\n1,Café\n2,Zürich\n".encode() + b"3,\x82\xa0\n"
    assert csv_encoding.line_evidence(data, "utf-8") == 1  # 2 行読めて 1 行読めない
    assert csv_encoding.line_evidence(b"id\n1\n", "utf-8") == 0
    assert csv_encoding.line_evidence("1,ライン\n".encode("cp932"), "utf-8") == -1


def test_importer_and_check_use_the_same_candidates(tmp_path):
    config = MonitorConfig(tables={"t": TableDef(columns={"id": "int"})})
    db = Database(f"sqlite:///{tmp_path / 'x.db'}")
    try:
        importer = CsvImporter(
            config,
            TableRegistry(config),
            db,
            tmp_path,
            "auto",
            encoding_fallback="utf_8_sig, cp932",
        )
        assert importer.encoding_candidates() == ["utf-8-sig", "cp932"]
    finally:
        db.dispose()
    # 予備の文字コード名の誤りは点検のエラー 1 件(CSV ごとには報告しない)
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()
    (csv_dir / "t.csv").write_text("id\n1\n")
    settings = AppSettings(
        _env_file=None,
        csv_dir=str(csv_dir),
        csv_encoding_fallback="cp9322",
        database_url=f"sqlite:///{tmp_path / 'y.db'}",
    )
    codes = [i.code for i in run_checks(config, settings, data=True)]
    assert codes.count("csv-encoding-name") == 1
    assert "csv-unreadable" not in codes
