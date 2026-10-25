"""ソースコネクタの取り込みカーソル(ARCH-03)。

監査で再現した不具合:
- (A) ウォーターマーク列が全行 NULL → 毎回同じ行を取り込み直して主キー重複が続く
- (B) 主キーを写さないと、同じ行が毎回重複して増える
- (C) 行の確定とカーソルの保存の間で止まると、以後ずっと主キー重複で失敗する
- (D) カーソルを文字列で保存するため、型の無い列では 2 回目以降に取り込めない
新しいカーソルは挿入の前に決め、行とカーソルを 1 トランザクションで書く。
カーソルは型付き(JSON)で保存し、v2.3.1 までの文字列のカーソルも従来どおり読む。
"""

from __future__ import annotations

import json
import logging
import sqlite3

import pytest
from sqlalchemy import text

from monitor_app.db.engine import Database
from monitor_app.db.ingest_state import IngestStateStore, decode_cursor, encode_cursor
from monitor_app.db.registry import TableRegistry
from monitor_app.services.connectors import (
    JsonlSourceConnector,
    JsonSourceConnector,
    SqliteSourceConnector,
    build_connectors,
)
from monitor_app.settings.declarative import MonitorConfig, SourceDef, TableDef

#: テストで作った DB(終わったらコネクションを閉じる)。
_DATABASES: list = []


@pytest.fixture(autouse=True)
def _propagate(monkeypatch):
    monkeypatch.setattr(logging.getLogger("monitor_app"), "propagate", True)
    yield
    while _DATABASES:
        _DATABASES.pop().dispose()


def _env(tmp_path, sdef: SourceDef, columns=None):
    config = MonitorConfig(
        tables={
            "events": TableDef(
                columns=columns or {"id": "int", "ts": "int", "v": "float"}
            )
        },
        sources={"src": sdef},
    )
    db = Database(f"sqlite:///{(tmp_path / 'app.db').as_posix()}")
    _DATABASES.append(db)
    registry = TableRegistry(config)
    registry.create_all(db)
    state = IngestStateStore(db)
    state.create()
    return config, registry, db, state


def _source_db(tmp_path, ddl: str, rows, table="s"):
    path = tmp_path / "src.db"
    conn = sqlite3.connect(path)
    conn.execute(ddl)
    conn.executemany(
        f"INSERT INTO {table} VALUES ({', '.join('?' * len(rows[0]))})", rows
    )
    conn.commit()
    conn.close()
    return path


def _add_source_rows(path, rows, table="s"):
    conn = sqlite3.connect(path)
    conn.executemany(
        f"INSERT INTO {table} VALUES ({', '.join('?' * len(rows[0]))})", rows
    )
    conn.commit()
    conn.close()


def _ids(db):
    with db.connect() as conn:
        return conn.execute(text("SELECT id FROM events ORDER BY id")).scalars().all()


def _sqlite_source(path, **kwargs) -> SourceDef:
    return SourceDef(
        kind="sqlite",
        path=str(path),
        table="events",
        source_table="s",
        watermark_column="ts",
        **kwargs,
    )


# ---------------------------------------------------------------------------
# sqlite + watermark
# ---------------------------------------------------------------------------
def test_all_null_watermarks_do_not_loop_or_duplicate(tmp_path, caplog):
    src = _source_db(
        tmp_path, "CREATE TABLE s (id INTEGER, ts INTEGER, v REAL)", [(1, None, 1.0)]
    )
    _add_source_rows(src, [(2, None, 2.0)])
    sdef = _sqlite_source(src)
    config, registry, db, state = _env(tmp_path, sdef)
    conn = SqliteSourceConnector("src", sdef, config, registry, db, state)
    for _ in range(3):
        assert conn.poll_once() == 0  # (A) 例外にも主キー重複にもならない
    assert _ids(db) == [] and state.get("src") is None


def test_rows_without_a_watermark_are_skipped_and_the_rest_imported(tmp_path, caplog):
    """(B) 主キーを写さない設定でも、毎回の重複が起きない。"""
    src = _source_db(
        tmp_path,
        "CREATE TABLE s (sid INTEGER, ts INTEGER, v REAL)",
        [(1, 10, 1.0), (2, None, 2.0), (3, 11, 3.0)],
    )
    sdef = _sqlite_source(src, mapping={"ts": "ts", "v": "v"})
    config, registry, db, state = _env(tmp_path, sdef)
    conn = SqliteSourceConnector("src", sdef, config, registry, db, state)
    assert conn.poll_once() == 2
    assert conn.poll_once() == 0
    assert conn.poll_once() == 0
    assert len(_ids(db)) == 2
    assert state.get_value("src") == 11


def test_custom_query_rows_with_null_watermarks_are_skipped_with_a_warning(
    tmp_path, caplog
):
    src = _source_db(
        tmp_path,
        "CREATE TABLE s (id INTEGER, ts INTEGER, v REAL)",
        [(1, 10, 1.0), (2, None, 2.0)],
    )
    sdef = SourceDef(
        kind="sqlite",
        path=str(src),
        table="events",
        query="SELECT id, ts, v FROM s WHERE :wm IS NULL OR ts > :wm OR ts IS NULL",
        watermark_column="ts",
    )
    config, registry, db, state = _env(tmp_path, sdef)
    conn = SqliteSourceConnector("src", sdef, config, registry, db, state)
    assert conn.poll_once() == 1
    assert _ids(db) == [1]
    assert "source 'src': watermark 列 'ts' が NULL の 1 行は取り込みません" in (
        caplog.messages
    )


def test_custom_query_without_the_watermark_column_is_reported(tmp_path, caplog):
    src = _source_db(
        tmp_path, "CREATE TABLE s (id INTEGER, ts INTEGER, v REAL)", [(1, 10, 1.0)]
    )
    sdef = SourceDef(
        kind="sqlite",
        path=str(src),
        table="events",
        query="SELECT id, v FROM s",
        watermark_column="ts",
    )
    config, registry, db, state = _env(tmp_path, sdef)
    conn = SqliteSourceConnector("src", sdef, config, registry, db, state)
    assert conn.poll_once() == 0
    assert "source 'src': query の結果に watermark 列 'ts' がありません" in (
        caplog.messages
    )


def test_rows_and_cursor_are_committed_together(tmp_path, monkeypatch):
    """(C) 行を入れた後・カーソルを保存する前に止まっても、何も確定しない。"""
    src = _source_db(
        tmp_path,
        "CREATE TABLE s (id INTEGER, ts INTEGER, v REAL)",
        [(1, 10, 1.0), (2, 11, 2.0)],
    )
    sdef = _sqlite_source(src)
    config, registry, db, state = _env(tmp_path, sdef)
    conn = SqliteSourceConnector("src", sdef, config, registry, db, state)

    def crash(*args, **kwargs):
        raise RuntimeError("電源断")

    monkeypatch.setattr(IngestStateStore, "set_value", crash)
    with pytest.raises(RuntimeError):
        conn.poll_once()
    assert _ids(db) == [] and state.get("src") is None
    monkeypatch.undo()
    assert conn.poll_once() == 2
    _add_source_rows(src, [(3, 12, 3.0)])
    assert conn.poll_once() == 1  # 新しく増えた行も取り込める
    assert _ids(db) == [1, 2, 3]


def test_untyped_source_columns_keep_working_after_the_first_poll(tmp_path):
    """(D) 型の無い列(``CREATE TABLE s (id, ts, v)``)でも 2 回目以降に取り込める。"""
    src = _source_db(
        tmp_path, "CREATE TABLE s (id, ts, v)", [(1, 10, 1.0), (2, 11, 2.0)]
    )
    sdef = _sqlite_source(src)
    config, registry, db, state = _env(tmp_path, sdef)
    conn = SqliteSourceConnector("src", sdef, config, registry, db, state)
    assert conn.poll_once() == 2
    assert state.get("src") == '{"v":11}'  # 型付き(JSON)で保存する
    _add_source_rows(src, [(3, 12, 3.0)])
    assert conn.poll_once() == 1
    assert conn.poll_once() == 0
    assert _ids(db) == [1, 2, 3]


@pytest.mark.parametrize(
    "ddl,rows,new_row,cursor",
    [
        (
            "CREATE TABLE s (id INTEGER, ts TEXT, v REAL)",
            [(1, "2024-01-01 10:00", 1.0), (2, "2024-01-02 09:00", 2.0)],
            (3, "2024-01-03 00:00", 3.0),
            "2024-01-02 09:00",
        ),
        (
            "CREATE TABLE s (id INTEGER, ts REAL, v REAL)",
            [(1, 1.5, 1.0), (2, 2.25, 2.0)],
            (3, 3.0, 3.0),
            2.25,
        ),
    ],
)
def test_cursor_keeps_the_native_type(tmp_path, ddl, rows, new_row, cursor):
    src = _source_db(tmp_path, ddl, rows)
    sdef = _sqlite_source(src)
    columns = {"id": "int", "ts": "str", "v": "float"}
    config, registry, db, state = _env(tmp_path, sdef, columns)
    conn = SqliteSourceConnector("src", sdef, config, registry, db, state)
    assert conn.poll_once() == 2
    assert state.get_value("src") == cursor
    assert type(state.get_value("src")) is type(cursor)
    _add_source_rows(src, [new_row])
    assert conn.poll_once() == 1


def test_legacy_plain_string_cursors_are_still_honoured(tmp_path):
    """v2.3.1 が保存した文字列のカーソルは文字列のまま比べる(従来どおり)。"""
    src = _source_db(
        tmp_path,
        "CREATE TABLE s (id INTEGER, ts TEXT, v REAL)",
        [(1, "2024-01-01", 1.0), (2, "2024-01-02", 2.0), (3, "2024-01-03", 3.0)],
    )
    sdef = _sqlite_source(src)
    config, registry, db, state = _env(
        tmp_path, sdef, {"id": "int", "ts": "str", "v": "float"}
    )
    state.set("src", "2024-01-02")  # v2.3.1 の str(max_wm)
    conn = SqliteSourceConnector("src", sdef, config, registry, db, state)
    assert conn.poll_once() == 1
    assert _ids(db) == [3]
    assert state.get("src") == '{"v":"2024-01-03"}'


def test_custom_queries_receive_the_typed_cursor(tmp_path):
    src = _source_db(
        tmp_path,
        "CREATE TABLE s (id, ts, v)",
        [(1, 10, 1.0), (2, 11, 2.0)],
    )
    sdef = SourceDef(
        kind="sqlite",
        path=str(src),
        table="events",
        query="SELECT id, ts, v FROM s WHERE :wm IS NULL OR ts > :wm ORDER BY ts",
        watermark_column="ts",
    )
    config, registry, db, state = _env(tmp_path, sdef)
    conn = SqliteSourceConnector("src", sdef, config, registry, db, state)
    assert conn.poll_once() == 2
    _add_source_rows(src, [(3, 12, 3.0)])
    assert conn.poll_once() == 1


def test_cursor_encoding_round_trip_and_legacy_values():
    for value in (5, 1.5, "2024-01-01", "日本", b"\x00\xff"):
        assert decode_cursor(encode_cursor(value)) == value
    assert encode_cursor(11) == '{"v":11}'
    for legacy in (
        None,
        "11",
        "2024-01-01 10:00",
        "1700000000:4096",
        '{"other":1}',
        "{",
    ):
        assert decode_cursor(legacy) == legacy


# ---------------------------------------------------------------------------
# JSONL / JSON
# ---------------------------------------------------------------------------
def test_jsonl_rows_and_offset_are_committed_together(tmp_path, monkeypatch):
    path = tmp_path / "events.jsonl"
    path.write_text(
        json.dumps({"id": 1, "ts": 1, "v": 1.0})
        + "\n"
        + json.dumps({"id": 2, "ts": 2, "v": 2.0})
        + "\n",
        encoding="utf-8",
    )
    sdef = SourceDef(kind="jsonl", path=str(path), table="events")
    config, registry, db, state = _env(tmp_path, sdef)
    conn = JsonlSourceConnector("src", sdef, config, registry, db, state)
    real = IngestStateStore._write

    def crash(self, conn_, source, cursor):
        raise RuntimeError("電源断")

    monkeypatch.setattr(IngestStateStore, "_write", crash)
    with pytest.raises(RuntimeError):
        conn.poll_once()
    assert _ids(db) == []
    monkeypatch.setattr(IngestStateStore, "_write", real)
    assert conn.poll_once() == 2
    assert conn.poll_once() == 0
    assert state.get("src") == str(path.stat().st_size)  # 従来どおりバイト位置の文字列


def test_json_records_that_are_not_objects_are_skipped(tmp_path, caplog):
    path = tmp_path / "data.json"
    path.write_text(
        json.dumps([{"id": 1, "ts": 1, "v": 1.0}, 5, "x"]), encoding="utf-8"
    )
    sdef = SourceDef(kind="json", path=str(path), table="events", mode="replace")
    config, registry, db, state = _env(tmp_path, sdef)
    conn = JsonSourceConnector("src", sdef, config, registry, db, state)
    assert conn.poll_once() == 1
    assert "source 'src': object でないレコード 2 件をスキップ" in caplog.messages


def test_connectors_notify_after_writing_rows(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text(json.dumps({"id": 1, "ts": 1, "v": 1.0}) + "\n", encoding="utf-8")
    sdef = SourceDef(kind="jsonl", path=str(path), table="events")
    config, registry, db, state = _env(tmp_path, sdef)
    calls = []
    connectors = build_connectors(
        config, registry, db, state, on_change=lambda: calls.append(1)
    )
    assert connectors["src"].poll_once() == 1
    assert connectors["src"].poll_once() == 0  # 変化なし → 呼ばない
    assert calls == [1]
