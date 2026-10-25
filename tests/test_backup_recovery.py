"""バックアップからの復旧で起きる、失敗しやすい場面(壊れた DB・空のファイル・``~``)。

- 復元のいちばんの用途は、停電などで壊れて開けなくなった DB を戻すこと。壊れた
  ファイルにはバックアップ API で書き込めないため、新しいファイルに写して置き換える。
  壊れたファイルは(-wal も)そのまま退避し、古い -wal が新しいファイルに書き戻されない
  ようにする。
- 0 バイトのファイルは SQLite では空のデータベースとして開けてしまうため、それで復元
  するとデータがすべて消える。テーブルの無いファイルは復元に使わない。
- ``MONITOR_BACKUP_DIR=~/backups`` のときも、定期バックアップは前回のバックアップを
  見つけられる(見つけられないと 10 分ごとにバックアップし、古い世代がすぐ消える)。
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from contract import helpers
from typer.testing import CliRunner

from monitor_app.cli import app, maintenance
from monitor_app.db.backup import (
    is_corrupt_error,
    is_damaged,
    latest_backup_time,
    list_backups,
    restore_database,
    save_damaged_copy,
    validate_backup,
)
from monitor_app.db.engine import Database
from monitor_app.exceptions import BackupError
from monitor_app.services.backup_task import BackupTask


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    helpers.isolate_from_host(monkeypatch, tmp_path)
    monkeypatch.setattr(maintenance, "_server_seems_running", lambda settings: False)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def project(tmp_path, runner, monkeypatch) -> Path:
    proj = helpers.start_project(tmp_path)
    monkeypatch.chdir(proj)
    assert runner.invoke(app, ["import-csv"]).exit_code == 0
    return proj


def _db(proj: Path) -> Path:
    return proj / "instances" / "database.db"


def _query(path: Path, sql: str) -> list[tuple]:
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute(sql).fetchall()


def _damage(path: Path) -> None:
    """SQLite のヘッダを壊す(停電で書きかけになったファイルなど)。"""
    data = bytearray(path.read_bytes())
    data[:16] = b"not sqlite here!"
    path.write_bytes(bytes(data))


def _make_db(path: Path, ids=(1, 2, 3)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in ids])
        conn.commit()
    return path


# ---------------------------------------------------------------------------
# 壊れた DB の判定
# ---------------------------------------------------------------------------
def test_damage_is_told_apart_from_a_lock(tmp_path):
    good = _make_db(tmp_path / "good.db")
    assert is_damaged(good) is False
    bad = _make_db(tmp_path / "bad.db")
    _damage(bad)
    assert is_damaged(bad) is True
    with (
        closing(sqlite3.connect(bad)) as conn,
        pytest.raises(sqlite3.DatabaseError) as info,
    ):
        conn.execute("SELECT * FROM sqlite_master").fetchall()
    assert is_corrupt_error(info.value)
    assert not is_corrupt_error(sqlite3.OperationalError("database is locked"))
    assert not is_corrupt_error(OSError("disk full"))
    # ほかの接続が書き込みのロックを持っていても、壊れているとはみなさない(待たない)
    holder = sqlite3.connect(good, isolation_level=None)
    try:
        holder.execute("BEGIN EXCLUSIVE")
        assert is_damaged(good) is False
    finally:
        holder.execute("ROLLBACK")
        holder.close()


@pytest.mark.parametrize("folder", ["工場 データ", "a#b", "p%20q", "q?r"])
def test_damage_check_with_special_characters_in_the_path(
    tmp_path, folder, monkeypatch
):
    """読み取り専用の確認は URI で開くため、記号や日本語を含むパスでも正しく判定する。"""
    db = _make_db(tmp_path / folder / "data base.db")
    assert is_damaged(db) is False
    _damage(db)
    assert is_damaged(db) is True
    monkeypatch.chdir(tmp_path)
    assert is_damaged(Path(folder) / "data base.db") is True  # 相対パスでも


def test_the_read_only_check_leaves_the_files_alone(tmp_path):
    """判定で -wal を本体へ書き戻したり消したりしない(退避の前に確かめるため)。"""
    db = tmp_path / "wal.db"
    writer = sqlite3.connect(db)
    try:
        writer.execute("PRAGMA journal_mode = WAL")
        writer.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        writer.execute("INSERT INTO t VALUES (1)")
        writer.commit()
        wal = db.with_name(db.name + "-wal")
        before = (db.read_bytes(), wal.read_bytes())
        assert is_damaged(db) is False
        assert (db.read_bytes(), wal.read_bytes()) == before
    finally:
        writer.close()


def test_damaged_copy_keeps_the_wal_and_is_not_a_generation(tmp_path):
    db = _make_db(tmp_path / "data" / "database.db")
    _damage(db)
    db.with_name("database.db-wal").write_bytes(b"recent pages")
    now = datetime(2026, 10, 21, 9, 0, 0)
    first = save_damaged_copy(db, tmp_path / "bk", now=now)
    second = save_damaged_copy(db, tmp_path / "bk", now=now)
    assert first.name == "database-damaged-20261021-090000.db"
    assert second.name == "database-damaged-20261021-090000-2.db"
    assert first.read_bytes() == db.read_bytes()
    assert first.with_name(first.name + "-wal").read_bytes() == b"recent pages"
    # バックアップの世代として数えない(世代の整理で消さない・最新とみなさない)
    assert list_backups(tmp_path / "bk", "database") == []


# ---------------------------------------------------------------------------
# 復元(ライブラリ)
# ---------------------------------------------------------------------------
def _damaged_with_a_valid_wal(target: Path) -> None:
    """ヘッダの壊れた DB と、その DB の正しい -wal(ヘッダのページは含まない)。

    SQLite は -wal があれば(本体のヘッダの journal_mode に関係なく)開くときに読むため、
    置き換えた新しいファイルの横に残すと、古い DB のページが書き戻される。
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    source = target.with_name("old.db")
    conn = sqlite3.connect(source)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        conn.executemany("INSERT INTO t VALUES (?)", [(7,), (8,)])
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("INSERT INTO t VALUES (9)")  # 表のページだけが -wal に入る
        conn.commit()
        shutil.copy2(source, target)
        shutil.copy2(f"{source}-wal", f"{target}-wal")
    finally:
        conn.close()
    _damage(target)


def test_restore_replaces_a_damaged_database(tmp_path):
    backup = _make_db(tmp_path / "backup.db", ids=(1, 2, 3))
    target = tmp_path / "data" / "database.db"
    _damaged_with_a_valid_wal(target)
    os.chmod(target, 0o640)
    target.with_name("database.db-shm").write_bytes(b"y" * 100)
    assert is_damaged(target)
    db = Database(helpers.sqlite_url(target))
    try:
        restore_database(backup, db)
        assert _query(target, "SELECT id FROM t ORDER BY id") == [(1,), (2,), (3,)]
        assert _query(target, "PRAGMA integrity_check") == [("ok",)]
        assert not target.with_name("database.db-wal").exists()
        assert not target.with_name("database.db-shm").exists()
        assert list(target.parent.glob(".*.tmp")) == []
        if os.name != "nt":
            assert target.stat().st_mode & 0o777 == 0o640  # 元の権限を引き継ぐ
        # 同じ Database でそのまま使い続けられる(WAL への切り替えも通常どおり)
        with db.connect() as conn:
            assert conn.exec_driver_sql("SELECT COUNT(*) FROM t").scalar() == 3
    finally:
        db.dispose()


def _sqlite_without_tables(path: Path) -> None:
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("PRAGMA user_version = 1")  # ヘッダだけの正しい SQLite のファイル
        conn.commit()


@pytest.mark.parametrize(
    "make",
    [lambda p: p.write_bytes(b""), _sqlite_without_tables],
    ids=["zero-bytes", "sqlite-without-tables"],
)
def test_files_without_tables_are_not_used_for_a_restore(tmp_path, make):
    empty = tmp_path / "empty.db"
    make(empty)
    with pytest.raises(BackupError, match="テーブルが 1 つも無いため"):
        validate_backup(empty)
    target = _make_db(tmp_path / "data" / "database.db")
    db = Database(helpers.sqlite_url(target))
    try:
        with pytest.raises(BackupError, match="テーブルが 1 つも無いため"):
            restore_database(empty, db)
    finally:
        db.dispose()
    assert _query(target, "SELECT COUNT(*) FROM t") == [(3,)]  # 何も変えない


# ---------------------------------------------------------------------------
# ~ を使った保存先(定期バックアップ)
# ---------------------------------------------------------------------------
def test_backup_task_finds_backups_in_a_folder_under_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))  # Windows の ~
    db = Database(helpers.sqlite_url(_make_db(tmp_path / "data" / "app.db")))
    now = datetime(2026, 10, 21, 8, 0, 0)
    clock = [now]
    task = BackupTask(
        db, Path("~/backups"), interval_hours=24, keep=7, clock=lambda: clock[0]
    )
    try:
        assert task.due() is True
        task.tick()
        (made,) = list((home / "backups").iterdir())
        assert list_backups("~/backups", "app") == [made]
        assert latest_backup_time("~/backups", "app") == now
        # 直後の確認では取らない(以前は ~ を解釈せず、確認のたびに取り直していた)
        clock[0] = now + timedelta(minutes=10)
        assert task.due() is False
        clock[0] = now + timedelta(hours=24)
        assert task.due() is True
    finally:
        db.dispose()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
@pytest.fixture
def snapshot(project, runner) -> Path:
    assert runner.invoke(app, ["backup", "--dest", "snap"]).exit_code == 0
    (made,) = sorted((project / "snap").glob("database-*.db"))
    return made


def test_restore_command_recovers_a_damaged_database(project, runner, snapshot):
    db = _db(project)
    _damage(db)
    result = runner.invoke(app, ["restore", str(snapshot), "--yes"])
    assert result.exit_code == 0, result.output
    assert "Traceback" not in result.output
    assert "今のデータベースは壊れていて開けません" in result.stderr
    (raw,) = (project / "backups").glob("database-damaged-*.db")
    assert f"ファイルをそのまま退避しました: {raw.resolve()}" in result.stderr
    assert raw.read_bytes()[:16] == b"not sqlite here!"
    assert "✅ 復元しました" in result.stdout
    assert _query(db, "SELECT COUNT(*) FROM users") == [(5,)]
    assert _query(db, "PRAGMA integrity_check") == [("ok",)]
    # 復元した DB をそのまま使える
    assert runner.invoke(app, ["import-csv", "--keep"]).exit_code == 0


def test_restore_command_refuses_an_empty_file(project, runner):
    empty = project / "empty.db"
    empty.write_bytes(b"")
    result = runner.invoke(app, ["restore", str(empty), "--yes"])
    assert result.exit_code == 1
    assert "テーブルが 1 つも無いため、復元に使えません" in result.stderr
    assert _query(_db(project), "SELECT COUNT(*) FROM users") == [(5,)]
    assert not (project / "backups").exists()  # 退避もしない(何も変えない)


def test_backup_command_explains_a_damaged_database(project, runner):
    _damage(_db(project))
    result = runner.invoke(app, ["backup"])
    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "データベースを読めないため、バックアップできません" in result.stderr
    assert "monitor-app restore" in result.stderr
    assert list((project / "backups").glob("*")) == []  # 書きかけのファイルも残さない


def test_a_replace_that_fails_is_reported_briefly(
    project, runner, snapshot, monkeypatch
):
    """壊れた DB を置き換えられない(Windows でほかのプロセスが開いている、など)。"""
    db = _db(project)
    _damage(db)

    def busy(src, dst):
        raise PermissionError(13, "別のプロセスが使用中です")

    monkeypatch.setattr("monitor_app.db.backup.os.replace", busy)
    result = runner.invoke(app, ["restore", str(snapshot), "--yes"])
    assert result.exit_code == 1
    assert "壊れたデータベースのファイルを置き換えられませんでした" in result.stderr
    assert "サーバー(runserver)を停止してから" in result.stderr
    assert db.read_bytes()[:16] == b"not sqlite here!"  # 置き換えていない
    assert list(db.parent.glob(".*.tmp")) == []  # 一時ファイルも残さない
    (raw,) = (project / "backups").glob("database-damaged-*.db")  # 退避はしてある
    assert raw.read_bytes()[:16] == b"not sqlite here!"


def test_restore_goes_on_when_the_damaged_copy_cannot_be_saved(
    project, runner, snapshot, monkeypatch
):
    _damage(_db(project))

    def refuse(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("monitor_app.db.backup.save_damaged_copy", refuse)
    result = runner.invoke(app, ["restore", str(snapshot), "--yes"])
    assert result.exit_code == 0, result.output
    assert "退避できなかったため、そのまま復元します" in result.stderr
    assert _query(_db(project), "SELECT COUNT(*) FROM users") == [(5,)]
