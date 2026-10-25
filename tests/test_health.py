"""``/api/health``(死活監視)のテスト。

DB への ``SELECT 1`` の結果で 200 / 503 を返すこと、バージョンとバックグラウンドタスクの
状態を含むこと、認証なしで読めるため秘密や SQL を本文に出さないことを確かめる。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

import monitor_app
from monitor_app import AlertRule, MonitorConfig, TableDef, ViewDef
from monitor_app.main import create_app
from monitor_app.services.periodic import PeriodicTask
from monitor_app.settings.runtime import AppSettings

SECRET = "s3cret-pw"


def _settings(**overrides) -> AppSettings:
    values = {"database_url": "sqlite://", "db_wait_timeout": 0} | overrides
    return AppSettings(_env_file=None, **values)


def _config_with_alert() -> MonitorConfig:
    return MonitorConfig(
        tables={"t": TableDef(columns={"id": "int", "temp": "float"})},
        views={"v": ViewDef(query="SELECT id, temp FROM t")},
        alerts=[AlertRule(view="v", column="temp", op=">", value=80)],
    )


@pytest.fixture
def make_app():
    """create_app で組み、テストの後で DB のコネクションを閉じる(インメモリでも)。"""
    apps = []

    def make(config: MonitorConfig, settings: AppSettings, **kwargs):
        apps.append(create_app(config, settings, **kwargs))
        return apps[-1]

    yield make
    for app in apps:
        app.state.services.db.dispose()


def _break_database(app, monkeypatch) -> None:
    def readonly():
        raise OperationalError(
            "SELECT 1", {}, Exception(f"password authentication failed ({SECRET})")
        )

    monkeypatch.setattr(app.state.services.db, "readonly", readonly)


def test_health_reports_version_database_and_tasks(client):
    res = client.get("/api/health")
    assert res.status_code == 200
    assert res.json() == {
        "status": "ok",
        "version": monitor_app.__version__,
        "database": "ok",
        "tasks": {},  # conftest のアプリはフォルダ監視もアラートも無い
    }


def test_health_is_503_without_details_when_the_database_fails(app, monkeypatch):
    _break_database(app, monkeypatch)
    res = TestClient(app).get("/api/health")
    assert res.status_code == 503
    body = res.json()
    assert body["status"] == "error"
    assert body["database"] == "error"
    assert body["version"] == monitor_app.__version__
    assert SECRET not in res.text
    assert "SELECT" not in res.text


def test_health_is_503_for_an_unreachable_server_database(make_app):
    """本物のドライバで接続できない PostgreSQL(閉じたポート)でも 503 を返す。"""
    url = f"postgresql+pg8000://monitor:{SECRET}@127.0.0.1:1/monitor"
    app = make_app(MonitorConfig(), _settings(database_url=url), init_db=False)
    res = TestClient(app).get("/api/health")
    assert res.status_code == 503
    assert res.json()["database"] == "error"
    assert SECRET not in res.text
    assert "127.0.0.1" not in res.text


def test_health_works_without_authentication_even_when_reads_are_protected(make_app):
    app = make_app(
        MonitorConfig(), _settings(api_key="key", protect_reads=True), init_db=False
    )
    assert TestClient(app).get("/api/health").status_code == 200


def test_health_shows_background_tasks_while_running(make_app):
    app = make_app(_config_with_alert(), _settings())
    with TestClient(app) as client:
        tasks = client.get("/api/health").json()["tasks"]
    assert tasks == {
        "alert scheduler": {"running": True, "last_tick": None, "last_error": None}
    }


def test_health_renders_last_tick_and_last_error(make_app):
    app = make_app(_config_with_alert(), _settings())
    (task,) = app.state.services.background_tasks
    task.last_tick = datetime(2026, 10, 21, 3, 4, 5, tzinfo=timezone.utc)
    task.last_error = "OperationalError"

    status = TestClient(app).get("/api/health").json()["tasks"]["alert scheduler"]

    assert status["running"] is False  # lifespan を起動していない
    assert status["last_error"] == "OperationalError"
    rendered = datetime.fromisoformat(status["last_tick"])
    assert rendered == task.last_tick  # ローカル時刻(オフセット付き)で同じ時点
    assert rendered.utcoffset() is not None


def test_tasks_with_the_same_name_are_listed_separately(app):
    class _Task(PeriodicTask):
        name = "worker"

    app.state.services.background_tasks.extend([_Task(1.0), _Task(1.0), _Task(1.0)])
    tasks = TestClient(app).get("/api/health").json()["tasks"]
    assert list(tasks) == ["worker", "worker (2)", "worker (3)"]


@pytest.mark.parametrize("broken", [False, True])
def test_head_request_returns_the_same_status_without_a_body(app, monkeypatch, broken):
    if broken:
        _break_database(app, monkeypatch)
    res = TestClient(app).head("/api/health")
    assert res.status_code == (503 if broken else 200)
    assert res.content == b""


def test_openapi_documents_503_and_hides_head(app):
    operation = app.openapi()["paths"]["/api/health"]
    assert set(operation) == {"get"}
    assert {"200", "503"} <= set(operation["get"]["responses"])
