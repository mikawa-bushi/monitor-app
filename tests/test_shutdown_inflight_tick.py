"""停止のときに実行中のバックグラウンド処理(tick)を待ちすぎず、壊さずに止めること。

- 停止の指示は実行中の tick へ届き、次の SQL で打ち切られる(トランザクションは取り消し)。
- SQL を使わない処理で止まらなくても、``stop_timeout`` 秒で待つのをやめる。
- tick はデーモンスレッドで動くため、待ちきれなかった処理がプロセスの終了(コンテナで
  PID 1 のときの ``docker stop``)を引き延ばさない。
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest
from sqlalchemy import event, func, insert, select

from monitor_app import MonitorConfig, TableDef
from monitor_app.cancel import TaskStopped, stop_requested, stop_scope
from monitor_app.container import Services
from monitor_app.db.engine import Database
from monitor_app.db.registry import TableRegistry
from monitor_app.services.importer import CsvImporter
from monitor_app.services.periodic import PeriodicTask


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@pytest.fixture
def log():
    logger = logging.getLogger("tests.shutdown")
    handler = _Capture()
    logger.addHandler(handler)
    old = logger.level
    logger.setLevel(logging.DEBUG)
    yield handler
    logger.removeHandler(handler)
    logger.setLevel(old)


def _config() -> MonitorConfig:
    return MonitorConfig(
        tables={
            "items": TableDef(columns={"id": "int", "name": "str"}, primary_key="id")
        }
    )


@pytest.fixture
def db(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'app.db'}")
    registry = TableRegistry(_config())
    registry.create_all(database)
    yield database, registry
    database.dispose()


class _Task(PeriodicTask):
    name = "slow task"
    _log = logging.getLogger("tests.shutdown")

    def __init__(self, body) -> None:
        super().__init__(0.01)
        self.body = body
        self.started = threading.Event()
        self.finished = threading.Event()
        self.ticks = 0

    def tick(self) -> None:
        self.ticks += 1
        self.started.set()
        try:
            self.body()
        finally:
            self.finished.set()


async def _started(task: _Task, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not task.started.is_set():
        assert time.monotonic() < deadline, "tick が始まりませんでした"
        await asyncio.sleep(0.01)


def _count(database: Database, registry: TableRegistry) -> int:
    with database.readonly() as conn:
        return conn.execute(
            select(func.count()).select_from(registry.get("items"))
        ).scalar_one()


def test_a_long_sql_tick_is_aborted_at_the_next_statement_and_rolled_back(db, log):
    database, registry = db
    table = registry.get("items")

    def write_for_a_while() -> None:
        deadline = time.monotonic() + 5  # 打ち切られなければ 5 秒書き続ける
        with database.connect() as conn:
            i = 0
            while time.monotonic() < deadline:
                conn.execute(insert(table).values(id=i, name="x"))
                i += 1
                time.sleep(0.001)

    task = _Task(write_for_a_while)

    async def go() -> tuple[bool, float]:
        task.start()
        await _started(task)
        await asyncio.sleep(0.1)  # 書き込みの途中
        began = time.monotonic()
        stopped = await task.stop()
        return stopped, time.monotonic() - began

    stopped, took = asyncio.run(go())
    assert stopped is True
    assert took < 1.0
    assert task.finished.is_set()
    assert _count(database, registry) == 0  # 書きかけのトランザクションは取り消し
    assert "slow task: 停止の指示を受けて処理を打ち切りました" in log.messages
    assert "slow task stopped" in log.messages


def test_a_replace_import_is_aborted_and_keeps_the_old_rows(db, tmp_path):
    database, registry = db
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()
    path = csv_dir / "items.csv"
    path.write_text("id,name\n1,old\n2,old\n", encoding="utf-8")
    importer = CsvImporter(_config(), registry, database, csv_dir)
    importer.import_table("items", path)
    with path.open("w", encoding="utf-8") as fh:
        fh.write("id,name\n")
        fh.writelines(f"{i},new\n" for i in range(300_000))
    inserting = threading.Event()

    def mark(*_args) -> None:
        inserting.set()

    event.listen(database.engine, "before_cursor_execute", mark)
    task = _Task(lambda: importer.import_table("items", path))

    async def go() -> float:
        task.start()
        deadline = time.monotonic() + 10
        while not inserting.is_set() or not task.started.is_set():
            assert time.monotonic() < deadline
            await asyncio.sleep(0.001)
        began = time.monotonic()
        assert await task.stop()
        return time.monotonic() - began

    took = asyncio.run(go())
    event.remove(database.engine, "before_cursor_execute", mark)
    assert took < 2.0
    assert task.finished.wait(30)
    with database.readonly() as conn:
        rows = conn.execute(select(registry.get("items").c.name)).scalars().all()
    assert rows == ["old", "old"]  # 置き換えは取り消され、元の行が残る


def test_a_tick_that_does_not_stop_is_given_up_after_stop_timeout(log):
    release = threading.Event()
    task = _Task(lambda: release.wait(10))
    task.stop_timeout = 0.2

    async def go() -> tuple[bool, float]:
        task.start()
        await _started(task)
        began = time.monotonic()
        stopped = await task.stop()
        return stopped, time.monotonic() - began

    try:
        stopped, took = asyncio.run(go())
        assert stopped is False
        assert 0.15 < took < 1.5
        assert task._thread is not None and task._thread.daemon
        assert any("0.2 秒以内に終わらない" in m for m in log.messages)
    finally:
        release.set()


def test_long_loops_without_sql_can_check_the_stop_request():
    seen: list[bool] = []

    def loop() -> None:
        while not stop_requested():
            time.sleep(0.005)
        seen.append(True)

    task = _Task(loop)

    async def go() -> float:
        task.start()
        await _started(task)
        began = time.monotonic()
        assert await task.stop()
        return time.monotonic() - began

    assert asyncio.run(go()) < 1.0
    assert seen == [True]


def test_a_restarted_task_gets_a_fresh_stop_request():
    task = _Task(lambda: None)

    async def go() -> None:
        task.start()
        await _started(task)
        await task.stop()
        first = task.stopping
        assert first.is_set()
        task.started.clear()
        task.start()
        await _started(task)
        assert task.stopping is not first and not task.stopping.is_set()
        await task.stop()

    asyncio.run(go())


def test_requests_outside_a_task_are_never_aborted(db):
    database, registry = db
    stopping = threading.Event()
    stopping.set()
    assert stop_requested() is False
    # 停止の指示はそれを結び付けたスレッドの処理だけに効く。
    with stop_scope(stopping):
        assert stop_requested() is True
        with pytest.raises(TaskStopped), database.connect() as conn:
            conn.execute(insert(registry.get("items")).values(id=1, name="x"))
    assert stop_requested() is False
    with database.connect() as conn:
        conn.execute(insert(registry.get("items")).values(id=1, name="x"))
    assert _count(database, registry) == 1


def test_services_stop_waits_for_all_tasks_together_within_the_budget():
    releases = [threading.Event(), threading.Event()]
    tasks = [_Task(lambda r=r: r.wait(10)) for r in releases]
    closed: list[float] = []

    class _Alerts:
        def close(self, timeout: float) -> bool:
            closed.append(timeout)
            return True

    services = Services.__new__(Services)
    services.background_tasks = tasks
    services.alert_engine = _Alerts()

    async def go() -> float:
        for task in tasks:
            task.start()
        for task in tasks:
            await _started(task)
        began = time.monotonic()
        await services.stop(timeout=0.5)
        return time.monotonic() - began

    try:
        took = asyncio.run(go())
        # 2 つのタスクを順番にではなく、まとめて待つ(合計で約 0.5 秒)。
        assert took < 0.5 + 1.0 + 0.5
        assert closed and closed[0] >= 1.0  # 通知の送り切りにも時間を残す
    finally:
        for release in releases:
            release.set()


def test_an_unfinished_tick_does_not_delay_the_process_exit(tmp_path):
    # asyncio の既定のスレッドプールで tick を動かすと、asyncio.run の終了処理と
    # インタープリタの終了がそのスレッドを待つ(コンテナで PID 1 のとき docker stop が
    # 猶予時間いっぱい待たされて強制終了になる)。
    script = tmp_path / "exit_check.py"
    script.write_text(
        textwrap.dedent("""
            import asyncio, time
            from monitor_app.services.periodic import PeriodicTask

            class Blocking(PeriodicTask):
                name = "blocking"
                stop_timeout = 0.2

                def tick(self):
                    time.sleep(30)

            async def main():
                task = Blocking(0.01)
                task.start()
                await asyncio.sleep(0.3)
                await task.stop()

            asyncio.run(main())
            print("exited")
            """),
        encoding="utf-8",
    )
    began = time.monotonic()
    proc = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=Path(__file__).parent.parent,
    )
    took = time.monotonic() - began
    assert proc.returncode == 0, proc.stderr
    assert "exited" in proc.stdout
    assert took < 10
