"""サービスの配線(container.build_services / Services)と
create_app・app_factory のテスト。

create_app が HTTP 層だけを組み、サービスは build_services が従来どおりの条件で
組み立てること、lifespan でバックグラウンドタスクを起動・停止して DB を閉じること
(インメモリ SQLite は閉じずに中身を保つこと)を確かめる。
"""

from __future__ import annotations

import asyncio
import json
import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import inspect, text

from monitor_app import AlertRule, MonitorConfig, SourceDef, TableDef, ViewDef
from monitor_app.container import Services, build_services
from monitor_app.main import app_factory, create_app
from monitor_app.services.alert_scheduler import AlertScheduler
from monitor_app.services.ingest_watcher import IngestWatcher
from monitor_app.settings.runtime import AppSettings

#: create_app が公開してきた app.state の属性(後方互換で名前を変えない)。
STATE_ATTRS = (
    "settings",
    "config",
    "db",
    "registry",
    "connectors",
    "audit_service",
    "view_cache",
    "crud_service",
    "view_service",
    "alert_engine",
    "kpi_service",
)


def _full_config(tmp_path, *, refresh_interval_ms: int = 2500) -> MonitorConfig:
    """ソース・アラート付きの設定(全バックグラウンドタスクが組まれる)。"""
    jsonl = tmp_path / "events.jsonl"
    jsonl.write_text(json.dumps({"id": 1, "name": "a"}) + "\n", encoding="utf-8")
    return MonitorConfig(
        refresh_interval_ms=refresh_interval_ms,
        tables={
            "events": TableDef(columns={"id": "int", "name": "str"}, primary_key="id")
        },
        views={"ev": ViewDef(query="SELECT * FROM events")},
        alerts=[AlertRule(view="ev", column="id", op=">", value=100)],
        sources={
            "src": SourceDef(kind="jsonl", path=str(jsonl), table="events"),
        },
    )


def _settings(tmp_path, **overrides) -> AppSettings:
    values = {
        "database_url": f"sqlite:///{tmp_path / 'app.db'}",
        "csv_dir": tmp_path / "csv",
    }
    values.update(overrides)
    return AppSettings(**values)


def _minimal_config() -> MonitorConfig:
    return MonitorConfig(
        tables={"t": TableDef(columns={"id": "int", "x": "float"}, primary_key="id")},
        views={"v": ViewDef(query="SELECT * FROM t")},
    )


# ---------------------------------------------------------------------------
# build_services: 配線と条件
# ---------------------------------------------------------------------------
def test_build_services_wires_everything(tmp_path):
    settings = _settings(
        tmp_path,
        ingest_watch=True,
        ingest_mode="append",
        ingest_interval=0.1,
        audit_enabled=True,
    )
    services = build_services(_full_config(tmp_path), settings)
    try:
        assert isinstance(services, Services)
        assert services.settings is settings
        # ソースがあればコネクタと取り込みカーソル表が用意される。
        assert set(services.connectors) == {"src"}
        tables = set(inspect(services.db.engine).get_table_names())
        assert {"events", "_ingest_state", "_audit"} <= tables

        # サービス間で共有するもの(キャッシュ・リポジトリ・監査)。
        assert services.view_service.cache is services.view_cache
        assert services.crud_service.view_cache is services.view_cache
        assert services.crud_service.repo is services.repository
        assert services.crud_service.audit is services.audit_service
        assert services.audit_service.enabled is True
        assert services.kpi_service.db is services.db
        assert services.view_service.db is services.db

        # バックグラウンドタスク: フォルダ監視 → アラート評価の順。
        watcher, scheduler = services.background_tasks
        assert isinstance(watcher, IngestWatcher)
        assert watcher.keep is True  # ingest_mode=append
        assert watcher.interval == 0.5  # 下限 0.5 秒
        assert watcher.connectors is services.connectors
        assert watcher.importer.db is services.db
        assert watcher.csv_dir == tmp_path / "csv"
        assert isinstance(scheduler, AlertScheduler)
        assert scheduler.interval == 2.5  # refresh_interval_ms / 1000
        assert scheduler.engine is services.alert_engine
        assert scheduler.view_service is services.view_service
    finally:
        services.close()


def test_build_services_alert_interval_has_one_second_floor(tmp_path):
    services = build_services(
        _full_config(tmp_path, refresh_interval_ms=200), _settings(tmp_path)
    )
    try:
        # ingest_watch 無効ならアラート評価だけ。
        (scheduler,) = services.background_tasks
        assert isinstance(scheduler, AlertScheduler)
        assert scheduler.interval == 1.0
    finally:
        services.close()


def test_build_services_minimal_has_no_optional_parts(tmp_path):
    services = build_services(_minimal_config(), _settings(tmp_path))
    try:
        assert services.connectors is None
        assert services.background_tasks == []
        tables = set(inspect(services.db.engine).get_table_names())
        assert "t" in tables
        assert "_ingest_state" not in tables  # ソースが無ければ作らない
        assert services.audit_service.enabled is False
    finally:
        services.close()


def test_build_services_init_db_false_creates_no_tables(tmp_path):
    services = build_services(_minimal_config(), _settings(tmp_path), init_db=False)
    try:
        assert inspect(services.db.engine).get_table_names() == []
    finally:
        services.close()


def test_build_services_disposes_engine_when_wiring_fails(tmp_path, monkeypatch):
    import monitor_app.container as container

    disposed: list[bool] = []

    class _SpyDatabase(container.Database):
        def dispose(self) -> None:
            disposed.append(True)
            super().dispose()

    class _BoomRegistry(container.TableRegistry):
        def create_all(self, db) -> None:
            raise RuntimeError("DB に接続できない")

    monkeypatch.setattr(container, "Database", _SpyDatabase)
    monkeypatch.setattr(container, "TableRegistry", _BoomRegistry)
    with pytest.raises(RuntimeError, match="接続できない"):
        build_services(_minimal_config(), _settings(tmp_path))
    # 組み立て途中で失敗してもコネクションを残さない。
    assert disposed == [True]


# ---------------------------------------------------------------------------
# Services: start / stop / close
# ---------------------------------------------------------------------------
def test_services_start_stop_close(tmp_path, monkeypatch):
    services = build_services(
        _full_config(tmp_path), _settings(tmp_path, ingest_watch=True)
    )
    closed: list[bool] = []
    original_dispose = services.db.dispose

    def spy_dispose() -> None:
        closed.append(True)
        original_dispose()

    monkeypatch.setattr(services.db, "dispose", spy_dispose)

    async def go():
        await services.start()
        running = [task.running for task in services.background_tasks]
        await services.stop()
        await services.stop()  # 2 回目は何もしない
        return running

    assert asyncio.run(go()) == [True, True]
    assert all(task._task is None for task in services.background_tasks)
    services.close()
    assert closed == [True]


# ---------------------------------------------------------------------------
# create_app: HTTP 層の配線と lifespan
# ---------------------------------------------------------------------------
def test_create_app_exposes_services_on_app_state(tmp_path):
    app = create_app(_full_config(tmp_path), _settings(tmp_path))
    services = app.state.services
    try:
        assert isinstance(services, Services)
        for name in STATE_ATTRS:
            assert getattr(app.state, name) is getattr(services, name), name
        assert app.state.templates is not None
    finally:
        services.close()


def test_create_app_adds_cors_only_when_configured(tmp_path):
    def middleware_names(app) -> list[str]:
        return [m.cls.__name__ for m in app.user_middleware]

    plain = create_app(_minimal_config(), _settings(tmp_path))
    cors = create_app(
        _minimal_config(),
        _settings(tmp_path, cors_origins=["http://wallboard.local"]),
    )
    try:
        assert "CORSMiddleware" not in middleware_names(plain)
        assert "CORSMiddleware" in middleware_names(cors)
        res = TestClient(cors).get(
            "/api/health", headers={"Origin": "http://wallboard.local"}
        )
        assert res.headers["access-control-allow-origin"] == "http://wallboard.local"
        assert res.headers["x-content-type-options"] == "nosniff"  # 既存ヘッダも維持
    finally:
        plain.state.services.close()
        cors.state.services.close()


def test_create_app_defaults_settings_from_environment(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # .env を読まない場所で
    monkeypatch.setenv("MONITOR_DATABASE_URL", "sqlite:///:memory:")
    app = create_app(_minimal_config())
    try:
        assert app.state.settings.database_url == "sqlite:///:memory:"
        assert app.state.services.settings is app.state.settings
    finally:
        app.state.services.close()


def test_lifespan_starts_tasks_and_closes_db(tmp_path, monkeypatch):
    app = create_app(_full_config(tmp_path), _settings(tmp_path, ingest_watch=True))
    services = app.state.services
    closed: list[bool] = []
    monkeypatch.setattr(services, "close", lambda: closed.append(True))

    with TestClient(app) as client:
        assert client.get("/api/health").json()["status"] == "ok"
        assert all(task.running for task in services.background_tasks)
        assert closed == []

    assert all(task._task is None for task in services.background_tasks)
    assert closed == [True]
    services.db.dispose()


@pytest.mark.parametrize("url", ["sqlite:///:memory:", "sqlite://"])
def test_lifespan_keeps_in_memory_database(tmp_path, url):
    """インメモリ SQLite は lifespan の終了で破棄しない(従来どおりアプリと同じ寿命)。

    StaticPool の単一コネクションを破棄すると表ごと消えるため、lifespan の後に
    DB の中身を確かめるテストや、同じアプリで 2 回目の lifespan に入る使い方が壊れる。
    """
    app = create_app(_minimal_config(), _settings(tmp_path, database_url=url))
    with TestClient(app) as client:
        assert client.post("/api/tables/t", json={"x": 1.5}).status_code == 201

    with app.state.db.connect() as conn:
        assert conn.execute(text("SELECT x FROM t")).scalars().all() == [1.5]
    with TestClient(app) as client:
        res = client.get("/api/views/v")
        assert res.status_code == 200
        assert res.json()["data"] == [{"id": 1, "x": 1.5}]


# ---------------------------------------------------------------------------
# app_factory(uvicorn --factory / --reload 用)
# ---------------------------------------------------------------------------
@pytest.fixture
def factory_env(tmp_path, monkeypatch):
    """app_factory 用の環境。グローバルなログ設定は変えない。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MONITOR_DATABASE_URL", f"sqlite:///{tmp_path / 'app.db'}")
    monkeypatch.setattr(
        "monitor_app.logging_conf.configure_logging", lambda *a, **k: None
    )
    return tmp_path


def test_app_factory_loads_config_from_env(factory_env, monkeypatch):
    cfg = factory_env / "my_config.py"
    cfg.write_text(
        "from monitor_app import MonitorConfig, TableDef\n"
        "config = MonitorConfig(app_title='Factory',"
        " tables={'t': TableDef(columns=['id', 'name'])})\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MONITOR_CONFIG_PATH", str(cfg))
    app = app_factory()
    try:
        assert isinstance(app, FastAPI)
        assert app.title == "Factory"
        assert app.state.services.config.app_title == "Factory"
    finally:
        app.state.services.close()


def test_app_factory_config_error_logs_one_line_and_exits(factory_env, monkeypatch):
    monkeypatch.setenv("MONITOR_CONFIG_PATH", str(factory_env / "missing.py"))
    logger = logging.getLogger("monitor_app.main")
    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append
    old_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        with pytest.raises(SystemExit) as excinfo:
            app_factory()
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)

    assert excinfo.value.code == 1
    assert excinfo.value.__cause__ is None  # 元の例外を連鎖させない(短い表示)
    assert len(records) == 1
    record = records[0]
    assert record.levelno == logging.ERROR
    assert record.exc_info is None  # トレースバックを出さない
    assert "config.py が見つかりません" in record.getMessage()
