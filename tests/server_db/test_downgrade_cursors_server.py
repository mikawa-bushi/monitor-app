"""2.3.x に戻す前のカーソルの戻し方を PostgreSQL / MySQL で確かめる(D5-02)。

実行方法は conftest.py を参照(``MONITOR_TEST_PG_URL`` / ``MONITOR_TEST_MYSQL_URL``)。
"""

from __future__ import annotations

import pytest

from monitor_app.db.engine import Database
from monitor_app.db.ingest_state import IngestStateStore, unwrap_typed_cursors

pytestmark = pytest.mark.server_db


def test_typed_cursors_are_unwrapped(server_url):
    db = Database(server_url)
    try:
        store = IngestStateStore(db)
        store.create()
        store.set_value("int", 12)
        store.set_value("float", 1792808340.5)
        store.set_value("text", "2026-10-02 08:02:00")
        store.set_value("japanese", '東京"引用"')
        store.set("offset", "4096")
        assert unwrap_typed_cursors(db) == 4
        assert {
            name: store.get(name)
            for name in ("int", "float", "text", "japanese", "offset")
        } == {
            "int": "12",
            "float": "1792808340.5",
            "text": "2026-10-02 08:02:00",
            "japanese": '東京"引用"',
            "offset": "4096",
        }
    finally:
        db.dispose()
