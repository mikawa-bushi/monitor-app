"""文字列の列の保持期間(区切りの混在・空欄 — D3-10)を PostgreSQL / MySQL で確かめる。

実行方法は conftest.py を参照(``MONITOR_TEST_PG_URL`` / ``MONITOR_TEST_MYSQL_URL``)。
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import insert, select

from monitor_app import MonitorConfig, Retention, TableDef
from monitor_app.db.engine import Database
from monitor_app.db.registry import TableRegistry
from monitor_app.services.retention import RetentionTask

pytestmark = pytest.mark.server_db


def test_mixed_text_timestamps(server_url):
    config = MonitorConfig(
        tables={
            "t": TableDef(
                columns={"id": "int", "ts": "str"},
                retention=Retention(keep_days=7, column="ts"),
            )
        }
    )
    db = Database(server_url)
    registry = TableRegistry(config)
    registry.create_all(db)
    table = registry.get("t")
    try:
        with db.connect() as conn:
            conn.execute(
                insert(table),
                [
                    {"id": 1, "ts": ""},
                    {"id": 2, "ts": "2026-10-01T00:00:00"},
                    {"id": 3, "ts": "2026-10-14 18:00:00"},
                    {"id": 4, "ts": "2026-10-14t06:00:00"},  # 小文字の t は残してよい
                    {"id": 5, "ts": "2026-10-13"},
                    {"id": 6, "ts": None},
                ],
            )
        now = datetime(2026, 10, 21, 12, 0, 0)
        RetentionTask(config, registry, db, 60, clock=lambda: now).purge_once()
        with db.readonly() as conn:
            ids = conn.execute(select(table.c.id).order_by(table.c.id)).scalars().all()
    finally:
        db.dispose()
    assert 2 not in ids and 5 not in ids
    assert {1, 3, 6} <= set(ids)
