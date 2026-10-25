"""datetime 列の実サーバーでの扱い(MySQL の小数秒 — D3-05)。

実行方法は conftest.py を参照(``MONITOR_TEST_PG_URL`` / ``MONITOR_TEST_MYSQL_URL``)。
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from monitor_app import MonitorConfig, TableDef, ViewDef
from monitor_app.db.bulk import insert_rows
from monitor_app.db.engine import Database
from monitor_app.db.registry import TableRegistry
from monitor_app.db.schema import DATETIME_PRECISION, inspect_schema
from monitor_app.main import create_app
from monitor_app.settings.runtime import AppSettings

pytestmark = pytest.mark.server_db

LATE = dt.datetime(2026, 10, 21, 23, 59, 59, 700000)


def _config() -> MonitorConfig:
    return MonitorConfig(
        tables={
            "m": TableDef(columns={"id": "int", "ts": "datetime"}),
            "s": TableDef(columns={"ts": "datetime", "v": "float"}, primary_key="ts"),
        },
        views={"by_day": ViewDef(query="SELECT id, ts FROM m ORDER BY ts", title="d")},
    )


def test_fractional_seconds_are_kept(server_url):
    db = Database(server_url)
    registry = TableRegistry(_config())
    registry.create_all(db)
    try:
        with db.connect() as conn:
            insert_rows(conn, registry.get("m"), [{"id": 1, "ts": LATE}])
        with db.readonly() as conn:
            stored = conn.execute(select(registry.get("m").c.ts)).scalar()
        assert stored == LATE  # 翌日の 00:00:00 に丸めない
        assert inspect_schema(db, registry).problems == []
    finally:
        db.dispose()


def test_resending_datetime_keys_is_counted_correctly(server_url):
    app = create_app(_config(), AppSettings(database_url=server_url))
    batch = [
        {"ts": "2026-10-21T08:00:00.4", "v": 1},
        {"ts": "2026-10-21T23:59:59.7", "v": 2},
    ]
    with TestClient(app) as client:
        first = client.post("/api/ingest/s?on_conflict=ignore", json=batch).json()
        again = client.post("/api/ingest/s?on_conflict=ignore", json=batch).json()
        got = client.get("/api/tables/s/2026-10-21T23:59:59.700")
    assert (first["inserted"], again["inserted"], again["skipped"]) == (2, 0, 2)
    assert got.status_code == 200
    assert got.json()["data"]["v"] == 2


def test_existing_mysql_datetime_without_fraction_is_reported(server_url):
    if not server_url.startswith("mysql"):
        pytest.skip("MySQL の DATETIME の確認")
    db = Database(server_url)
    try:
        with db.connect() as conn:
            conn.execute(
                text("CREATE TABLE s (ts DATETIME NOT NULL PRIMARY KEY, v DOUBLE)")
            )
            conn.execute(text("CREATE TABLE m (id INTEGER PRIMARY KEY, ts DATETIME)"))
            conn.execute(text("INSERT INTO s VALUES ('2026-10-21 08:00:00', 1)"))
        registry = TableRegistry(_config())
        report = inspect_schema(db, registry)
        problems = {(p.kind, p.table): p for p in report.problems}
        assert set(problems) == {(DATETIME_PRECISION, "s"), (DATETIME_PRECISION, "m")}
        fix_s = problems[(DATETIME_PRECISION, "s")].fix_sql
        assert fix_s == "ALTER TABLE s MODIFY ts DATETIME(6) NOT NULL"
        assert problems[(DATETIME_PRECISION, "m")].fix_sql == (
            "ALTER TABLE m MODIFY ts DATETIME(6)"
        )
        assert "DATETIME(6)" in problems[(DATETIME_PRECISION, "s")].message
        # 案内の SQL を実行すれば直り、値と主キーは保たれる
        with db.connect() as conn:
            for problem in problems.values():
                conn.execute(text(problem.fix_sql))
        assert inspect_schema(db, registry).problems == []
        with db.connect() as conn:
            insert_rows(conn, registry.get("s"), [{"ts": LATE, "v": 2}])
        with db.readonly() as conn:
            rows = conn.execute(select(registry.get("s")).order_by("ts")).all()
        assert rows == [(dt.datetime(2026, 10, 21, 8, 0), 1.0), (LATE, 2.0)]
    finally:
        db.dispose()
