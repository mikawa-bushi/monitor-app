"""float の NaN / inf と、表のページ分けの順序を実サーバーで確かめる(D3-14・D3-15)。

実行方法は conftest.py を参照(``MONITOR_TEST_PG_URL`` / ``MONITOR_TEST_MYSQL_URL``)。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from monitor_app import MonitorConfig, TableDef
from monitor_app.db.engine import Database
from monitor_app.db.registry import TableRegistry
from monitor_app.main import create_app
from monitor_app.services.importer import CsvImporter
from monitor_app.settings.runtime import AppSettings

pytestmark = pytest.mark.server_db


def test_nan_and_inf_are_missing_values_on_every_database(server_url, tmp_path):
    config = MonitorConfig(
        tables={"t": TableDef(columns={"id": "int", "temp": "float"})}
    )
    (tmp_path / "t.csv").write_text("id,temp\n1,21.5\n2,NaN\n3,inf\n4,22.0\n")
    db = Database(server_url)
    try:
        report = CsvImporter(config, TableRegistry(config), db, tmp_path).import_all()
        assert report.total_inserted == 4  # MySQL でも取り込み全体が失敗しない
        with db.readonly() as conn:
            rows = dict(conn.execute(text("SELECT id, temp FROM t")).all())
            avg, top = conn.execute(text("SELECT AVG(temp), MAX(temp) FROM t")).one()
    finally:
        db.dispose()
    assert rows == {1: 21.5, 2: None, 3: None, 4: 22.0}
    assert (float(avg), float(top)) == (21.75, 22.0)


def test_table_pages_follow_the_primary_key_after_updates(server_url):
    config = MonitorConfig(tables={"p": TableDef(columns={"id": "int", "name": "str"})})
    app = create_app(config, AppSettings(database_url=server_url))
    with TestClient(app) as client:
        client.post(
            "/api/ingest/p", json=[{"id": i, "name": f"n{i}"} for i in range(1, 6)]
        )
        client.put("/api/tables/p/1", json={"name": "edited"})
        pages = [
            [
                r["id"]
                for r in client.get(f"/api/tables/p?limit=2&offset={o}").json()["data"]
            ]
            for o in (0, 2, 4)
        ]
    assert pages == [[1, 2], [3, 4], [5]]
