"""PeriodicTask(バックグラウンドタスクの共通基盤)のテスト。

setup はスレッドで 1 回だけ、tick の例外でループは止まらない、start/stop は冪等、
を確かめる。IngestWatcher / AlertScheduler がロガー名と開始・停止のログ文言を
保っていることもここで確認する。pytest-asyncio は使わず ``asyncio.run`` で駆動する。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time

import pytest

from monitor_app import MonitorConfig, TableDef
from monitor_app.db.engine import Database
from monitor_app.db.registry import TableRegistry
from monitor_app.services.alert_scheduler import AlertScheduler
from monitor_app.services.importer import CsvImporter
from monitor_app.services.ingest_watcher import IngestWatcher
from monitor_app.services.periodic import PeriodicTask


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def messages(self) -> list[str]:
        return [r.getMessage() for r in self.records]


@pytest.fixture
def capture():
    """指定ロガーのレコードを集める。

    ``monitor_app`` ロガーは propagate=False のことがあり caplog では拾えないため。"""
    attached: list[tuple[logging.Logger, _ListHandler, int]] = []

    def attach(name: str) -> _ListHandler:
        logger = logging.getLogger(name)
        handler = _ListHandler()
        attached.append((logger, handler, logger.level))
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        return handler

    yield attach
    for logger, handler, level in attached:
        logger.removeHandler(handler)
        logger.setLevel(level)


async def _wait_for(pred, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        await asyncio.sleep(0.01)
    return pred()


class _Recorder(PeriodicTask):
    """setup/tick の呼び出し回数と実行スレッドを記録するテスト用タスク。"""

    name = "recorder"
    _log = logging.getLogger("tests.periodic")

    def __init__(self, interval: float = 0.01, *, fail_tick=False, fail_setup=False):
        super().__init__(interval)
        self.fail_tick = fail_tick
        self.fail_setup = fail_setup
        self.setup_calls = 0
        self.tick_calls = 0
        self.threads: set[int] = set()
        self.ticks_seen_by_setup: int | None = None

    def setup(self) -> None:
        self.setup_calls += 1
        self.threads.add(threading.get_ident())
        self.ticks_seen_by_setup = self.tick_calls
        if self.fail_setup:
            raise RuntimeError("setup boom")

    def tick(self) -> None:
        self.tick_calls += 1
        self.threads.add(threading.get_ident())
        if self.fail_tick:
            raise RuntimeError("tick boom")


# ---------------------------------------------------------------------------
# PeriodicTask 本体
# ---------------------------------------------------------------------------
def test_setup_runs_once_in_a_thread_before_ticks(capture):
    log = capture("tests.periodic")
    task = _Recorder()

    async def go():
        loop_thread = threading.get_ident()
        task.start()
        assert await _wait_for(lambda: task.tick_calls >= 3)
        await task.stop()
        return loop_thread

    loop_thread = asyncio.run(go())
    assert task.setup_calls == 1  # 何周回っても setup は 1 回だけ
    assert task.ticks_seen_by_setup == 0  # 最初の tick より前に実行された
    # setup も tick もイベントループのスレッドではなくワーカースレッドで動く。
    assert loop_thread not in task.threads
    assert "recorder started (interval=0.0s)" in log.messages()
    assert log.messages()[-1] == "recorder stopped"


def test_tick_exceptions_are_logged_and_loop_continues(capture):
    log = capture("tests.periodic")
    task = _Recorder(fail_tick=True)

    async def go():
        task.start()
        assert await _wait_for(lambda: task.tick_calls >= 3)
        still_running = task.running
        await task.stop()
        return still_running

    assert asyncio.run(go()) is True  # 例外が続いてもループは生きている
    failures = [
        r for r in log.records if r.getMessage() == "recorder tick failed; continuing"
    ]
    assert len(failures) >= 2
    assert all(r.exc_info is not None for r in failures)  # トレースバック付きで記録


def test_setup_failure_is_logged_and_task_ends_without_ticks(capture):
    log = capture("tests.periodic")
    task = _Recorder(fail_setup=True)

    async def go():
        task.start()
        assert await _wait_for(lambda: not task.running)
        await asyncio.sleep(0.05)
        await task.stop()

    asyncio.run(go())
    assert task.tick_calls == 0
    assert "recorder setup failed; not started" in log.messages()
    assert "recorder started (interval=0.0s)" not in log.messages()


def test_start_is_idempotent():
    task = _Recorder()

    async def go():
        task.start()
        first = task._task
        task.start()  # 実行中なら何もしない
        assert task._task is first
        assert await _wait_for(lambda: task.tick_calls >= 1)
        await task.stop()

    asyncio.run(go())
    assert task.setup_calls == 1


def test_stop_is_idempotent_and_safe_before_start():
    task = _Recorder()

    async def go():
        await task.stop()  # 未起動でも例外なし
        task.start()
        assert await _wait_for(lambda: task.tick_calls >= 1)
        await task.stop()
        await task.stop()  # 2 回目も何もしない
        assert task._task is None
        assert not task.running

    asyncio.run(go())


def test_restart_after_stop_runs_setup_again():
    task = _Recorder()

    async def go():
        for expected in (1, 2):
            task.start()
            assert await _wait_for(
                lambda expected=expected: task.setup_calls == expected
            )
            await task.stop()

    asyncio.run(go())
    assert task.setup_calls == 2


def test_base_class_defaults():
    task = PeriodicTask(1.5)
    assert task.interval == 1.5
    assert task._task is None and not task.running
    task.setup()  # 既定の setup は何もしない
    with pytest.raises(NotImplementedError):
        task.tick()


# ---------------------------------------------------------------------------
# IngestWatcher / AlertScheduler(サブクラス)
# ---------------------------------------------------------------------------
def _watcher(tmp_path, *, keep: bool) -> IngestWatcher:
    config = MonitorConfig(
        tables={
            "events": TableDef(columns={"id": "int", "name": "str"}, primary_key="id")
        },
    )
    db = Database(f"sqlite:///{tmp_path / 'app.db'}")
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()
    (csv_dir / "events.csv").write_text("id,name\n1,a\n2,b\n")
    return IngestWatcher(
        CsvImporter(config, TableRegistry(config), db, csv_dir), 0.5, keep=keep
    )


def test_ingest_watcher_setup_runs_in_thread_and_keeps_log_messages(tmp_path, capture):
    log = capture("monitor_app.ingest")
    watcher = _watcher(tmp_path, keep=True)
    watcher.interval = 0.01  # 下限(0.5 秒)を上書きして高速化
    setup_threads: list[int] = []
    original = watcher._init_row_counts

    def spy() -> None:
        setup_threads.append(threading.get_ident())
        original()

    watcher._init_row_counts = spy
    polls: list[int] = []
    watcher.poll_once = lambda **kw: polls.append(1) or []

    async def go():
        loop_thread = threading.get_ident()
        watcher.start()
        assert await _wait_for(lambda: len(polls) >= 1)
        await watcher.stop()
        return loop_thread

    loop_thread = asyncio.run(go())
    # 起動時の重い初期化(行数カウント)はイベントループを止めずスレッドで行う。
    assert setup_threads and setup_threads[0] != loop_thread
    assert watcher._row_counts == {"events": 2}
    assert watcher._mtimes  # 既存ファイルの mtime を記録済み(即発火しない)
    assert watcher._task is None
    assert log.messages()[0] == "ingest watcher started (interval=0.0s)"
    assert log.messages()[-1] == "ingest watcher stopped"
    watcher.importer.db.dispose()


def test_ingest_watcher_poll_failure_keeps_legacy_message(tmp_path, capture):
    log = capture("monitor_app.ingest")
    watcher = _watcher(tmp_path, keep=False)
    watcher.interval = 0.01

    def boom(**_kw):
        raise RuntimeError("poll boom")

    watcher.poll_once = boom

    async def go():
        watcher.start()
        assert await _wait_for(
            lambda: "ingest watcher poll failed; continuing" in log.messages()
        )
        await watcher.stop()

    asyncio.run(go())
    watcher.importer.db.dispose()


class _Engine:
    def __init__(self) -> None:
        self.evaluated: list[str] = []

    def alert_views(self) -> list[str]:
        return ["v1", "v2"]

    def evaluate_view(self, name, rows) -> None:
        self.evaluated.append(name)


class _ViewService:
    def get_view(self, name):
        return {"data": []}


def test_alert_scheduler_is_a_periodic_task_with_legacy_logging(capture):
    log = capture("monitor_app.alerts")
    engine = _Engine()
    sched = AlertScheduler(engine, _ViewService(), 0.2)
    assert isinstance(sched, PeriodicTask)
    assert sched.interval == 1.0  # 1 秒未満は 1 秒に切り上げる
    sched.interval = 0.01

    async def go():
        sched.start()
        assert await _wait_for(lambda: len(engine.evaluated) >= 2)
        await sched.stop()

    asyncio.run(go())
    assert log.messages()[0] == "alert scheduler started (interval=0.0s, views=2)"
    assert log.messages()[-1] == "alert scheduler stopped"
    assert all(r.name == "monitor_app.alerts" for r in log.records)
