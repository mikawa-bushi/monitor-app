"""PeriodicTask の実行結果の記録(``last_tick`` / ``last_error``)のテスト。

``/api/health`` が表示する値。成功した tick の時刻(UTC)と、直近の失敗の例外の型名
(メッセージは記録しない)を残し、成功すると失敗の記録を消す。
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

from monitor_app.services.periodic import PeriodicTask


class _Scripted(PeriodicTask):
    """tick ごとに、渡された例外を順に送出する(None なら成功)。"""

    name = "scripted"
    _log = logging.getLogger("tests.periodic_status")

    def __init__(self, outcomes, *, setup_error=None):
        super().__init__(0.01)
        self.outcomes = list(outcomes)
        self.setup_error = setup_error
        self.ticks = 0

    def setup(self) -> None:
        if self.setup_error is not None:
            raise self.setup_error

    def tick(self) -> None:
        self.ticks += 1
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if outcome is not None:
            raise outcome


async def _run_until(task: PeriodicTask, pred, timeout: float = 2.0) -> None:
    task.start()
    try:
        deadline = time.monotonic() + timeout
        while not pred() and time.monotonic() < deadline:
            await asyncio.sleep(0.005)
    finally:
        await task.stop()


def test_new_task_has_no_results():
    task = _Scripted([])
    assert task.last_tick is None
    assert task.last_error is None


def test_successful_tick_records_the_time_in_utc():
    task = _Scripted([])
    before = datetime.now(timezone.utc)
    asyncio.run(_run_until(task, lambda: task.last_tick is not None))
    assert task.last_tick is not None
    assert task.last_tick.tzinfo is timezone.utc
    assert before <= task.last_tick <= datetime.now(timezone.utc) + timedelta(seconds=1)
    assert task.last_error is None


def test_failed_tick_records_only_the_exception_type():
    task = _Scripted([RuntimeError("secret detail"), RuntimeError("again")] * 50)
    asyncio.run(_run_until(task, lambda: task.ticks >= 2))
    assert task.last_error == "RuntimeError"
    assert task.last_tick is None  # 一度も成功していない


def test_success_after_a_failure_clears_the_error():
    task = _Scripted([ValueError("bad row")])
    asyncio.run(_run_until(task, lambda: task.last_tick is not None))
    assert task.last_error is None
    assert task.ticks >= 2


def test_failed_setup_is_recorded_and_the_task_stops():
    task = _Scripted([], setup_error=OSError("no such dir"))
    asyncio.run(_run_until(task, lambda: task.last_error is not None))
    assert task.last_error == "OSError"
    assert task.ticks == 0
    assert not task.running
