"""KPI の評価結果をビューの共有キャッシュに載せるテスト(PERF-03)。"""

from __future__ import annotations

import sqlite3
import time
from contextlib import closing

import pytest
from contract import helpers
from fastapi.testclient import TestClient
from sqlalchemy import event

from monitor_app import KpiCard, MonitorConfig, TableDef, ViewDef
from monitor_app.db.engine import Database
from monitor_app.main import create_app
from monitor_app.services.kpi_service import CACHE_KEY, KpiService
from monitor_app.services.view_cache import ViewCache
from monitor_app.settings.runtime import AppSettings


def _config(**kpis: KpiCard) -> MonitorConfig:
    return MonitorConfig(
        tables={"t": TableDef(columns={"id": "int", "v": "float", "ts": "str"})},
        views={"v": ViewDef(query="SELECT * FROM t")},
        kpis=kpis
        or {
            "n": KpiCard(title="件数", query="SELECT COUNT(*) FROM t"),
            "max": KpiCard(title="最大", query="SELECT MAX(v) FROM t"),
        },
    )


def _count_kpi_queries(db) -> list[str]:
    seen: list[str] = []

    @event.listens_for(db.engine, "before_cursor_execute")
    def _spy(conn, cursor, statement, params, context, executemany):
        if statement.startswith("SELECT COUNT(*)") or "datetime('now'" in statement:
            seen.append(statement)

    return seen


@pytest.fixture
def memory_db():
    db = Database("sqlite://")
    yield db
    db.dispose()


@pytest.fixture
def file_app(tmp_path):
    path = tmp_path / "kpi.db"
    app = create_app(
        _config(),
        AppSettings(
            _env_file=None, database_url=helpers.sqlite_url(path), view_cache_ttl_ms=20
        ),
    )
    yield app, path
    app.state.services.close()


def test_kpis_are_cached_until_the_data_changes(file_app):
    app, path = file_app
    client = TestClient(app)
    queries = _count_kpi_queries(app.state.db)
    first = client.get("/api/kpis").json()["kpis"]
    time.sleep(0.05)  # TTL(20 ms)を過ぎても、データが同じなら評価し直さない
    assert client.get("/api/kpis").json()["kpis"] == first
    assert len(queries) == 1

    with closing(sqlite3.connect(path)) as conn:  # 外部プロセス相当の書き込み
        conn.execute("INSERT INTO t (v, ts) VALUES (5, 'x')")
        conn.commit()
    after = {k["key"]: k["value"] for k in client.get("/api/kpis").json()["kpis"]}
    assert after == {"n": 1.0, "max": 5.0}
    assert len(queries) == 2


def test_kpi_service_shares_the_view_cache(file_app):
    app, _path = file_app
    assert app.state.kpi_service.cache is app.state.view_cache


def test_time_relative_kpis_follow_the_ttl(tmp_path):
    config = _config(
        recent=KpiCard(
            title="直近",
            query="SELECT COUNT(*) FROM t WHERE ts > datetime('now', '-1 day')",
        )
    )
    app = create_app(
        config,
        AppSettings(
            _env_file=None,
            database_url=helpers.sqlite_url(tmp_path / "t.db"),
            view_cache_ttl_ms=1000,
        ),
    )
    clock = {"now": 1000.0}
    app.state.view_cache._clock = lambda: clock["now"]
    try:
        queries = _count_kpi_queries(app.state.db)
        service = app.state.kpi_service
        service.evaluate_all()
        clock["now"] += 0.5
        service.evaluate_all()
        assert len(queries) == 1  # TTL 内は共有
        clock["now"] += 0.6
        service.evaluate_all()  # データが同じでも now() の結果は変わり得る
        assert len(queries) == 2
    finally:
        app.state.services.close()


def test_results_with_a_failed_kpi_are_not_kept(memory_db):
    config = _config(
        bad=KpiCard(title="壊れた", query="SELECT * FROM does_not_exist"),
        good=KpiCard(title="正常", query="SELECT 42"),
    )
    cache = ViewCache(60_000)
    service = KpiService(config, memory_db, cache=cache)
    results = {r["key"]: r for r in service.evaluate_all()}
    assert results["bad"]["value"] is None and results["good"]["value"] == 42.0
    assert cache.size == 0  # 一時的な失敗を含む結果をキャッシュに残さない


def test_returned_cards_are_copies(memory_db):
    cache = ViewCache(60_000)
    service = KpiService(
        _config(one=KpiCard(title="1", query="SELECT 1")), memory_db, cache
    )
    cards = service.evaluate_all()
    cards[0]["value"] = "書き換え"
    cards.append({"key": "extra"})
    assert service.evaluate_all() == [
        {
            "key": "one",
            "title": "1",
            "value": 1.0,
            "display": "1",
            "unit": "",
            "target": None,
            "status": "neutral",
            "link_view": None,
        }
    ]
    assert CACHE_KEY in cache._store


def test_kpis_without_a_cache_still_work(memory_db):
    service = KpiService(_config(one=KpiCard(title="1", query="SELECT 1")), memory_db)
    assert service.evaluate_all()[0]["value"] == 1.0
