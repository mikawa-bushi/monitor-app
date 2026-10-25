"""v2.3.1 で取り込んだ PostgreSQL の遅れた連番を直す(D5-01)。

v2.3.1 の CSV 取り込みは id を指定して入れても連番(SERIAL)を進めなかった。2.4 に
上げて ``runserver --import-csv-if-empty``(行があるので取り込まない)で起動すると、
画面・API からの追加が主キーの重複(409)で失敗し続けていた。

実行方法は conftest.py を参照(``MONITOR_TEST_PG_URL``)。
"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from monitor_app import MonitorConfig, TableDef
from monitor_app.db.engine import Database
from monitor_app.db.registry import TableRegistry
from monitor_app.db.schema import SEQUENCE_BEHIND, inspect_schema
from monitor_app.main import create_app
from monitor_app.settings.runtime import AppSettings

pytestmark = pytest.mark.server_db


@pytest.fixture
def pg_url(server_url):
    if not server_url.startswith("postgresql"):
        pytest.skip("PostgreSQL の連番の確認")
    return server_url


def _config() -> MonitorConfig:
    return MonitorConfig(
        tables={
            "users": TableDef(columns={"id": "int", "name": "str"}, primary_key="id"),
            "codes": TableDef(
                columns={"code": "str", "name": "str"}, primary_key="code"
            ),
        }
    )


def _raw(url: str, *statements: str) -> None:
    """v2.3.1 の取り込みと同じく、id を指定して直接 INSERT する(連番は進まない)。"""
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            for sql in statements:
                conn.execute(text(sql))
    finally:
        engine.dispose()


def _last_value(url: str) -> int | None:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return conn.execute(
                text(
                    "SELECT pg_sequence_last_value("
                    "CAST(pg_get_serial_sequence('users', 'id') AS regclass))"
                )
            ).scalar()
    finally:
        engine.dispose()


def test_upgraded_database_accepts_form_inserts_after_startup(pg_url, caplog):
    config, settings = _config(), AppSettings(database_url=pg_url)
    with TestClient(create_app(config, settings)):
        pass  # 1 回目の起動で表を作る(v2.3.1 と同じ)
    _raw(pg_url, "INSERT INTO users (id, name) VALUES (1,'a'),(2,'b'),(3,'c')")

    # 起動前の check / migrate --dry-run は遅れを報告する(自動で直ることも)
    db = Database(pg_url)
    try:
        report = inspect_schema(db, TableRegistry(config))
    finally:
        db.dispose()
    [problem] = report.problems
    assert (problem.kind, problem.table, problem.level) == (
        SEQUENCE_BEHIND,
        "users",
        "info",
    )
    assert "SELECT setval(pg_get_serial_sequence('users', 'id')" in problem.message
    assert report.ok

    with (
        caplog.at_level(logging.INFO, logger="monitor_app.db"),
        TestClient(create_app(config, settings)) as client,
    ):
        r1 = client.post("/api/tables/users", json={"name": "operator"})
        r2 = client.post("/api/tables/users", json={"name": "operator2"})
    assert (r1.status_code, r2.status_code) == (201, 201), (r1.text, r2.text)
    assert (r1.json()["data"]["id"], r2.json()["data"]["id"]) == (4, 5)
    assert any("連番" in r.message and "0 → 3" in r.message for r in caplog.records)

    db = Database(pg_url)
    try:
        assert inspect_schema(db, TableRegistry(config)).problems == []
        # 遅れていなければ何もしない(何度呼んでもよい・連番を戻さない)
        assert TableRegistry(config).repair_sequences(db) == {}
    finally:
        db.dispose()
    assert _last_value(pg_url) == 5


def test_rows_added_by_another_process_while_running_are_retried(pg_url, caplog):
    config = _config()
    with TestClient(create_app(config, AppSettings(database_url=pg_url))) as client:
        assert (
            client.post("/api/tables/users", json={"name": "first"}).status_code == 201
        )
        # 稼働中にほかのプロセスが id を指定して入れた(連番は 1 のまま)
        _raw(pg_url, "INSERT INTO users (id, name) VALUES (2,'x'),(3,'y')")
        with caplog.at_level(logging.WARNING, logger="monitor_app.db"):
            res = client.post("/api/tables/users", json={"name": "second"})
    assert res.status_code == 201, res.text
    assert res.json()["data"]["id"] == 4
    assert any("やり直します" in r.message for r in caplog.records)


def test_sync_sources_and_imports_also_repair(pg_url, tmp_path):
    """CSV の取り込み(import-csv・runserver --import-csv-if-empty)も同じく直す。"""
    from monitor_app.services.importer import CsvImporter

    config = _config()
    db = Database(pg_url)
    try:
        TableRegistry(config).create_all(db)
        _raw(pg_url, "INSERT INTO users (id, name) VALUES (7,'a')")
        (tmp_path / "users.csv").write_text("id,name\n1,a\n")
        report = CsvImporter(config, TableRegistry(config), db, tmp_path).import_all(
            mode="if-empty"
        )
        assert report.total_inserted == 0  # 行があるので取り込まない
        assert _last_value(pg_url) == 7
    finally:
        db.dispose()
