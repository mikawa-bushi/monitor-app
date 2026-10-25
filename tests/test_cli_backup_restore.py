"""``monitor-app backup`` / ``restore``(SQLite のオンラインバックアップと復元)。

backup はサーバーの稼働中でもよい(書き込み中の DB からも、確定済みのデータの一貫した
コピーを作る)。restore は ``--yes`` が無ければ内容を示して止まり、置き換える前に今の
DB を退避する。SQLite 以外は pg_dump / mysqldump を案内する。
"""

from __future__ import annotations

import os
import signal
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from contract import helpers
from typer.testing import CliRunner

from monitor_app.cli import app, maintenance

SECRET = "s3cret-pw"
#: 稼働中のサーバーを探す本物の関数(各テストでは差し替える)。
REAL_PROBE = maintenance._server_seems_running


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    helpers.isolate_from_host(monkeypatch, tmp_path)
    # 開発中のサーバーなど、実際に 9990 番で待ち受けているものに左右されない
    monkeypatch.setattr(maintenance, "_server_seems_running", lambda settings: False)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def project(tmp_path, monkeypatch, runner) -> Path:
    proj = helpers.start_project(tmp_path)
    monkeypatch.chdir(proj)
    assert runner.invoke(app, ["import-csv"]).exit_code == 0
    return proj


def _db(proj: Path) -> Path:
    return proj / "instances" / "database.db"


def _query(path: Path, sql: str) -> list[tuple]:
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute(sql).fetchall()


def _backups(folder: Path) -> list[Path]:
    return sorted(folder.glob("database-*.db"))


# ---------------------------------------------------------------------------
# backup
# ---------------------------------------------------------------------------
def test_backup_writes_one_consistent_file_into_backups(project, runner):
    result = runner.invoke(app, ["backup"])
    assert result.exit_code == 0, result.output
    (made,) = _backups(project / "backups")
    assert f"✅ バックアップを作成しました: {made.resolve()}" in result.stdout
    assert "のバックアップ: 1 件(新しい 7 件を残します)" in result.stdout
    assert _query(made, "SELECT COUNT(*) FROM users") == [(5,)]
    assert _query(made, "PRAGMA journal_mode") == [
        ("delete",)
    ]  # 1 ファイルで持ち運べる
    assert _query(made, "PRAGMA integrity_check") == [("ok",)]


def test_backup_while_another_connection_is_writing(project, runner):
    """稼働中のサーバーと同じく WAL で書き込み中の DB から、確定済みの行だけを写す。"""
    db = _db(project)
    with closing(sqlite3.connect(db)) as writer:
        assert writer.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_autocheckpoint = 0")  # 確定した行を -wal に残す
        writer.execute("INSERT INTO users (id, name, email) VALUES (6, 'w', 'w@x')")
        writer.commit()
        writer.execute("BEGIN IMMEDIATE")  # 書き込み中(確定していない行)
        writer.execute("INSERT INTO users (id, name, email) VALUES (7, 'u', 'u@x')")
        result = runner.invoke(app, ["backup", "--dest", "snap"])
        writer.rollback()
    assert result.exit_code == 0, result.output
    (made,) = _backups(project / "snap")
    assert _query(made, "SELECT MAX(id) FROM users") == [(6,)]


def test_backup_options_and_settings(project, runner, monkeypatch):
    for _ in range(3):
        assert (
            runner.invoke(app, ["backup", "--dest", "bk", "--keep", "2"]).exit_code == 0
        )
    assert len(_backups(project / "bk")) == 2  # 古いものから消す
    assert runner.invoke(app, ["backup", "--dest", "bk", "--keep", "0"]).exit_code == 0
    assert len(_backups(project / "bk")) == 3  # 0 はすべて残す

    monkeypatch.setenv("MONITOR_BACKUP_DIR", "nightly")
    monkeypatch.setenv("MONITOR_BACKUP_KEEP", "1")
    for _ in range(2):
        result = runner.invoke(app, ["backup"])
        assert result.exit_code == 0, result.output
    assert len(_backups(project / "nightly")) == 1
    assert "(新しい 1 件を残します)" in result.stdout
    # ほかのファイルには触れない
    (project / "nightly" / "notes.txt").write_text("x", encoding="utf-8")
    assert runner.invoke(app, ["backup"]).exit_code == 0
    assert (project / "nightly" / "notes.txt").exists()


def test_backup_without_a_database_file(project, runner):
    _db(project).unlink()
    result = runner.invoke(app, ["backup"])
    assert result.exit_code == 1
    assert "データベースファイルがありません" in result.stderr
    assert not (project / "backups").exists()


def test_backup_of_a_server_database_points_to_pg_dump(project, runner, monkeypatch):
    monkeypatch.setenv(
        "MONITOR_DATABASE_URL", f"postgresql+psycopg://monitor:{SECRET}@db/monitor"
    )
    result = runner.invoke(app, ["backup"])
    assert result.exit_code == 1
    assert (
        "SQLite のデータベースだけに対応しています(今のデータベース: postgresql)"
        in (result.stderr)
    )
    assert "pg_dump" in result.stderr and "mysqldump" in result.stderr
    assert SECRET not in result.output
    assert len(result.stderr.strip().splitlines()) <= 3


def test_backup_of_an_in_memory_database(project, runner, monkeypatch):
    monkeypatch.setenv("MONITOR_DATABASE_URL", "sqlite://")
    result = runner.invoke(app, ["backup"])
    assert result.exit_code == 1
    assert "メモリ上の SQLite データベースはバックアップできません" in result.stderr


def test_backup_to_an_unwritable_place(project, runner):
    (project / "afile").write_text("x", encoding="utf-8")
    result = runner.invoke(app, ["backup", "--dest", "afile/sub"])
    assert result.exit_code == 1
    assert "バックアップを書き込めません" in result.stderr
    assert "Traceback" not in result.output


# ---------------------------------------------------------------------------
# restore
# ---------------------------------------------------------------------------
@pytest.fixture
def snapshot(project, runner) -> Path:
    """取り込んだ直後のバックアップ(この後に入れた行は含まない)。"""
    assert runner.invoke(app, ["backup", "--dest", "snap"]).exit_code == 0
    (made,) = _backups(project / "snap")
    with closing(sqlite3.connect(_db(project))) as conn:
        conn.execute("INSERT INTO users (id, name, email) VALUES (6, 'new', 'n@x')")
        conn.commit()
    return made


def test_restore_without_yes_explains_and_changes_nothing(project, runner, snapshot):
    result = runner.invoke(app, ["restore", str(snapshot)])
    assert result.exit_code == 1
    assert "の内容に置き換わります(今のデータは失われます)" in result.stdout
    assert "確認のため --yes を付けて実行してください" in result.stderr
    assert "サーバー(runserver)を停止してから実行してください" in result.stderr
    assert _query(_db(project), "SELECT MAX(id) FROM users") == [(6,)]
    assert not (project / "backups").exists()


def test_restore_with_yes_replaces_the_database_and_keeps_a_copy(
    project, runner, snapshot
):
    result = runner.invoke(app, ["restore", str(snapshot), "--yes"])
    assert result.exit_code == 0, result.output
    assert "✅ 復元しました" in result.stdout
    assert _query(_db(project), "SELECT MAX(id) FROM users") == [(5,)]
    # 置き換える前の DB を退避している(間違えて復元しても戻せる)
    (saved,) = _backups(project / "backups")
    assert f"今のデータベースを退避しました: {saved.resolve()}" in result.stdout
    assert _query(saved, "SELECT MAX(id) FROM users") == [(6,)]
    # 復元した DB をそのまま使える
    assert runner.invoke(app, ["import-csv", "--keep"]).exit_code == 0


def test_restore_into_a_missing_database(project, runner, snapshot):
    _db(project).unlink()
    result = runner.invoke(app, ["restore", str(snapshot), "-y"])
    assert result.exit_code == 0, result.output
    assert "退避しました" not in result.stdout
    assert _query(_db(project), "SELECT COUNT(*) FROM users") == [(5,)]


def test_restore_warns_when_a_server_seems_to_be_running(
    project, runner, snapshot, monkeypatch
):
    monkeypatch.setattr(maintenance, "_server_seems_running", lambda settings: True)
    result = runner.invoke(app, ["restore", str(snapshot), "--yes"])
    assert result.exit_code == 0, result.output
    assert "127.0.0.1:9990 で動いているサーバーがあるようです" in result.stderr


@pytest.mark.parametrize(
    ("make", "message"),
    [
        (lambda p: p / "missing.db", "バックアップファイルが見つかりません"),
        (
            lambda p: (p / "bad.db", (p / "bad.db").write_bytes(b"x" * 200))[0],
            "SQLite のデータベースファイルとして読めません",
        ),
    ],
    ids=["missing", "not-sqlite"],
)
def test_restore_rejects_unusable_files_before_asking(project, runner, make, message):
    target = make(project)
    for args in ([], ["--yes"]):
        result = runner.invoke(app, ["restore", str(target), *args])
        assert result.exit_code == 1
        assert message in result.stderr
    assert _query(_db(project), "SELECT COUNT(*) FROM users") == [(5,)]


def test_restore_from_the_database_itself_is_refused(project, runner):
    result = runner.invoke(app, ["restore", str(_db(project)), "--yes"])
    assert result.exit_code == 1
    assert "復元元と復元先が同じファイルです" in result.stderr


def test_restore_outside_a_project_is_refused(tmp_path, runner, snapshot, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    result = runner.invoke(app, ["restore", str(snapshot), "--yes"])
    assert result.exit_code == 1
    assert "config.py が見つかりません" in result.stderr
    assert not (elsewhere / "instances").exists()


def test_restore_of_a_server_database_points_to_the_db_tools(
    project, runner, snapshot, monkeypatch
):
    monkeypatch.setenv("MONITOR_DATABASE_URL", "mysql+pymysql://u:p@h/db")
    result = runner.invoke(app, ["restore", str(snapshot), "--yes"])
    assert result.exit_code == 1
    assert "復元は SQLite のデータベースだけに対応しています" in result.stderr


def test_server_probe_connects_to_the_configured_port():
    import socket

    from monitor_app.settings.runtime import AppSettings

    with socket.create_server(("127.0.0.1", 0)) as sock:
        port = sock.getsockname()[1]
        for host in ("127.0.0.1", "0.0.0.0"):
            settings = AppSettings(_env_file=None, host=host, port=port)
            assert REAL_PROBE(settings) is True
    settings = AppSettings(_env_file=None, host="127.0.0.1", port=port)
    assert REAL_PROBE(settings) is False


def test_backups_made_in_the_same_second_are_never_pruned_right_away(tmp_path):
    """世代の整理で古い連番が消えたあとも、新しいバックアップを消さない。"""
    from datetime import datetime

    from monitor_app.db.backup import backup_database
    from monitor_app.db.engine import Database

    source = tmp_path / "x.db"
    with closing(sqlite3.connect(source)) as conn:
        conn.execute("CREATE TABLE t (a)")
    db = Database(helpers.sqlite_url(source))
    try:
        now = datetime(2026, 10, 21, 10, 0, 0)
        made = [backup_database(db, tmp_path / "bk", keep=1, now=now) for _ in range(4)]
        assert all(path.exists() is (path == made[-1]) for path in made)
        assert [p.name for p in made] == [
            "x-20261021-100000.db",
            "x-20261021-100000-2.db",
            "x-20261021-100000-3.db",
            "x-20261021-100000-4.db",
        ]
        # 時計が戻っても、作ったばかりのものは残す
        earlier = backup_database(
            db, tmp_path / "bk", keep=1, now=datetime(2026, 10, 21, 9, 0, 0)
        )
        assert sorted(tmp_path.joinpath("bk").iterdir()) == [earlier]
    finally:
        db.dispose()


# ---------------------------------------------------------------------------
# 失敗の扱い
# ---------------------------------------------------------------------------
def test_backup_errors_from_the_library_are_short(project, runner, monkeypatch):
    from monitor_app.exceptions import BackupError

    def refuse(*args, **kwargs):
        raise BackupError("バックアップできません(テスト)")

    monkeypatch.setattr("monitor_app.db.backup.backup_database", refuse)
    result = runner.invoke(app, ["backup"])
    assert result.exit_code == 1
    assert result.stderr.splitlines()[0] == "バックアップできません(テスト)"


def test_restore_continues_when_the_current_database_cannot_be_saved(
    project, runner, snapshot, monkeypatch
):
    def broken(*args, **kwargs):
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr("monitor_app.db.backup.backup_database", broken)
    result = runner.invoke(app, ["restore", str(snapshot), "--yes"])
    assert result.exit_code == 0, result.output
    assert "今のデータベースを退避できませんでした" in result.stderr
    assert "database disk image is malformed" in result.stderr
    assert _query(_db(project), "SELECT MAX(id) FROM users") == [(5,)]


def test_restore_errors_from_the_library_are_short(
    project, runner, snapshot, monkeypatch
):
    from monitor_app.exceptions import BackupError

    def refuse(*args, **kwargs):
        raise BackupError("復元できません(テスト)")

    monkeypatch.setattr("monitor_app.db.backup.restore_database", refuse)
    result = runner.invoke(app, ["restore", str(snapshot), "--yes"])
    assert result.exit_code == 1
    assert "復元できません(テスト)" in result.stderr


@pytest.mark.parametrize(
    ("size", "label"),
    [
        (0, "0 B"),
        (1023, "1023 B"),
        (2048, "2.0 KB"),
        (5 * 1024**2, "5.0 MB"),
        (3 * 1024**3, "3.0 GB"),
    ],
)
def test_size_label(size, label):
    assert maintenance._size_label(size) == label


def test_file_uri_urls_are_not_backed_up(project, runner, monkeypatch):
    monkeypatch.setenv(
        "MONITOR_DATABASE_URL", f"sqlite:///file:{_db(project).as_posix()}?uri=true"
    )
    result = runner.invoke(app, ["backup"])
    assert result.exit_code == 1
    assert "file: 形式の MONITOR_DATABASE_URL のバックアップには対応していません" in (
        result.stderr
    )


# ---------------------------------------------------------------------------
# ほかのプロセスが書き込みのロックを持ち続けるとき
# ---------------------------------------------------------------------------
@pytest.fixture
def locked(project, monkeypatch):
    """rollback ジャーナルの DB に、ほかの接続が排他ロックを持っている状態。"""
    monkeypatch.setenv("MONITOR_SQLITE_JOURNAL_MODE", "delete")
    monkeypatch.setenv("MONITOR_SQLITE_BUSY_TIMEOUT_MS", "100")  # 待ちは最短の 1 秒
    holder = sqlite3.connect(_db(project), isolation_level=None)
    holder.execute("PRAGMA journal_mode = DELETE")
    holder.execute("BEGIN EXCLUSIVE")
    yield project
    holder.execute("ROLLBACK")
    holder.close()


def test_backup_gives_up_when_the_database_stays_locked(locked, runner):
    import time

    started = time.monotonic()
    result = runner.invoke(app, ["backup"])
    assert result.exit_code == 1
    assert time.monotonic() - started < 10
    assert "ロックされているため、バックアップできませんでした" in result.stderr
    assert "少し待ってから" in result.stderr
    assert list((locked / "backups").iterdir()) == []  # 書きかけのファイルも残さない


def test_restore_gives_up_when_the_database_stays_locked(locked, runner, tmp_path):
    other = tmp_path / "other.db"
    with closing(sqlite3.connect(other)) as conn:
        conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY)")
    result = runner.invoke(app, ["restore", str(other), "--yes"])
    assert result.exit_code == 1
    assert "ロックされているため、復元できませんでした" in result.stderr
    assert "サーバー(runserver)を停止してから" in result.stderr


def _default_sigint() -> None:
    signal.signal(signal.SIGINT, signal.SIG_DFL)


@pytest.mark.skipif(os.name == "nt", reason="POSIX のシグナルで確かめる")
@pytest.mark.parametrize(
    ("signum", "code"),
    [(signal.SIGINT, 130), (signal.SIGTERM, 143)],
    ids=["int", "term"],
)
def test_a_waiting_backup_stops_on_ctrl_c_and_sigterm(project, signum, code):
    """ロック待ちの間も Ctrl+C・docker stop で止まり、書きかけのファイルを消す。"""
    import subprocess
    import sys
    import time

    holder = sqlite3.connect(_db(project), isolation_level=None)
    try:
        holder.execute("PRAGMA journal_mode = DELETE")
        holder.execute("BEGIN EXCLUSIVE")
        env = helpers.child_env(
            MONITOR_SQLITE_JOURNAL_MODE="delete", MONITOR_SQLITE_BUSY_TIMEOUT_MS="60000"
        )
        proc = subprocess.Popen(
            [sys.executable, "-m", "monitor_app", "backup"],
            cwd=project,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            # バックグラウンドのジョブ(cmd &・nohup)から pytest を起動すると SIGINT が
            # 無視された状態で引き継がれ、子の Python が Ctrl+C を受けなくなる。端末から
            # 起動したときと同じ状態にする。
            preexec_fn=_default_sigint,
        )
        try:
            deadline = time.monotonic() + 30
            while not list(project.glob("backups/.database-*.tmp")):  # 写している途中
                assert proc.poll() is None, proc.communicate()
                assert time.monotonic() < deadline
                time.sleep(0.05)
            time.sleep(0.3)
            proc.send_signal(signum)
            proc.communicate(timeout=10)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        assert proc.returncode == code
        assert list((project / "backups").iterdir()) == []
    finally:
        holder.execute("ROLLBACK")
        holder.close()
