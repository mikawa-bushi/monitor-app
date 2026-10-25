"""SQLite の権限・ロックの問題に、的外れでない案内を出すこと。

読み取り専用の DB ファイル・書き込めないフォルダに「接続先(ホスト・ポート・ユーザー・
パスワード)を確認」と案内しない。どのファイル・フォルダに書き込めないのかを示す。
ほかのプロセスがロック中のときに「ネットワーク共有なら journal_mode=delete」とも言わない。
"""

from __future__ import annotations

import logging
import os
import sqlite3
import stat
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from typer.testing import CliRunner

from monitor_app.cli import app
from monitor_app.cli._common import _db_hint, explain_error
from monitor_app.db.engine import Database

posix_non_root = pytest.mark.skipif(
    os.name == "nt" or os.geteuid() == 0,
    reason="POSIX の権限(root は権限に関係なく書き込める)",
)
SERVER_HINT = "ホスト・ポート・ユーザー・パスワード"


@pytest.fixture
def project(tmp_path, monkeypatch) -> Path:
    runner = CliRunner()
    assert runner.invoke(app, ["startproject", str(tmp_path / "proj")]).exit_code == 0
    monkeypatch.chdir(tmp_path / "proj")
    monkeypatch.setattr(
        "monitor_app.logging_conf.configure_logging", lambda *a, **k: None
    )
    return tmp_path / "proj"


@pytest.fixture
def restore_modes():
    changed: list[tuple[Path, int]] = []

    def chmod(path: Path, mode: int) -> None:
        changed.append((path, stat.S_IMODE(path.stat().st_mode)))
        path.chmod(mode)

    yield chmod
    for path, mode in reversed(changed):
        path.chmod(mode)


@posix_non_root
def test_a_read_only_database_file_is_named_in_the_hint(project, restore_modes):
    runner = CliRunner()
    assert runner.invoke(app, ["import-csv"]).exit_code == 0
    db_file = project / "instances" / "database.db"
    for leftover in ("-wal", "-shm"):
        Path(f"{db_file}{leftover}").unlink(missing_ok=True)
    restore_modes(db_file, 0o444)
    result = runner.invoke(app, ["import-csv"])
    assert result.exit_code == 1
    assert "readonly database" in result.output
    assert f"データベースファイル {db_file.resolve()} に書き込めません" in result.output
    assert "Docker" in result.output
    assert SERVER_HINT not in result.output


@posix_non_root
def test_a_read_only_folder_without_a_database_is_named_in_the_hint(
    project, restore_modes
):
    folder = project / "instances"
    folder.mkdir(exist_ok=True)
    restore_modes(folder, 0o555)
    result = CliRunner().invoke(app, ["import-csv"])
    assert result.exit_code == 1
    assert "unable to open database file" in result.output
    assert f"保存先フォルダ {folder.resolve()} に書き込めません" in result.output
    assert "フォルダ(既定: instances/)があるか" not in result.output
    assert SERVER_HINT not in result.output


def test_a_locked_database_is_not_blamed_on_network_shares(tmp_path, caplog):
    path = tmp_path / "locked.db"
    holder = sqlite3.connect(path, isolation_level=None)
    holder.execute("PRAGMA journal_mode = delete")
    holder.execute("CREATE TABLE t (id INTEGER)")
    holder.execute("BEGIN EXCLUSIVE")  # 別のプロセス(書き込み中のサーバーなど)の代わり
    db = Database(f"sqlite:///{path}", sqlite_busy_timeout_ms=50)
    try:
        with caplog.at_level(logging.WARNING, logger="monitor_app.db"):
            logging.getLogger("monitor_app").propagate = True
            with pytest.raises(OperationalError), db.engine.connect() as conn:
                conn.execute(text("SELECT * FROM t"))
        warning = " ".join(r.getMessage() for r in caplog.records)
        assert "WAL モードにできませんでした" in warning
        assert "ほかのプロセス" in warning
        assert "MONITOR_SQLITE_JOURNAL_MODE=delete" not in warning
    finally:
        holder.rollback()
        holder.close()
        db.dispose()


def test_server_database_errors_keep_the_connection_hint():
    class _DriverError(Exception):
        pass

    _DriverError.__module__ = "psycopg"
    exc = OperationalError("SELECT 1", {}, _DriverError("connection refused"))
    explained = explain_error(exc)
    assert explained is not None
    assert SERVER_HINT in (explained[1] or "")


def test_hints_without_a_known_path_still_mention_permissions():
    hint = _db_hint("attempt to write a readonly database")
    assert "書き込めるか" in hint and SERVER_HINT not in hint
    hint = _db_hint("unable to open database file")
    assert "instances/" in hint and "権限" in hint
    hint = _db_hint("database is locked")
    assert "少し待ってから" in hint
    assert SERVER_HINT in _db_hint("could not connect to server", sqlite=False)
