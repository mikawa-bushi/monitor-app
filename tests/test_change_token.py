"""変更トークン(db/change_token.py)と行取り出しゲート(db/gate.py)のテスト。"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import closing, nullcontext

import pytest
from contract import helpers
from sqlalchemy import text

from monitor_app.db import change_token as change_token_mod
from monitor_app.db.change_token import SqliteDataVersion, change_token_for
from monitor_app.db.engine import Database
from monitor_app.db.gate import fetch_all, fetch_gate


def _file_db(
    tmp_path, name: str = "app.db", journal_mode: str | None = None
) -> Database:
    if journal_mode:  # WAL はファイルに記録される(以後のコネクションも WAL)
        with closing(sqlite3.connect(tmp_path / name)) as conn:
            conn.execute(f"PRAGMA journal_mode={journal_mode}")
    db = Database(helpers.sqlite_url(tmp_path / name))
    with db.connect() as conn:
        conn.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)"))
    return db


# ---------------------------------------------------------------------------
# change_token_for: どの DB でトークンが使えるか
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("url", ["sqlite://", "sqlite:///:memory:"])
def test_no_token_for_in_memory_sqlite(url):
    assert change_token_for(Database(url)) is None


def test_no_token_for_server_databases():
    # エンジンの生成だけで接続はしない
    db = Database("postgresql+pg8000://user:secret@127.0.0.1:1/none")
    assert change_token_for(db) is None


def test_relative_sqlite_path_is_resolved_like_sqlalchemy(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    token = change_token_for(Database("sqlite:///instances/app.db"))
    assert isinstance(token, SqliteDataVersion)
    assert token.filename == str(tmp_path / "instances" / "app.db")


@pytest.mark.filterwarnings("ignore::sqlalchemy.exc.SADeprecationWarning")
def test_uri_sqlite_urls(tmp_path):
    path = tmp_path / "u.db"
    token = change_token_for(Database(f"sqlite:///file:{path}?uri=true"))
    assert isinstance(token, SqliteDataVersion)
    memory = Database("sqlite:///file:shared?mode=memory&cache=shared&uri=true")
    assert change_token_for(memory) is None


# ---------------------------------------------------------------------------
# PRAGMA data_version の振る舞い
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("journal_mode", ["delete", "wal"])
def test_token_changes_on_commits_from_other_connections(tmp_path, journal_mode):
    db = _file_db(tmp_path, journal_mode=journal_mode)
    token = change_token_for(db)
    try:
        first = token.current()
        assert isinstance(first, int)
        assert token.current() == first  # 読むだけでは変わらない
        with db.readonly() as conn:
            conn.execute(text("SELECT * FROM t")).fetchall()
        assert token.current() == first

        with db.connect() as conn:  # 同じプロセスのプール(別コネクション)
            conn.execute(text("INSERT INTO t (v) VALUES ('a')"))
        second = token.current()
        assert second != first

        raw = sqlite3.connect(tmp_path / "app.db")  # 外部のツール
        with closing(raw):
            raw.execute("INSERT INTO t (v) VALUES ('b')")
            raw.commit()
        assert token.current() not in (first, second)
    finally:
        token.close()
        db.dispose()


@pytest.mark.parametrize("journal_mode", ["delete", "wal"])
def test_token_detects_commits_from_another_process(tmp_path, journal_mode):
    db = _file_db(tmp_path, journal_mode=journal_mode)
    token = change_token_for(db)
    try:
        before = token.current()
        code = (
            "import sqlite3, sys; c = sqlite3.connect(sys.argv[1]);"
            "c.execute(\"INSERT INTO t (v) VALUES ('cron')\"); c.commit()"
        )
        subprocess.run(
            [sys.executable, "-c", code, str(tmp_path / "app.db")], check=True
        )
        assert token.current() != before
    finally:
        token.close()
        db.dispose()


def test_token_connection_never_writes(tmp_path):
    db = _file_db(tmp_path)
    token = change_token_for(db)
    try:
        token.current()
        with pytest.raises(sqlite3.OperationalError):
            token._conn.execute("INSERT INTO t (v) VALUES ('x')")
    finally:
        token.close()
        db.dispose()


def test_missing_file_is_not_created_and_is_retried_later(tmp_path, monkeypatch):
    path = tmp_path / "later.db"
    token = SqliteDataVersion(str(path))
    assert token.current() is None
    assert not path.exists()  # トークンのせいで空の DB ファイルを作らない
    assert token.current() is None  # 再試行の間隔内は開き直さない

    with closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE t (id INTEGER)")
    monkeypatch.setattr(change_token_mod, "_RETRY_INTERVAL", 0.0)
    token._next_open = 0.0
    try:
        assert isinstance(token.current(), int)
    finally:
        token.close()


def test_busy_database_returns_none_and_keeps_the_connection(tmp_path):
    path = tmp_path / "busy.db"
    with closing(sqlite3.connect(path)) as conn:  # ロールバックジャーナル(既定)
        conn.execute("CREATE TABLE t (id INTEGER)")
        conn.commit()
    token = SqliteDataVersion(str(path))
    try:
        assert isinstance(token.current(), int)
        writer = sqlite3.connect(path, isolation_level=None)
        with closing(writer):
            writer.execute("BEGIN EXCLUSIVE")
            started = time.monotonic()
            assert token.current() is None  # 書き込み中は待たずに「不明」
            assert time.monotonic() - started < 2.0
            assert token._conn is not None  # 一時的なエラーではコネクションを捨てない
            writer.execute("COMMIT")
        assert isinstance(token.current(), int)
    finally:
        token.close()


def test_busy_database_does_not_queue_concurrent_callers(tmp_path):
    """書き込みのロック中でも、トークンの参照が順番待ちにならない(キャッシュの
    ヒットまで遅らせない)。ロックを待つと 20 スレッドで最後の 1 件が約 2 秒待った。"""
    path = tmp_path / "busy.db"
    with closing(sqlite3.connect(path)) as conn:  # ロールバックジャーナル(既定)
        conn.execute("CREATE TABLE t (id INTEGER)")
        conn.commit()
    token = SqliteDataVersion(str(path))
    token.current()
    writer = sqlite3.connect(path, isolation_level=None)
    barrier = threading.Barrier(20)
    latencies: list[float] = []
    lock = threading.Lock()

    def call() -> None:
        barrier.wait()
        started = time.monotonic()
        assert token.current() is None
        with lock:
            latencies.append(time.monotonic() - started)

    try:
        with closing(writer):
            writer.execute("BEGIN EXCLUSIVE")
            threads = [threading.Thread(target=call) for _ in range(20)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(10)
            writer.execute("COMMIT")
        assert len(latencies) == 20
        assert max(latencies) < 0.5, latencies
    finally:
        token.close()


def test_close_and_reopen(tmp_path):
    db = _file_db(tmp_path)
    token = change_token_for(db)
    try:
        value = token.current()
        token.close()
        assert token._conn is None
        assert token.current() == value  # 次の呼び出しで開き直す
    finally:
        token.close()
        db.dispose()


def test_services_close_releases_the_token_connection(tmp_path):
    from monitor_app import MonitorConfig, TableDef, ViewDef
    from monitor_app.container import build_services
    from monitor_app.settings.runtime import AppSettings

    config = MonitorConfig(
        tables={"t": TableDef(columns={"id": "int", "v": "str"})},
        views={"v": ViewDef(query="SELECT * FROM t")},
    )
    services = build_services(
        config,
        AppSettings(_env_file=None, database_url=helpers.sqlite_url(tmp_path / "s.db")),
    )
    token = services.view_cache._change_token
    assert isinstance(token, SqliteDataVersion)
    services.view_service.get_view("v")
    assert token._conn is not None
    services.close()
    assert token._conn is None


# ---------------------------------------------------------------------------
# 行取り出しのゲート
# ---------------------------------------------------------------------------
def test_gate_is_a_reentrant_lock_per_sqlite_database(tmp_path):
    db = _file_db(tmp_path)
    other = _file_db(tmp_path, "other.db")
    try:
        gate = fetch_gate(db)
        assert fetch_gate(db) is gate
        assert fetch_gate(other) is not gate
        with gate, fetch_gate(db):  # 同じスレッドなら入れ子にできる(デッドロックしない)
            pass
    finally:
        db.dispose()
        other.dispose()


def test_gate_is_a_noop_for_server_databases():
    db = Database("postgresql+pg8000://user:secret@127.0.0.1:1/none")
    assert isinstance(fetch_gate(db), nullcontext)


def test_fetch_all_reads_every_row_in_chunks(tmp_path):
    db = _file_db(tmp_path)
    try:
        with db.connect() as conn:
            conn.execute(
                text("INSERT INTO t (v) VALUES (:v)"),
                [{"v": str(i)} for i in range(10)],
            )
        for chunk in (1, 3, 10, 11):
            with db.readonly() as conn:
                result = conn.execute(text("SELECT v FROM t ORDER BY id"))
                rows = fetch_all(result, db, chunk=chunk)
                assert result.closed  # 読み切ったカーソルは閉じる
            assert [r[0] for r in rows] == [str(i) for i in range(10)]
    finally:
        db.dispose()


def test_fetch_all_waits_while_another_thread_holds_the_gate(tmp_path):
    db = _file_db(tmp_path)
    try:
        with db.connect() as conn:
            conn.execute(text("INSERT INTO t (v) VALUES ('x')"))
        done = threading.Event()

        def reader():
            with db.readonly() as conn:
                fetch_all(conn.execute(text("SELECT * FROM t")), db)
            done.set()

        with fetch_gate(db):
            t = threading.Thread(target=reader)
            t.start()
            assert not done.wait(0.2)  # ゲートが空くまで取り出せない
        assert done.wait(5)
        t.join(5)
    finally:
        db.dispose()


def test_uri_token_reads_an_existing_file(tmp_path):
    path = tmp_path / "uri.db"
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE t (id INTEGER)")
    token = SqliteDataVersion(f"file:{path}?cache=private", uri=True)
    try:
        assert isinstance(token.current(), int)
    finally:
        token.close()


def test_broken_connection_is_dropped_and_reopened_later(tmp_path, monkeypatch):
    db = _file_db(tmp_path)
    token = change_token_for(db)

    class _Broken:
        closed = False

        def execute(self, _sql):
            raise sqlite3.DatabaseError("database disk image is malformed")

        def close(self):
            _Broken.closed = True

    try:
        assert isinstance(token.current(), int)
        token._conn.close()
        token._conn = _Broken()
        assert token.current() is None
        assert _Broken.closed and token._conn is None
        assert token.current() is None  # 少し待ってから開き直す
        monkeypatch.setattr(change_token_mod, "_RETRY_INTERVAL", 0.0)
        token._next_open = 0.0
        assert isinstance(token.current(), int)
    finally:
        token.close()
        db.dispose()


@pytest.mark.parametrize(
    "message, transient",
    [("database is locked", True), ("database table is busy", True), ("boom", False)],
)
def test_transient_errors_are_recognised_without_error_codes(message, transient):
    assert (
        change_token_mod._is_transient(sqlite3.OperationalError(message)) is transient
    )


def test_unparseable_urls_get_no_token(monkeypatch, tmp_path):
    db = _file_db(tmp_path)
    try:

        def boom(_url):
            raise ValueError("想定外の URL")

        monkeypatch.setattr(db.engine.dialect, "create_connect_args", boom)
        assert change_token_for(db) is None
    finally:
        db.dispose()


def test_fetch_all_without_a_gate_uses_fetchall():
    db = Database("postgresql+pg8000://user:secret@127.0.0.1:1/none")

    class _Result:
        def fetchall(self):
            return [(1,), (2,)]

    assert fetch_all(_Result(), db) == [(1,), (2,)]
