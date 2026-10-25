"""CSV の文字コードの自動判別(``MONITOR_CSV_ENCODING=auto`` が既定 — DX-04)。

UTF-8(BOM の有無は問わない)で読み、読めなければ ``csv_encoding_fallback``(既定:
cp932)で読み直す。途中で読めなくなったら、そのテーブルに入れた分だけ取り消して
(SAVEPOINT)最初から読み直す。どれでも読めなければファイル名と設定名を示す
:class:`CsvEncodingError`。
"""

from __future__ import annotations

import logging

import pytest
from sqlalchemy import text
from typer.testing import CliRunner

import monitor_app.services.importer as importer_mod
from monitor_app import MonitorConfig, TableDef
from monitor_app.cli import app as cli_app
from monitor_app.cli._common import explain_error
from monitor_app.db.engine import Database
from monitor_app.db.registry import TableRegistry
from monitor_app.exceptions import CsvEncodingError
from monitor_app.services.importer import CsvImporter
from monitor_app.settings.runtime import AppSettings

COLUMNS = {"id": "int", "name": "str"}


@pytest.fixture(autouse=True)
def _propagate(monkeypatch):
    monkeypatch.setattr(logging.getLogger("monitor_app"), "propagate", True)


@pytest.fixture
def env(tmp_path):
    config = MonitorConfig(tables={"t": TableDef(columns=COLUMNS)})
    db = Database(f"sqlite:///{(tmp_path / 'app.db').as_posix()}")
    registry = TableRegistry(config)
    registry.create_all(db)
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()
    yield config, registry, db, csv_dir
    db.dispose()


def _names(db):
    with db.connect() as conn:
        return dict(conn.execute(text("SELECT id, name FROM t ORDER BY id")).all())


def test_default_setting_is_auto():
    assert AppSettings(_env_file=None).csv_encoding == "auto"
    assert AppSettings(_env_file=None).csv_encoding_fallback == "cp932"


@pytest.mark.parametrize(
    "encoding,expected",
    [("utf-8", "utf-8-sig"), ("utf-8-sig", "utf-8-sig"), ("cp932", "cp932")],
)
def test_auto_reads_utf8_bom_and_cp932(env, encoding, expected):
    config, registry, db, csv_dir = env
    path = csv_dir / "t.csv"
    path.write_bytes("id,name\n1,第1ライン①ﾊﾝｶｸ\n".encode(encoding))
    result = CsvImporter(config, registry, db, csv_dir).import_table("t", path)
    assert result.inserted == 1 and result.encoding == expected
    assert _names(db) == {1: "第1ライン①ﾊﾝｶｸ"}


def test_fallback_is_logged_once_per_file_and_kept_in_the_result(env, caplog):
    config, registry, db, csv_dir = env
    path = csv_dir / "t.csv"
    path.write_bytes("id,name\n1,ライン\n".encode("cp932"))
    importer = CsvImporter(config, registry, db, csv_dir)
    message = (
        "t.csv: UTF-8 として読めないため cp932 で読み込みました(文字化けする場合は .env の"
        " MONITOR_CSV_ENCODING で CSV の文字コードを指定してください。"
        "例: MONITOR_CSV_ENCODING=cp932)"
    )
    for _ in range(2):
        result = importer.import_table("t", path)
        assert result.warnings == [message]
    assert caplog.messages.count(message) == 1


def test_a_late_decode_error_rolls_back_only_that_table_and_rereads(env, monkeypatch):
    """先頭 64 KB は UTF-8 として読める cp932 の CSV(半角カナ「ﾃｽ」のバイト列は
    たまたま UTF-8 の「ý」)。途中の漢字で読めなくなったら、入れた分を取り消して
    cp932 で最初から読み直す(追記でも、取り消さないと「ý」の行が残る)。"""
    config, registry, db, csv_dir = env
    monkeypatch.setattr(importer_mod, "INSERT_CHUNK", 100)
    with db.connect() as conn:
        conn.execute(text("INSERT INTO t (id, name) VALUES (2, 'existing')"))
    body = "id,name\n1,ﾃｽ\n" + "".join(f"{i},ascii-{i:06d}\n" for i in range(2, 4000))
    body += "9999,漢字\n"
    data = body.encode("cp932")
    assert data[:70000].decode("utf-8")  # 判別に読む先頭部分は UTF-8 として読める
    path = csv_dir / "t.csv"
    path.write_bytes(data)
    result = CsvImporter(config, registry, db, csv_dir).import_table(
        "t", path, keep=True
    )
    assert (result.encoding, result.inserted, result.duplicates) == ("cp932", 3999, 1)
    names = _names(db)
    assert names[1] == "ﾃｽ" and names[2] == "existing" and names[9999] == "漢字"
    assert len(names) == 4000


def test_explicit_encoding_failure_names_the_file_and_the_setting(env):
    config, registry, db, csv_dir = env
    path = csv_dir / "t.csv"
    path.write_bytes("id,name\n1,ライン\n".encode("cp932"))
    importer = CsvImporter(config, registry, db, csv_dir, encoding="utf-8")
    with pytest.raises(CsvEncodingError) as info:
        importer.import_table("t", path)
    assert info.value.message.startswith("t.csv を文字コード utf-8 として読めません(")
    assert "MONITOR_CSV_ENCODING=cp932" in info.value.hint
    assert explain_error(info.value) == (info.value.message, info.value.hint)


def test_total_failure_in_auto_lists_the_tried_encodings(env):
    config, registry, db, csv_dir = env
    path = csv_dir / "t.csv"
    path.write_bytes(b"id,name\n1,\x82\x7f\n")  # UTF-8 でも cp932 でもない
    with pytest.raises(CsvEncodingError) as info:
        CsvImporter(config, registry, db, csv_dir).import_all()
    assert info.value.message.startswith(
        "t.csv を文字コード utf-8-sig, cp932 のどれでも読めません"
    )
    assert "MONITOR_CSV_ENCODING" in info.value.hint
    assert _names(db) == {}


def test_unknown_encoding_names_are_reported(env):
    config, registry, db, csv_dir = env
    (csv_dir / "t.csv").write_text("id,name\n1,a\n")
    with pytest.raises(CsvEncodingError, match="cp9322"):
        CsvImporter(config, registry, db, csv_dir, encoding="cp9322").import_all()
    with pytest.raises(CsvEncodingError, match="nope"):
        CsvImporter(
            config, registry, db, csv_dir, encoding_fallback="cp932, nope"
        ).import_all()


def test_fallback_chain_is_configurable(env):
    config, registry, db, csv_dir = env
    path = csv_dir / "t.csv"
    path.write_bytes("id,name\n1,ライン\n".encode("euc-jp"))
    importer = CsvImporter(
        config, registry, db, csv_dir, encoding_fallback=" euc-jp , cp932 "
    )
    assert importer.encoding_candidates() == ["utf-8-sig", "euc-jp", "cp932"]
    assert importer.import_table("t", path).encoding == "euc-jp"
    assert _names(db) == {1: "ライン"}
    as_list = CsvImporter(config, registry, db, csv_dir, encoding_fallback=["euc-jp"])
    assert as_list.encoding_candidates() == ["utf-8-sig", "euc-jp"]


def test_western_cp1252_files_are_read_as_cp932_with_a_visible_warning(env, caplog):
    """cp932 を予備にすると cp1252(欧文 Windows)の「°」などは読めてしまい文字化け
    する(従来は読めずに失敗していた)。警告に使った文字コードと設定名を出すので、
    MONITOR_CSV_ENCODING=cp1252 を指定すれば正しく読める。"""
    config, registry, db, csv_dir = env
    path = csv_dir / "t.csv"
    path.write_bytes("id,name\n1,25°C\n".encode("cp1252"))
    CsvImporter(config, registry, db, csv_dir).import_table("t", path)
    assert _names(db) == {1: "25ｰC"}
    assert any("cp932 で読み込みました" in m for m in caplog.messages)
    CsvImporter(config, registry, db, csv_dir, encoding="cp1252").import_table(
        "t", path
    )
    assert _names(db) == {1: "25°C"}


def test_cli_imports_excel_cp932_csvs_with_default_settings(tmp_path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(cli_app, ["startproject", "proj"]).exit_code == 0
    proj = tmp_path / "proj"
    monkeypatch.chdir(proj)
    (proj / "csv" / "users.csv").write_bytes(
        "id,name,email\n1,山田 太郎,yamada@example.com\n".encode("cp932")
    )
    result = runner.invoke(cli_app, ["import-csv"])
    assert result.exit_code == 0, result.output
    assert "cp932 で読み込みました" in result.output


def test_cli_reports_an_unreadable_csv_briefly(tmp_path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(cli_app, ["startproject", "proj"]).exit_code == 0
    proj = tmp_path / "proj"
    monkeypatch.chdir(proj)
    (proj / "csv" / "users.csv").write_bytes(b"id,name,email\n1,\x82\x7f,x\n")
    result = runner.invoke(cli_app, ["import-csv"])
    assert result.exit_code == 1
    assert (
        "users.csv を文字コード utf-8-sig, cp932 のどれでも読めません" in result.output
    )
    assert "ヒント: CSV の文字コードを .env の MONITOR_CSV_ENCODING" in result.output
    assert "Traceback" not in result.output
