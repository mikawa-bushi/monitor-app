"""取り込みに失敗した CSV をフォルダ監視が取り込み直す(D4-07)。

以前は変更を見つけた時点で mtime を記録したため、一時的な失敗(ほかの書き込みの
ロック待ちの時間切れなど)の後は CSV が変わるまで取り込み直さず、次の成功した回で
``/api/health`` も OK に戻っていた。
"""

from __future__ import annotations

import logging
import sqlite3

import pytest
from sqlalchemy import text

import monitor_app.services.ingest_watcher as watcher_mod
from monitor_app import MonitorConfig, TableDef
from monitor_app.db.engine import Database
from monitor_app.db.registry import TableRegistry
from monitor_app.services.importer import CsvImporter
from monitor_app.services.ingest_watcher import IngestWatcher


@pytest.fixture(autouse=True)
def _propagate(monkeypatch):
    monkeypatch.setattr(logging.getLogger("monitor_app"), "propagate", True)


@pytest.fixture
def env(tmp_path):
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()
    config = MonitorConfig(
        tables={"items": TableDef(columns={"id": "int", "name": "str"})}
    )
    db = Database(
        f"sqlite:///{(tmp_path / 'app.db').as_posix()}", sqlite_busy_timeout_ms=50
    )
    registry = TableRegistry(config)
    registry.create_all(db)
    yield config, registry, db, csv_dir, tmp_path / "app.db"
    db.dispose()


def _count(db) -> int:
    with db.connect() as conn:
        return conn.execute(text("SELECT COUNT(*) FROM items")).scalar()


@pytest.mark.parametrize("keep", [False, True], ids=["replace", "append"])
def test_a_csv_that_failed_on_a_locked_db_is_retried(env, keep):
    config, registry, db, csv_dir, db_file = env
    csv = csv_dir / "items.csv"
    csv.write_text("id,name\n1,a\n2,b\n")
    importer = CsvImporter(config, registry, db, csv_dir)
    importer.import_table("items", csv)
    watcher = IngestWatcher(importer, 0.5, keep=keep)
    watcher.setup()
    blocker = sqlite3.connect(db_file, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")  # ほかの書き込みがロックを持っている
    csv.write_text("id,name\n1,a\n2,b\n3,c\n4,d\n")
    try:
        assert watcher.poll_once() == ["items"]
        watcher.record_result()
        assert watcher.last_error == "OperationalError"
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    assert _count(db) == 2
    watcher.poll_once()  # ロックが外れた後の次の回で取り込み直す
    watcher.record_result()
    assert _count(db) == 4
    assert watcher.last_error is None
    assert watcher.poll_once() == []  # 成功した後は変わるまで読まない


def test_the_error_stays_visible_until_the_retry_succeeds(env, monkeypatch):
    config, registry, db, csv_dir, _db_file = env
    csv = csv_dir / "items.csv"
    csv.write_text("id,name\n1,a\n")
    importer = CsvImporter(config, registry, db, csv_dir)
    watcher = IngestWatcher(importer, 1.0, keep=False)
    watcher.setup()
    clock = [1000.0]
    monkeypatch.setattr(watcher_mod.time, "monotonic", lambda: clock[0])
    calls = []

    def flaky(*args, **kwargs):
        calls.append(clock[0])
        raise RuntimeError("一時的な失敗")

    monkeypatch.setattr(importer, "import_table", flaky)
    csv.write_text("id,name\n1,a\n2,b\n")
    for _ in range(4):  # 1 回目の後は間隔を伸ばす(1 秒・2 秒…)
        watcher.poll_once()
        watcher.record_result()
        assert watcher.last_error == "RuntimeError"
        clock[0] += 0.5
    assert calls == [1000.0, 1000.5, 1001.5]
    assert watcher._failed["items"].count == 3
    # ファイルが変わったら待たずに取り込み直す
    monkeypatch.undo()
    csv.write_text("id,name\n1,a\n2,b\n3,c\n")
    watcher.poll_once()
    watcher.record_result()
    assert watcher.last_error is None and _count(db) == 3


def test_retry_delay_is_capped(env):
    config, registry, db, csv_dir, _db_file = env
    watcher = IngestWatcher(CsvImporter(config, registry, db, csv_dir), 2.0, keep=True)
    for _ in range(12):
        failure = watcher._mark_failed("items", RuntimeError("x"))
    assert failure.count == 12
    assert (
        failure.retry_at - watcher_mod.time.monotonic() <= watcher_mod.MAX_RETRY_DELAY
    )
