"""SQLite のバックアップ・復元(CRIT-11)と定期バックアップのタスク。"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from monitor_app import MonitorConfig, TableDef
from monitor_app.container import build_services
from monitor_app.db.backup import (
    backup_database,
    latest_backup_time,
    list_backups,
    restore_database,
)
from monitor_app.db.engine import Database
from monitor_app.exceptions import BackupError
from monitor_app.services.backup_task import BackupTask, build_backup_task
from monitor_app.settings.runtime import AppSettings


@pytest.fixture(autouse=True)
def _propagate_monitor_logs(monkeypatch):
    """CLI のテストが configure_logging で monitor_app を propagate=False にしていても、
    caplog(root のハンドラ)で monitor_app.* のログを拾えるようにする。"""
    monkeypatch.setattr(logging.getLogger("monitor_app"), "propagate", True)


NOW = datetime(2026, 10, 21, 8, 30, 0)


def _db(tmp_path) -> Database:
    db = Database(f"sqlite:///{(tmp_path / 'data' / 'database.db').as_posix()}")
    with db.connect() as conn:
        conn.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)"))
    return db


def _insert(db: Database, *ids: int) -> None:
    with db.connect() as conn:
        for i in ids:
            conn.execute(text("INSERT INTO t VALUES (:i, :v)"), {"i": i, "v": f"r{i}"})


def _ids(path: Path) -> list[int]:
    with closing(sqlite3.connect(path)) as conn:
        return [r[0] for r in conn.execute("SELECT id FROM t ORDER BY id")]


def test_backup_includes_rows_that_are_only_in_the_wal(tmp_path):
    db = _db(tmp_path)
    try:
        _insert(db, *range(1, 501))
        # 稼働中(接続が開いたまま)は、コミット済みの行が -wal にだけある
        assert db.journal_mode == "wal"
        assert (tmp_path / "data" / "database.db-wal").exists()

        path = backup_database(db, tmp_path / "backups", now=NOW)

        assert path == tmp_path / "backups" / "database-20261021-083000.db"
        assert _ids(path) == list(range(1, 501))
        with closing(sqlite3.connect(path)) as conn:
            # 単体で持ち運べる 1 ファイル(WAL ではない)
            assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not (tmp_path / "backups" / "database-20261021-083000.db-wal").exists()
    finally:
        db.dispose()


def test_backup_keeps_the_newest_generations_only(tmp_path):
    db = _db(tmp_path)
    folder = tmp_path / "backups"
    folder.mkdir()
    unrelated = folder / "notes.txt"
    unrelated.write_text("keep me")
    other_db = folder / "other-20200101-000000.db"
    other_db.write_text("another database's backup")
    try:
        made = [
            backup_database(db, folder, keep=3, now=NOW + timedelta(hours=i))
            for i in range(5)
        ]
        assert list_backups(folder, "database") == made[-3:]
        assert unrelated.exists() and other_db.exists()  # ほかのファイルは消さない
        assert latest_backup_time(folder, "database") == NOW + timedelta(hours=4)
    finally:
        db.dispose()


def test_backups_in_the_same_second_get_a_sequence_number(tmp_path):
    db = _db(tmp_path)
    try:
        first = backup_database(db, tmp_path / "b", now=NOW)
        second = backup_database(db, tmp_path / "b", now=NOW)
        third = backup_database(db, tmp_path / "b", now=NOW)
        assert second.name == "database-20261021-083000-2.db"
        assert third.name == "database-20261021-083000-3.db"
        assert list_backups(tmp_path / "b", "database") == [first, second, third]
    finally:
        db.dispose()


def test_keep_zero_keeps_everything(tmp_path):
    db = _db(tmp_path)
    try:
        for i in range(3):
            backup_database(db, tmp_path / "b", keep=0, now=NOW + timedelta(days=i))
        assert len(list_backups(tmp_path / "b", "database")) == 3
    finally:
        db.dispose()


def test_failed_backup_leaves_no_partial_file(tmp_path, monkeypatch):
    db = _db(tmp_path)
    folder = tmp_path / "b"

    def boom(src, dest):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr("monitor_app.db.backup._copy", boom)
    try:
        with pytest.raises(sqlite3.OperationalError):
            backup_database(db, folder, now=NOW)
        assert list(folder.iterdir()) == []
    finally:
        db.dispose()


def test_backup_rejects_other_databases(tmp_path):
    with pytest.raises(BackupError, match="pg_dump"):
        backup_database(Database("postgresql+pg8000://u:p@h/db"), tmp_path)
    with pytest.raises(BackupError, match="メモリ内"):
        backup_database(Database("sqlite://"), tmp_path)
    missing = Database(f"sqlite:///{(tmp_path / 'missing.db').as_posix()}")
    with pytest.raises(BackupError, match="ありません"):
        backup_database(missing, tmp_path / "b")


def test_restore_replaces_the_database(tmp_path):
    db = _db(tmp_path)
    try:
        _insert(db, 1, 2, 3)
        backup = backup_database(db, tmp_path / "b", now=NOW)
        _insert(db, 4, 5)
        with db.connect() as conn:
            conn.execute(text("DELETE FROM t WHERE id = 1"))

        restore_database(backup, db)

        with db.connect() as conn:  # 同じ Database から読み直せる(WAL も整合する)
            assert conn.execute(
                text("SELECT id FROM t ORDER BY id")
            ).scalars().all() == [
                1,
                2,
                3,
            ]
            assert conn.execute(text("PRAGMA integrity_check")).scalar() == "ok"
    finally:
        db.dispose()
    assert _ids(tmp_path / "data" / "database.db") == [1, 2, 3]


def test_restore_validates_the_source(tmp_path):
    db = _db(tmp_path)
    try:
        with pytest.raises(BackupError, match="見つかりません"):
            restore_database(tmp_path / "nope.db", db)
        junk = tmp_path / "junk.db"
        junk.write_bytes(b"this is not a database" * 100)
        with pytest.raises(BackupError, match="読めません"):
            restore_database(junk, db)
        with pytest.raises(BackupError, match="同じファイル"):
            restore_database(db.sqlite_path, db)
        with pytest.raises(BackupError):
            restore_database(junk, Database("postgresql+pg8000://u:p@h/db"))
    finally:
        db.dispose()


def test_list_backups_of_a_missing_folder(tmp_path):
    assert list_backups(tmp_path / "missing", "database") == []
    assert latest_backup_time(tmp_path / "missing", "database") is None


# ---------------------------------------------------------------------------
# 定期バックアップ
# ---------------------------------------------------------------------------
class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def test_backup_task_backs_up_when_due(tmp_path):
    db = _db(tmp_path)
    clock = _Clock(NOW)
    task = BackupTask(db, tmp_path / "b", interval_hours=24, keep=2, clock=clock)
    try:
        assert task.interval == 600  # 間隔が長くても 10 分ごとに要否を確かめる
        assert task.due()  # まだ 1 つも無い
        task.tick()
        assert len(list_backups(tmp_path / "b", "database")) == 1
        clock.now += timedelta(hours=23)
        task.tick()  # 前回から 24 時間たっていない
        assert len(list_backups(tmp_path / "b", "database")) == 1
        clock.now += timedelta(hours=1)
        task.tick()
        clock.now += timedelta(hours=24)
        task.tick()
        assert len(list_backups(tmp_path / "b", "database")) == 2  # keep=2
    finally:
        db.dispose()


def test_backup_task_uses_the_previous_backup_across_restarts(tmp_path):
    """毎晩電源を落とす PC でも、前回のバックアップから間隔が空けば起動直後に取る。"""
    db = _db(tmp_path)
    try:
        backup_database(db, tmp_path / "b", now=NOW - timedelta(hours=30))
        task = BackupTask(db, tmp_path / "b", interval_hours=24, clock=_Clock(NOW))
        task.setup()  # 起動時の確認
        assert latest_backup_time(tmp_path / "b", "database") == NOW
    finally:
        db.dispose()


def test_backup_task_short_interval(tmp_path):
    db = _db(tmp_path)
    try:
        task = BackupTask(db, tmp_path / "b", interval_hours=0.01)
        assert task.interval == pytest.approx(36.0)
    finally:
        db.dispose()


def test_backup_task_setup_failure_does_not_stop_the_task(tmp_path, caplog):
    db = _db(tmp_path)
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a folder")
    task = BackupTask(db, blocker / "b", interval_hours=1)
    try:
        with caplog.at_level(logging.ERROR, logger="monitor_app.backup"):
            task.setup()  # 例外を外に出さない
        assert "backup at startup failed" in caplog.text
    finally:
        db.dispose()


def test_backup_task_runs_in_the_lifespan(tmp_path):
    db = _db(tmp_path)
    task = BackupTask(db, tmp_path / "b", interval_hours=1)

    async def go():
        task.start()
        for _ in range(100):
            if list_backups(tmp_path / "b", "database"):
                break
            await asyncio.sleep(0.02)
        await task.stop()

    try:
        asyncio.run(go())
        assert len(list_backups(tmp_path / "b", "database")) == 1
    finally:
        db.dispose()


def _settings(**overrides):
    values = {"backup_dir": None, "backup_interval_hours": 24.0, "backup_keep": 7}
    values.update(overrides)
    return SimpleNamespace(**values)


def test_build_backup_task_conditions(tmp_path, caplog):
    db = _db(tmp_path)
    try:
        assert build_backup_task(db, _settings()) is None
        assert (
            build_backup_task(
                db, _settings(backup_dir=tmp_path, backup_interval_hours=0)
            )
            is None
        )
        task = build_backup_task(
            db, _settings(backup_dir=tmp_path / "b", backup_keep=3)
        )
        assert isinstance(task, BackupTask) and task.keep == 3
    finally:
        db.dispose()
    server = Database("postgresql+pg8000://u:secret@h/db")
    with caplog.at_level(logging.WARNING, logger="monitor_app.backup"):
        assert build_backup_task(server, _settings(backup_dir=tmp_path)) is None
    assert "pg_dump" in caplog.text and "secret" not in caplog.text
    assert (
        build_backup_task(Database("sqlite://"), _settings(backup_dir=tmp_path)) is None
    )


def test_build_services_schedules_the_backup(tmp_path):
    settings = AppSettings(
        _env_file=None,
        database_url=f"sqlite:///{(tmp_path / 'app.db').as_posix()}",
        backup_dir=tmp_path / "backups",
    )
    config = MonitorConfig(tables={"t": TableDef(columns=["id"])})
    services = build_services(config, settings)
    try:
        (task,) = services.background_tasks
        assert isinstance(task, BackupTask)
        assert task.dest_dir == tmp_path / "backups"
    finally:
        services.close()
    assert not os.path.exists(tmp_path / "backups")  # 起動前には作らない
