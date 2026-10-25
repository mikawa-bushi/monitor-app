"""UTF-8 の CSV に壊れたバイトが混じったとき(D3-01, D3-02)。

読めている UTF-8 の CSV の 1 行が読めなくても、ファイル全体(追記監視では以後の
すべての行)を cp932 として読まない。読めない行だけを行番号つきの警告を残して読み
飛ばす。先頭がたまたま UTF-8 として読めた cp932 の CSV は、従来どおり cp932 で読み直す。
"""

from __future__ import annotations

import logging

import pytest
from sqlalchemy import text

from monitor_app import MonitorConfig, TableDef
from monitor_app.db.engine import Database
from monitor_app.db.registry import TableRegistry
from monitor_app.exceptions import CsvEncodingError
from monitor_app.services.importer import CsvImporter, TailState

COLUMNS = {"id": "int", "place": "str"}


@pytest.fixture(autouse=True)
def _propagate(monkeypatch):
    monkeypatch.setattr(logging.getLogger("monitor_app"), "propagate", True)


@pytest.fixture
def env(tmp_path):
    config = MonitorConfig(tables={"m": TableDef(columns=COLUMNS)})
    db = Database(f"sqlite:///{(tmp_path / 'app.db').as_posix()}")
    registry = TableRegistry(config)
    registry.create_all(db)
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()
    yield config, registry, db, csv_dir
    db.dispose()


def _importer(env, **kw) -> CsvImporter:
    config, registry, db, csv_dir = env
    return CsvImporter(config, registry, db, csv_dir, **kw)


def _places(env) -> dict:
    with env[2].connect() as conn:
        return dict(conn.execute(text("SELECT id, place FROM m ORDER BY id")).all())


def _append(path, data: bytes) -> None:
    with path.open("ab") as fh:
        fh.write(data)


# ---------------------------------------------------------------------------
# 追記監視(D3-01)
# ---------------------------------------------------------------------------
def test_tail_keeps_utf8_after_one_undecodable_line(env, caplog):
    importer = _importer(env)
    path = env[3] / "m.csv"
    path.write_bytes("id,place\n1,Café\n".encode())
    state = importer.init_tail("m", path)
    assert (state.encoding, state.evidence) == ("utf-8", 1)  # 既存の行から判別
    _append(path, b"3,\x82\xa0\n")  # 1 行だけ UTF-8 として読めない
    assert importer.tail_append("m", path, state) == 0
    _append(path, "4,Genève\n5,東京\n".encode())
    assert importer.tail_append("m", path, state) == 2
    _append(path, b"6,\xff\n7,ok\n")
    assert importer.tail_append("m", path, state) == 1
    assert state.encoding == "utf-8"
    assert _places(env) == {4: "Genève", 5: "東京", 7: "ok"}
    # 読めない行はそれぞれ行番号つきで警告する(1 回だけではない)
    message = "文字コード UTF-8 として読めないバイトを含むため、この行は取り込みません"
    assert f"m.csv 3行目: {message}" in caplog.messages
    assert f"m.csv 6行目: {message}" in caplog.messages
    assert not any("cp932 で読み込みました" in m for m in caplog.messages)


def test_tail_still_switches_an_ascii_stream_to_cp932(env, caplog):
    """ASCII だけの CSV に cp932 の行が追記されたら cp932 に切り替える(従来どおり)。"""
    importer = _importer(env)
    path = env[3] / "m.csv"
    path.write_bytes(b"id,place\n1,a\n")
    state = importer.init_tail("m", path)
    _append(path, "2,温度\n".encode("cp932"))
    assert importer.tail_append("m", path, state) == 1
    assert state.encoding == "cp932"
    assert _places(env)[2] == "温度"
    assert sum("cp932 で読み込みました" in m for m in caplog.messages) == 1


def test_tail_start_uses_cp932_when_the_body_is_cp932(env):
    """見出しが ASCII でも、データが cp932 なら追記監視は cp932 で読み始める。"""
    importer = _importer(env)
    path = env[3] / "m.csv"
    path.write_bytes(b"id,place\n" + "1,温度\n2,湿度\n".encode("cp932"))
    state = importer.init_tail("m", path)
    assert state.encoding == "cp932" and state.evidence > 0
    _append(path, "3,気圧\n".encode("cp932"))
    assert importer.tail_append("m", path, state) == 1
    assert _places(env) == {3: "気圧"}


def test_tail_with_a_wrong_fixed_encoding_stops_instead_of_dropping_rows(env):
    """固定の文字コードがファイルと違う(読める行が無い)ときは読み飛ばさず、
    エラーにして読み取り位置を進めない(設定を直せば続きから取り込める)。"""
    importer = _importer(env, encoding="utf-8")
    path = env[3] / "m.csv"
    path.write_bytes(b"id,place\n")
    state = importer.init_tail("m", path)
    _append(path, "1,温度\n".encode("cp932"))
    with pytest.raises(CsvEncodingError):
        importer.tail_append("m", path, state)
    assert state.offset == len(b"id,place\n")


def test_tail_from_the_start_skips_a_corrupt_line_in_a_utf8_block(env):
    importer = _importer(env)
    path = env[3] / "m.csv"
    path.write_bytes("id,place\n1,Café\n2,Zürich\n".encode() + b"3,\x82\xa0\n")
    state = TailState()
    assert importer.tail_append("m", path, state) == 2
    assert state.encoding == "utf-8"
    assert _places(env) == {1: "Café", 2: "Zürich"}


# ---------------------------------------------------------------------------
# 全体の取り込み(D3-02)
# ---------------------------------------------------------------------------
def _utf8_with_a_bad_byte(rows: int, bad_row: int) -> bytes:
    lines = ["id,place"] + [f"{i},Café Zürich" for i in range(1, rows + 1)]
    data = ("\n".join(lines) + "\n").encode()
    pos = data.index(f"\n{bad_row},".encode()) + 1
    return data[:pos] + b"\x82\xa0" + data[pos:]


def test_a_bad_byte_after_the_sniffed_head_skips_only_that_row(env):
    importer = _importer(env)
    path = env[3] / "m.csv"
    path.write_bytes(_utf8_with_a_bad_byte(30000, 29000))  # 64 KB より後ろ
    result = importer.import_table("m", path)
    assert (result.inserted, result.skipped, result.encoding) == (
        29999,
        1,
        "utf-8-sig",
    )
    assert "m.csv 29001行目: 文字コード UTF-8 として読めないバイトを含むため" in (
        result.warnings[0]
    )
    assert not any("cp932" in w for w in result.warnings)
    places = _places(env)
    assert places[1] == "Café Zürich" and 29000 not in places


def test_a_bad_byte_inside_the_sniffed_head_also_stays_utf8(env):
    importer = _importer(env)
    path = env[3] / "m.csv"
    path.write_bytes(_utf8_with_a_bad_byte(50, 10))
    result = importer.import_table("m", path)
    assert (result.inserted, result.skipped, result.encoding) == (49, 1, "utf-8-sig")
    assert _places(env)[1] == "Café Zürich"


def test_replace_with_a_bad_byte_keeps_the_good_rows_readable(env):
    importer = _importer(env)
    path = env[3] / "m.csv"
    path.write_bytes(b"id,place\n1,old\n")
    importer.import_table("m", path)
    path.write_bytes(_utf8_with_a_bad_byte(3000, 2000))
    importer.import_table("m", path)
    places = _places(env)
    assert len(places) == 2999 and places[1] == "Café Zürich"


def test_fixed_utf8_skips_the_corrupt_row_instead_of_failing(env):
    importer = _importer(env, encoding="utf-8")
    path = env[3] / "m.csv"
    path.write_bytes(_utf8_with_a_bad_byte(3000, 2000))
    result = importer.import_table("m", path)
    assert (result.inserted, result.skipped) == (2999, 1)


def test_a_cp932_file_that_starts_like_utf8_is_still_read_as_cp932(env):
    """先頭の「ﾃｽ」(バイト列はたまたま UTF-8 の「ý」)と後ろの漢字: 読めた行が
    読めない行より多くないので UTF-8 の壊れたファイルとはみなさない。"""
    importer = _importer(env)
    path = env[3] / "m.csv"
    body = "id,place\n1,ﾃｽ\n" + "".join(f"{i},a{i}\n" for i in range(2, 5000))
    path.write_bytes((body + "5000,漢字\n").encode("cp932"))
    result = importer.import_table("m", path)
    assert (result.encoding, result.inserted, result.skipped) == ("cp932", 5000, 0)
    places = _places(env)
    assert places[1] == "ﾃｽ" and places[5000] == "漢字"


def test_read_rows_skips_the_corrupt_row_too(env):
    importer = _importer(env)
    path = env[3] / "m.csv"
    path.write_bytes(_utf8_with_a_bad_byte(100, 50))
    rows, skipped, warnings = importer.read_rows("m", path)
    assert (len(rows), skipped) == (99, 1)
    assert "m.csv 51行目" in warnings[0]
