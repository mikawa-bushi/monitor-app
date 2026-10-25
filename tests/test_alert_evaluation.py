"""アラート評価の入力と回数のテスト(PERF-09 / ARCH-01)。

評価するのはビューの全データ(limit なし)だけで、同じデータ(同じハッシュ)なら
評価し直さない。AlertScheduler と GET・一括取得・SSE が評価済みの記録を共有する。
"""

from __future__ import annotations

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from monitor_app import AlertRule, MonitorConfig, TableDef, ViewDef
from monitor_app.api.routers.views import _sse_event_stream
from monitor_app.main import create_app
from monitor_app.services.alert_scheduler import AlertScheduler
from monitor_app.services.alert_service import AlertEngine
from monitor_app.services.notifiers.base import ConsoleNotifier
from monitor_app.settings.runtime import AppSettings


def _config() -> MonitorConfig:
    return MonitorConfig(
        tables={"t": TableDef(columns={"id": "int", "temp": "float"})},
        views={"v": ViewDef(query="SELECT id, temp FROM t ORDER BY id")},
        alerts=[
            AlertRule(
                view="v",
                column="temp",
                op=">",
                value=80,
                level="critical",
                notify=["console"],
            )
        ],
    )


def _settings(**overrides) -> AppSettings:
    return AppSettings(_env_file=None, database_url="sqlite://", **overrides)


@pytest.fixture
def notified(monkeypatch) -> list[tuple[str, str]]:
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        ConsoleNotifier,
        "send",
        lambda self, subject, body, level: sent.append((subject, level)),
    )
    return sent


@pytest.fixture
def evaluations(monkeypatch) -> list[str]:
    """ルール評価(_evaluate_locked)が実際に走った回数を数える。"""
    calls: list[str] = []
    original = AlertEngine._evaluate_locked

    def spy(self, view_name, rows):
        calls.append(view_name)
        return original(self, view_name, rows)

    monkeypatch.setattr(AlertEngine, "_evaluate_locked", spy)
    return calls


@pytest.fixture
def app():
    application = create_app(_config(), _settings())
    client = TestClient(application)
    client.post("/api/ingest/t", json=[{"id": 1, "temp": 20}, {"id": 2, "temp": 95}])
    return application


# ---------------------------------------------------------------------------
# AlertEngine.evaluate_view_if_changed
# ---------------------------------------------------------------------------
def test_same_digest_is_not_evaluated_again(evaluations):
    engine = AlertEngine(_config(), _settings())
    rows = [{"temp": 95}]
    assert engine.evaluate_view_if_changed("v", "d1", rows) is True
    assert engine.evaluate_view_if_changed("v", "d1", rows) is False
    assert evaluations == ["v"]
    assert engine.evaluate_view_if_changed("v", "d2", [{"temp": 20}]) is True
    assert engine.active_alerts() == []


def test_direct_evaluation_resets_the_memo():
    engine = AlertEngine(_config(), _settings())
    engine.evaluate_view_if_changed("v", "d1", [{"temp": 95}])
    engine.evaluate_view("v", [{"temp": 20}])  # 直接渡したデータで復帰した
    assert engine.active_alerts() == []
    # 同じハッシュでも、直接の評価で状態が変わったので評価し直す
    assert engine.evaluate_view_if_changed("v", "d1", [{"temp": 95}]) is True
    assert engine.active_alerts()


def test_older_snapshot_does_not_overwrite_a_newer_evaluation(notified):
    engine = AlertEngine(_config(), _settings())
    assert engine.evaluate_view_if_changed("v", "new", [{"temp": 20}], version=7)
    # 遅れて届いた古いデータ(違反あり)では評価し直さない(誤った発火を防ぐ)
    assert not engine.evaluate_view_if_changed("v", "old", [{"temp": 95}], version=3)
    assert engine.active_alerts() == []
    assert notified == []
    assert engine.evaluate_view_if_changed("v", "newer", [{"temp": 95}], version=8)
    assert [level for _, level in notified] == ["critical"]


# ---------------------------------------------------------------------------
# HTTP の読み取り経路
# ---------------------------------------------------------------------------
def test_paged_get_does_not_evaluate_but_reports_active_alerts(app, evaluations):
    client = TestClient(app)
    page = client.get("/api/views/v", params={"limit": 1}).json()
    assert evaluations == []  # 一部の行では評価しない
    assert page["alerts"] == []

    client.get("/api/views/v")  # 全データで評価 → 発火
    page = client.get("/api/views/v", params={"limit": 1}).json()
    assert len(page["alerts"]) == 1  # ページにも現在のアラートは付く
    assert evaluations == ["v"]


def test_repeated_reads_of_unchanged_data_evaluate_once(app, evaluations, notified):
    client = TestClient(app)
    for _ in range(5):
        client.get("/api/views/v")
        client.get("/api/views/batch", params={"names": "v"})
    assert evaluations == ["v"]
    assert [level for _, level in notified] == ["critical"]


def test_a_data_change_is_evaluated_again(app, evaluations, notified):
    client = TestClient(app)
    client.get("/api/views/v")
    client.put("/api/tables/t/2", json={"temp": 30})  # 違反が解消
    assert client.get("/api/views/v").json()["alerts"] == []
    assert evaluations == ["v", "v"]
    assert [level for _, level in notified] == ["critical", "info"]


def test_scheduler_and_requests_share_the_evaluation(app, evaluations):
    scheduler = AlertScheduler(app.state.alert_engine, app.state.view_service, 1.0)
    scheduler.run_once()
    TestClient(app).get("/api/views/v")
    scheduler.run_once()
    assert evaluations == ["v"]
    assert app.state.alert_engine.active_alerts()


def test_sse_evaluates_full_data(app, evaluations):
    flags = [False, True]

    async def is_disconnected():
        return flags.pop(0)

    async def drive():
        return [
            ev
            async for ev in _sse_event_stream(
                app.state.view_service,
                app.state.alert_engine,
                "v",
                0.0,
                is_disconnected,
            )
        ]

    events = asyncio.run(drive())
    assert evaluations == ["v"]
    assert '"critical"' in events[0]["data"]


def test_sse_sends_a_new_frame_when_only_the_alerts_change():
    config = _config()
    config.views["w"] = ViewDef(query="SELECT 1 AS id, 1.0 AS temp")
    config.alerts.append(AlertRule(view="w", column="temp", op=">", value=80))
    app = create_app(config, _settings())
    engine = app.state.alert_engine
    calls = {"n": 0}
    flags = [False, False, False, True]

    async def is_disconnected():
        calls["n"] += 1
        if calls["n"] == 2:
            # v のデータはそのままで、別のビュー w のアラートが発火した
            engine.evaluate_view("w", [{"temp": 99}])
        return flags.pop(0)

    async def drive():
        return [
            ev
            async for ev in _sse_event_stream(
                app.state.view_service, engine, "v", 0.0, is_disconnected
            )
        ]

    events = asyncio.run(drive())
    assert len(events) == 2  # データが同じでもアラートが変われば送り直す
    assert '"alerts":[]' in events[0]["data"]
    assert '"view":"w"' in events[1]["data"]


def test_paginating_client_with_the_scheduler_does_not_flap(notified):
    """監査の再現手順: ページ取得するクライアントとスケジューラが交互に動いても通知は 1 回。"""
    app = create_app(_config(), _settings())
    client = TestClient(app)
    client.post("/api/ingest/t", json=[{"id": 1, "temp": 20}, {"id": 2, "temp": 95}])
    scheduler = AlertScheduler(app.state.alert_engine, app.state.view_service, 1.0)
    for _ in range(5):
        scheduler.run_once()
        client.get("/api/views/v", params={"limit": 1})
        client.get("/api/views/v", params={"limit": 1, "offset": 0})
    deadline = time.monotonic() + 1.0
    while not notified and time.monotonic() < deadline:
        time.sleep(0.01)
    assert [level for _, level in notified] == ["critical"]


def test_scheduler_falls_back_for_duck_typed_engines():
    class _Engine:
        def __init__(self):
            self.evaluated = []

        def alert_views(self):
            return ["v"]

        def evaluate_view(self, name, rows):
            self.evaluated.append((name, rows))

    class _Snapshots:  # get_snapshot はあるがエンジンが旧来のもの
        def get_snapshot(self, name):
            raise AssertionError("使わない")

        def get_view(self, name):
            return {"data": [{"temp": 1}]}

    engine = _Engine()
    AlertScheduler(engine, _Snapshots(), 1.0).run_once()
    assert engine.evaluated == [("v", [{"temp": 1}])]
