"""バックアップの頑健さ: 未来の日時のバックアップ(D3-11)・壊れたページの検出(D3-12)・
強制終了で残った一時ファイル(D3-13 / D4-02)・保存先の共有(D3-16)。"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from monitor_app.db import backup as backup_module
from monitor_app.db.backup import (
    backup_database,
    backup_owner,
    database_owner,
    latest_backup_time,
    list_backups,
    remove_stale_temp_files,
    restore_database,
    validate_backup,
)
from monitor_app.db.engine import Database
from monitor_app.exceptions import BackupError
from monitor_app.services.backup_task import BackupTask

NOW = datetime(2026, 10, 24, 9, 0, 0)


@pytest.fixture(autouse=True)
def _propagate_monitor_logs(monkeypatch):
    monkeypatch.setattr(logging.getLogger("monitor_app"), "propagate", True)
    monkeypatch.setattr(backup_module, "_foreign_warned", set())


def _make_db(path: Path, table: str = "t", rows: int = 3) -> Database:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, p TEXT)")
        conn.executemany(
            f"INSERT INTO {table} VALUES (?, ?)", [(i, "x" * 200) for i in range(rows)]
        )
    return Database(f"sqlite:///{path.as_posix()}")


def _age(path: Path, seconds: float) -> None:
    old = time.time() - seconds
    os.utime(path, (old, old))


# ---------------------------------------------------------------------------
# D3-11: 時計が進んでいたときのバックアップで定期バックアップが止まらない
# ---------------------------------------------------------------------------
def test_a_future_dated_backup_does_not_stop_scheduled_backups(tmp_path, caplog):
    db = _make_db(tmp_path / "live.db")
    folder = tmp_path / "bk"
    try:
        wrong = datetime(2027, 3, 1, 9, 0, 0)
        BackupTask(db, folder, interval_hours=24, clock=lambda: wrong).tick()
        clock = [NOW]
        task = BackupTask(db, folder, interval_hours=24, clock=lambda: clock[0])
        with caplog.at_level(logging.WARNING, logger="monitor_app.backup"):
            assert task.due()
            task.tick()
            assert not task.due()  # 取った直後は取らない(10 分ごとに取り続けない)
            clock[0] = NOW + timedelta(days=1, minutes=1)
            assert task.due()
            task.tick()
        names = [p.name for p in list_backups(folder, "live")]
        assert names == [
            "live-20261024-090000.db",
            "live-20261025-090100.db",
            "live-20270301-090000.db",
        ]
        warnings = [r for r in caplog.records if "今(" in r.message]
        assert len(warnings) == 1  # 1 回だけ
        assert "2027-03-01" in warnings[0].message
    finally:
        db.dispose()


def test_daylight_saving_shift_within_an_hour_counts_as_the_last_backup(tmp_path):
    db = _make_db(tmp_path / "live.db")
    try:
        backup_database(db, tmp_path / "bk", now=NOW + timedelta(minutes=50))
        task = BackupTask(db, tmp_path / "bk", interval_hours=24, clock=lambda: NOW)
        assert not task.due()
        assert (
            latest_backup_time(tmp_path / "bk", "live", not_after=NOW) is None
        )  # 引数で除外できる
    finally:
        db.dispose()


# ---------------------------------------------------------------------------
# D3-12: 中ほどのページが壊れたバックアップでは復元しない
# ---------------------------------------------------------------------------
def _corrupt_copy(src: Path, dest: Path) -> Path:
    data = bytearray(src.read_bytes())
    mid = (len(data) // 4096 // 2) * 4096
    data[mid : mid + 4096] = b"\xff" * 4096
    dest.write_bytes(bytes(data))
    return dest


def test_a_backup_with_a_corrupt_page_is_rejected_and_the_db_is_kept(tmp_path):
    db = _make_db(tmp_path / "live.db", rows=20000)
    try:
        good = backup_database(db, tmp_path / "bk", keep=0)
        bad = _corrupt_copy(good, tmp_path / "bad.db")
        with pytest.raises(BackupError, match="壊れている"):
            validate_backup(bad)
        with pytest.raises(BackupError):
            restore_database(bad, db)
        with closing(sqlite3.connect(tmp_path / "live.db")) as conn, conn:
            assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
            assert conn.execute("SELECT count(*) FROM t").fetchone()[0] == 20000
        assert validate_backup(good) == good
    finally:
        db.dispose()


def test_validation_is_not_repeated_for_the_same_file(tmp_path, monkeypatch):
    db = _make_db(tmp_path / "live.db")
    try:
        good = backup_database(db, tmp_path / "bk", keep=0)
    finally:
        db.dispose()
    monkeypatch.setattr(backup_module, "_validated", set())
    opened = []
    real_connect = sqlite3.connect

    def counting(*args, **kwargs):
        opened.append(args[0])
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(backup_module.sqlite3, "connect", counting)
    validate_backup(good)
    validate_backup(good)
    assert len(opened) == 1
    good.write_bytes(good.read_bytes())  # 変わったら確かめ直す
    os.utime(good, (time.time() + 5, time.time() + 5))
    validate_backup(good)
    assert len(opened) == 2


# ---------------------------------------------------------------------------
# D3-13 / D4-02: 強制終了で残った一時ファイルを消す
# ---------------------------------------------------------------------------
def test_stale_temp_files_are_removed_but_fresh_ones_and_others_are_kept(tmp_path):
    db = _make_db(tmp_path / "live.db")
    folder = tmp_path / "bk"
    folder.mkdir()
    stale = folder / ".live-abcd1234.tmp"
    stale_journal = folder / ".live-abcd1234.tmp-journal"
    fresh = folder / ".live-efgh5678.tmp"  # 別のプロセスが書き込み中かもしれない
    other = folder / ".other-abcd1234.tmp"
    unrelated = folder / ".live-notes.txt"
    for f in (stale, stale_journal, fresh, other, unrelated):
        f.write_bytes(b"x" * 1024)
    for f in (stale, stale_journal, other, unrelated):
        _age(f, 2 * 3600)
    try:
        backup_database(db, folder, keep=3, now=NOW)
    finally:
        db.dispose()
    assert not stale.exists() and not stale_journal.exists()
    assert fresh.exists() and other.exists() and unrelated.exists()


def test_backup_task_setup_cleans_even_when_no_backup_is_due(tmp_path):
    db = _make_db(tmp_path / "live.db")
    folder = tmp_path / "bk"
    try:
        backup_database(db, folder, now=NOW)
        stale = folder / ".live-abcd1234.tmp"
        stale.write_bytes(b"x")
        _age(stale, 2 * 3600)
        task = BackupTask(db, folder, interval_hours=24, clock=lambda: NOW)
        task.setup()
        assert not stale.exists()
        assert len(list_backups(folder, "live")) == 1  # 間隔前なので取らない
    finally:
        db.dispose()


def test_remove_stale_temp_files_handles_missing_folders(tmp_path):
    assert remove_stale_temp_files(tmp_path / "nope", "live") == []


def test_damaged_restore_cleans_its_stale_temp_files(tmp_path):
    db = _make_db(tmp_path / "data" / "live.db")
    try:
        good = backup_database(db, tmp_path / "bk", keep=0)
    finally:
        db.dispose()
    target = tmp_path / "data" / "live.db"
    target.write_bytes(b"garbage" * 1000)
    stale = target.parent / ".live-abcd1234.tmp"
    stale.write_bytes(b"x")
    _age(stale, 2 * 3600)
    db = Database(f"sqlite:///{target.as_posix()}")
    try:
        restore_database(good, db)
    finally:
        db.dispose()
    assert not stale.exists()
    with closing(sqlite3.connect(target)) as conn, conn:
        assert conn.execute("SELECT count(*) FROM t").fetchone()[0] == 3


# ---------------------------------------------------------------------------
# D3-16: 保存先を複数のプロジェクトで共有しても、互いのバックアップを消さない
# ---------------------------------------------------------------------------
def test_projects_sharing_a_backup_folder_keep_their_own_generations(tmp_path, caplog):
    shared = tmp_path / "nas-backups"
    dbs = {
        name: _make_db(tmp_path / name / "instances" / "database.db", table=name)
        for name in ("line1", "line2")
    }
    try:
        with caplog.at_level(logging.WARNING, logger="monitor_app.backup"):
            for day in range(10):
                for i, db in enumerate(dbs.values()):
                    now = NOW + timedelta(days=day, minutes=30 * i)
                    task = BackupTask(
                        db, shared, interval_hours=24, keep=3, clock=lambda n=now: n
                    )
                    task.tick()
        files = list_backups(shared, "database")
        tables = []
        for path in files:
            with closing(sqlite3.connect(path)) as conn, conn:
                tables.append(
                    conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchone()[0]
                )
        assert sorted(tables) == ["line1"] * 3 + ["line2"] * 3
        owners = {name: database_owner(db.sqlite_path) for name, db in dbs.items()}
        assert owners["line1"] != owners["line2"]
        assert all(owners.values())
        for name in dbs:
            mine = list_backups(shared, "database", owner=owners[name])
            assert len(mine) == 3
            assert {backup_owner(p) for p in mine} == {owners[name]}
        warnings = [r for r in caplog.records if "別のデータベース" in r.message]
        assert len(warnings) == 2  # フォルダごと・データベースごとに 1 回
    finally:
        for db in dbs.values():
            db.dispose()


def test_backups_made_before_owner_ids_still_count(tmp_path):
    """番号の無い(以前の版の)バックアップは、従来どおり世代と前回の日時に数える。"""
    db = _make_db(tmp_path / "live.db")
    folder = tmp_path / "bk"
    try:
        legacy = backup_database(db, folder, keep=0, now=NOW)
        with closing(sqlite3.connect(legacy)) as conn, conn:
            conn.execute("PRAGMA application_id = 0")
        assert backup_owner(legacy) == 0
        task = BackupTask(db, folder, interval_hours=24, clock=lambda: NOW)
        assert not task.due()
        for i in range(1, 4):
            backup_database(db, folder, keep=2, now=NOW + timedelta(days=i))
        assert [p.name for p in list_backups(folder, "live")] == [
            "live-20261026-090000.db",
            "live-20261027-090000.db",
        ]
    finally:
        db.dispose()


def test_owner_id_is_written_once_and_survives_restore(tmp_path):
    db = _make_db(tmp_path / "live.db")
    try:
        assert database_owner(db.sqlite_path) == 0
        first = backup_database(db, tmp_path / "bk", keep=0, now=NOW)
        owner = database_owner(db.sqlite_path)
        assert owner and backup_owner(first) == owner
        backup_database(db, tmp_path / "bk", keep=0, now=NOW + timedelta(hours=1))
        assert database_owner(db.sqlite_path) == owner  # 付け直さない
        restore_database(first, db)
        assert database_owner(db.sqlite_path) == owner
        with closing(sqlite3.connect(db.sqlite_path)) as conn, conn:
            assert conn.execute("SELECT count(*) FROM t").fetchone()[0] == 3
    finally:
        db.dispose()


def test_owner_lookup_does_not_create_or_block(tmp_path):
    missing = tmp_path / "missing.db"
    assert database_owner(missing) is None
    assert not missing.exists()
    path = tmp_path / "locked.db"
    db = _make_db(path)
    holder = sqlite3.connect(path, isolation_level=None)
    try:
        holder.execute("PRAGMA journal_mode = DELETE")
        holder.execute("BEGIN EXCLUSIVE")
        started = time.monotonic()
        assert database_owner(path, assign=True) is None
        assert time.monotonic() - started < 2
    finally:
        holder.execute("ROLLBACK")
        holder.close()
        db.dispose()
