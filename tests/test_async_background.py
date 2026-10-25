"""非同期/バックグラウンド系のテスト。

対象: AlertScheduler / IngestWatcher の asyncio ループ・start/stop、
AlertEngine の非同期通知ワーカー(#14)、views の SSE ストリーム。

pytest-asyncio は導入していないため、ループ系は ``asyncio.run`` でドライブし、
インターバルを小さく上書きして数 tick 回してから停止する。
"""

from __future__ import annotations

import asyncio
import time

from fastapi.testclient import TestClient

from monitor_app import AlertRule, MonitorConfig, TableDef, ViewDef
from monitor_app.api.routers.views import _sse_event_stream
from monitor_app.db.engine import Database
from monitor_app.db.registry import TableRegistry
from monitor_app.main import create_app
from monitor_app.services.alert_scheduler import AlertScheduler
from monitor_app.services.alert_service import AlertEngine
from monitor_app.services.importer import CsvImporter
from monitor_app.services.ingest_watcher import IngestWatcher
from monitor_app.settings.runtime import AppSettings


def _wait_until(pred, timeout: float = 2.0, interval: float = 0.02) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return pred()


# ===========================================================================
# AlertScheduler
# ===========================================================================
class _FakeEngine:
    def __init__(self, views):
        self._views = views
        self.evaluated = []

    def alert_views(self):
        return self._views

    def evaluate_view(self, name, rows):
        self.evaluated.append(name)


class _CountingViewService:
    def __init__(self):
        self.calls = 0

    def get_view(self, name, **kw):
        self.calls += 1
        return {"data": []}


class _BoomViewService:
    def get_view(self, name, **kw):
        raise RuntimeError("boom")


def test_scheduler_run_once_swallows_view_error():
    # 1 ビューの評価失敗で run_once が落ちない(#13)。
    sched = AlertScheduler(_FakeEngine(["v"]), _BoomViewService(), 1.0)
    sched.run_once()  # 例外が外に漏れない


def test_scheduler_run_loop_fires_and_stops():
    engine = _FakeEngine(["v1"])
    svc = _CountingViewService()
    sched = AlertScheduler(engine, svc, 1.0)
    sched.interval = 0.05  # クランプ(>=1.0)を上書きして高速化

    async def go():
        sched.start()
        await asyncio.sleep(0.25)
        await sched.stop()

    asyncio.run(go())
    assert svc.calls >= 1  # バックグラウンドループが評価を駆動した
    assert engine.evaluated  # evaluate_view が呼ばれた
    assert sched._task is None  # stop で片付いた


def test_scheduler_stop_without_start_is_noop():
    sched = AlertScheduler(_FakeEngine([]), _CountingViewService(), 1.0)
    asyncio.run(sched.stop())  # _task が None でも例外なし


# ===========================================================================
# IngestWatcher
# ===========================================================================
def _ingest_env(tmp_path):
    config = MonitorConfig(
        tables={
            "events": TableDef(columns={"id": "int", "name": "str"}, primary_key="id")
        },
    )
    db = Database(f"sqlite:///{tmp_path / 'app.db'}")
    registry = TableRegistry(config)
    registry.create_all(db)
    return config, db, registry


def test_watcher_init_row_counts(tmp_path):
    config, db, registry = _ingest_env(tmp_path)
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()
    (csv_dir / "events.csv").write_text("id,name\n1,a\n2,b\n3,c\n")
    importer = CsvImporter(config, registry, db, csv_dir)

    watcher = IngestWatcher(importer, interval=1.0, keep=True)
    watcher._init_row_counts()
    # 起動時の有効行数がカーソル初期値になる(起動後の最初の変更で再取込しない — #9)。
    assert watcher._row_counts["events"] == 3


def test_watcher_run_loop_polls_and_stops(tmp_path):
    config, db, registry = _ingest_env(tmp_path)
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()
    (csv_dir / "events.csv").write_text("id,name\n1,a\n")
    importer = CsvImporter(config, registry, db, csv_dir)

    watcher = IngestWatcher(importer, interval=1.0, keep=False)
    watcher.interval = 0.05
    calls = []
    watcher.poll_once = lambda **kw: calls.append(1) or []  # poll をスパイ

    async def go():
        watcher.start()
        await asyncio.sleep(0.25)
        await watcher.stop()

    asyncio.run(go())
    assert len(calls) >= 1  # ループが poll_once を回した
    assert watcher._task is None


# ===========================================================================
# AlertEngine 非同期通知ワーカー(#14)
# ===========================================================================
class _RecordingNotifier:
    def __init__(self):
        self.sent = []

    def send(self, subject, body, level):
        self.sent.append((subject, body, level))


def _alert_config():
    return MonitorConfig(
        tables={"t": TableDef(columns={"id": "int", "x": "float"}, primary_key="id")},
        views={"v": ViewDef(query="SELECT 1 AS id, 1.0 AS x")},
        alerts=[
            AlertRule(
                view="v",
                column="x",
                op=">",
                value=0,
                level="critical",
                message="x が大きすぎます",
                notify=["fake"],
            )
        ],
    )


def test_async_notify_worker_dispatches():
    engine = AlertEngine(_alert_config(), AppSettings())
    rec = _RecordingNotifier()
    engine._notifiers = {"fake": rec}
    engine._use_async_notify = True  # 非同期ワーカー経路を強制(console 以外想定)

    try:
        # 違反(x=5 > 0)で発火 → 通知はキュー経由でワーカースレッドが送る。
        engine.evaluate_view("v", [{"x": 5.0}])
        assert _wait_until(lambda: len(rec.sent) >= 1)
        subject, _body, level = rec.sent[0]
        assert "異常検知" in subject
        assert level == "critical"
    finally:
        engine._notify_queue.put(None)  # ワーカースレッドを停止


def test_evaluate_rule_skips_none_and_nonnumeric():
    # None・非数値・対象列なしの行はスキップされ、違反 0 件=発火しない。
    engine = AlertEngine(_alert_config(), AppSettings())
    engine.evaluate_view("v", [{"x": None}, {"x": "abc"}, {"id": 1}])
    assert engine.active_alerts() == []


# ===========================================================================
# views: SSE ストリーム + batch
# ===========================================================================
def _view_app():
    config = MonitorConfig(
        tables={"t": TableDef(columns=["id", "v"], primary_key="id")},
        views={"v1": ViewDef(query="SELECT 1 AS id, 2 AS v", title="v1")},
    )
    return create_app(config, AppSettings(database_url="sqlite:///:memory:"))


def test_sse_event_stream_yields_then_stops():
    # SSE ジェネレータを直接駆動(HTTP ストリームはハングし得るため)。
    # is_disconnected を [False, False, True] にして 2 周回し 3 周目で停止する。
    app = _view_app()
    svc = app.state.view_service
    engine = app.state.alert_engine
    flags = [False, False, True]

    async def is_disconnected():
        return flags.pop(0)

    async def drive():
        return [
            ev
            async for ev in _sse_event_stream(svc, engine, "v1", 0.0, is_disconnected)
        ]

    events = asyncio.run(drive())
    # 初回のみ yield(2 周目は同一ハッシュで差分なし=送信しない)。
    assert len(events) == 1
    assert events[0]["event"] == "message"
    assert '"alerts"' in events[0]["data"]


def test_batch_skips_empty_names():
    with TestClient(_view_app()) as client:
        res = client.get("/api/views/batch?names=v1,,v1")
        assert res.status_code == 200
        views = res.json()["views"]
        assert "v1" in views  # 空セグメントは無視され、v1 は返る
