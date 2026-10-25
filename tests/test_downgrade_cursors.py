"""2.3.x に戻す前の取り込みカーソルの戻し方(D5-02)。

以前の案内(``_ingest_state`` の行を消す)では、2.3.x が sqlite ソースを最初から
取り込み直し、主キーの重複で毎回失敗したり行が重複したりした。型付きカーソル
``{"v": 値}`` を値そのものへ戻せば、2.3.x は続きから取り込む(実際の 2.3.1 での
確認は D5 の再現スクリプト)。
"""

from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest
from sqlalchemy import text

from monitor_app.db.engine import Database
from monitor_app.db.ingest_state import (
    LEGACY_CURSOR_SQL,
    IngestStateStore,
    decode_cursor,
    unwrap_typed_cursors,
)

#: 2.4 が保存する値 → 2.3.1 が保存していた形(``str(max(...))``)。
CASES = {
    "int": (12, "12"),
    "float": (1792808340.5, "1792808340.5"),
    "text": ("2026-10-02 08:02:00", "2026-10-02 08:02:00"),
    "japanese": ('東京"引用"', '東京"引用"'),
}
UNTOUCHED = {
    "offset": "4096",  # netdiag の読み取り位置(2.4 も文字列のまま)
    "mtime": "1700000000:4096",
    "legacy": "8",
}


def _fill(db: Database) -> None:
    store = IngestStateStore(db)
    store.create()
    for name, (value, _legacy) in CASES.items():
        store.set_value(name, value)
    for name, raw in UNTOUCHED.items():
        store.set(name, raw)


def test_typed_cursors_become_the_v2_3_form(tmp_path):
    path = tmp_path / "database.db"
    db = Database(f"sqlite:///{path.as_posix()}")
    try:
        _fill(db)
        assert unwrap_typed_cursors(db) == len(CASES)
        assert unwrap_typed_cursors(db) == 0  # 2 回目は何もしない
        store = IngestStateStore(db)
        for name, (_value, legacy) in CASES.items():
            assert store.get(name) == legacy
            assert decode_cursor(store.get(name)) == legacy  # 2.4 もそのまま読める
        for name, raw in UNTOUCHED.items():
            assert store.get(name) == raw
    finally:
        db.dispose()


def test_the_documented_sqlite_statement_works_with_plain_sqlite3(tmp_path):
    """sqlite3 コマンドや Python の sqlite3 で、そのまま実行できる(案内の SQL)。"""
    path = tmp_path / "database.db"
    db = Database(f"sqlite:///{path.as_posix()}")
    try:
        _fill(db)
    finally:
        db.dispose()
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(LEGACY_CURSOR_SQL["sqlite"])
        cursors = dict(conn.execute("SELECT source, cursor FROM _ingest_state"))
    assert cursors == {
        **{name: legacy for name, (_v, legacy) in CASES.items()},
        **UNTOUCHED,
    }


def test_url_and_missing_table(tmp_path):
    url = f"sqlite:///{(tmp_path / 'empty.db').as_posix()}"
    assert unwrap_typed_cursors(url) == 0


def test_unsupported_database_is_reported():
    db = Database("sqlite://")
    try:
        db.engine.dialect.name = "oracle"
        with pytest.raises(ValueError, match="oracle"):
            unwrap_typed_cursors(db)
    finally:
        db.engine.dialect.name = "sqlite"
        db.dispose()


def test_server_statements_use_quoted_columns():
    assert "`cursor`" in LEGACY_CURSOR_SQL["mysql"]
    with pytest.raises(KeyError):
        LEGACY_CURSOR_SQL["oracle"]
    assert text(LEGACY_CURSOR_SQL["postgresql"]) is not None
