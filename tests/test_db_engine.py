"""Database(エンジン)のテスト: SQLite のチューニング(PERF-02)、保存先フォルダの
作成と説明(DX-02)、サーバー DB のプール設定と起動時の待機(CRIT-01)。"""

from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError

import monitor_app.db.engine as engine_mod
from monitor_app.db.engine import Database, describe_url, is_network_path
from monitor_app.settings.runtime import AppSettings


@pytest.fixture(autouse=True)
def _propagate_monitor_logs(monkeypatch):
    """CLI のテストが configure_logging で monitor_app を propagate=False にしていても、
    caplog(root のハンドラ)で monitor_app.* のログを拾えるようにする。"""
    monkeypatch.setattr(logging.getLogger("monitor_app"), "propagate", True)


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _pragmas(db: Database) -> tuple:
    with db.connect() as conn:
        return tuple(
            conn.execute(text(f"PRAGMA {name}")).scalar()
            for name in ("journal_mode", "synchronous", "busy_timeout")
        )


# ---------------------------------------------------------------------------
# SQLite の PRAGMA
# ---------------------------------------------------------------------------
def test_file_sqlite_defaults_to_wal_normal_and_busy_timeout(tmp_path):
    db = Database(_url(tmp_path / "app.db"))
    try:
        assert _pragmas(db) == ("wal", 1, 5000)  # synchronous: 1 = NORMAL
        assert db.journal_mode == "wal"
    finally:
        db.dispose()


def test_pragmas_apply_to_every_new_connection(tmp_path):
    db = Database(_url(tmp_path / "app.db"), sqlite_busy_timeout_ms=1234)
    try:
        with db.engine.connect() as a, db.engine.connect() as b:  # 2 本目は新規接続
            for conn in (a, b):
                assert conn.execute(text("PRAGMA busy_timeout")).scalar() == 1234
                assert conn.execute(text("PRAGMA synchronous")).scalar() == 1
    finally:
        db.dispose()


def test_explicit_journal_and_synchronous_modes(tmp_path):
    db = Database(
        _url(tmp_path / "app.db"),
        sqlite_journal_mode="DELETE",
        sqlite_synchronous="extra",
    )
    try:
        assert _pragmas(db)[:2] == ("delete", 3)
    finally:
        db.dispose()


def test_normal_synchronous_is_only_used_with_wal(tmp_path):
    """rollback ジャーナルで normal は電源断で壊れ得るため SQLite 既定の full にする。"""
    db = Database(_url(tmp_path / "app.db"), sqlite_journal_mode="delete")
    try:
        assert _pragmas(db)[:2] == ("delete", 2)  # 2 = FULL
    finally:
        db.dispose()


def test_wal_survives_reopening_and_is_shared_with_other_processes(tmp_path):
    path = tmp_path / "app.db"
    db = Database(_url(path))
    with db.connect() as conn:
        conn.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY)"))
    db.dispose()
    # 別の接続(PRAGMA を設定しない)からも WAL
    with closing(sqlite3.connect(path)) as raw:
        assert raw.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_in_memory_databases_are_left_alone():
    for url in (
        "sqlite://",
        "sqlite:///:memory:",
        "sqlite:///",
        "sqlite:///file:memdb?mode=memory&cache=shared&uri=true",
    ):
        db = Database(url)
        try:
            assert db.is_memory
            assert not event.contains(db.engine, "connect", db._on_sqlite_connect)
            with db.connect() as conn:
                assert conn.execute(text("PRAGMA journal_mode")).scalar() == "memory"
                assert conn.execute(text("PRAGMA synchronous")).scalar() == 2
        finally:
            db.dispose()


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"sqlite_journal_mode": "bogus"}, "sqlite_journal_mode"),
        ({"sqlite_synchronous": "always"}, "sqlite_synchronous"),
    ],
)
def test_invalid_pragma_values_are_rejected(tmp_path, kwargs, match):
    with pytest.raises(ValueError, match=match):
        Database(_url(tmp_path / "app.db"), **kwargs)


class _FakeCursor:
    def __init__(self, journal_result):
        self.journal_result = journal_result
        self.sql: list[str] = []
        self._last = None

    def execute(self, sql):
        self.sql.append(sql)
        if "journal_mode" in sql:
            if isinstance(self.journal_result, Exception):
                raise self.journal_result
            self._last = (self.journal_result,)
        else:
            self._last = None

    def fetchone(self):
        return self._last

    def close(self):
        pass


def _connect_with(db: Database, cursor: _FakeCursor) -> None:
    db._on_sqlite_connect(SimpleNamespace(cursor=lambda: cursor), None)


def test_wal_refusal_warns_once_and_falls_back_to_full(tmp_path, caplog):
    db = Database(_url(tmp_path / "app.db"))
    try:
        cursors = [_FakeCursor("delete"), _FakeCursor("delete")]
        with caplog.at_level(logging.WARNING, logger="monitor_app.db"):
            for cur in cursors:
                _connect_with(db, cur)
        warnings = [
            r for r in caplog.records if "WAL モードにできませんでした" in r.message
        ]
        assert len(warnings) == 1  # 接続ごとではなく 1 回だけ
        assert "MONITOR_SQLITE_JOURNAL_MODE=delete" in warnings[0].message
        assert cursors[0].sql == [
            "PRAGMA busy_timeout = 5000",
            "PRAGMA journal_mode = wal",
            "PRAGMA synchronous = full",
        ]
        assert db.journal_mode == "delete"
    finally:
        db.dispose()


def test_journal_mode_errors_do_not_break_the_connection(tmp_path, caplog):
    db = Database(_url(tmp_path / "app.db"))
    try:
        cur = _FakeCursor(sqlite3.OperationalError("database is locked"))
        with caplog.at_level(logging.WARNING, logger="monitor_app.db"):
            _connect_with(db, cur)
        assert "database is locked" in caplog.text
        assert cur.sql[-1] == "PRAGMA synchronous = full"
        assert db.journal_mode is None
    finally:
        db.dispose()


@pytest.mark.skipif(
    os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="読み取り専用フォルダを POSIX の権限で作る",
)
def test_read_only_location_warns_and_keeps_working(tmp_path, caplog):
    folder = tmp_path / "ro"
    folder.mkdir()
    path = folder / "app.db"
    with closing(sqlite3.connect(path)) as raw, raw:
        raw.execute("CREATE TABLE t (id INTEGER)")
        raw.execute("INSERT INTO t VALUES (1)")
    path.chmod(0o444)
    folder.chmod(0o555)
    try:
        db = Database(_url(path))
        with (
            caplog.at_level(logging.WARNING, logger="monitor_app.db"),
            db.readonly() as conn,
        ):
            assert conn.execute(text("SELECT COUNT(*) FROM t")).scalar() == 1
        assert "WAL モードにできませんでした" in caplog.text
        db.dispose()
    finally:
        folder.chmod(0o755)
        path.chmod(0o644)


# ---------------------------------------------------------------------------
# ネットワーク共有
# ---------------------------------------------------------------------------
MOUNTINFO = (
    "22 1 8:1 / / rw,relatime - ext4 /dev/sda1 rw\n"
    "30 22 0:40 / /mnt/nas rw,relatime - cifs //nas/share rw\n"
    "31 22 0:41 / /mnt/my\\040nfs rw - nfs4 nas:/x rw\n"
    "32 22 0:42 / /mnt/c rw - 9p drvfs rw\n"
    "33 30 8:2 / /mnt/nas/local rw - ext4 /dev/sdb1 rw\n"
)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/home/u/proj/instances/database.db", False),
        ("/mnt/nas/proj/database.db", True),
        ("/mnt/nasx/database.db", False),  # 前方一致ではなくパスの区切りで判定
        ("/mnt/my nfs/database.db", True),  # mountinfo の \040 は空白
        ("/mnt/c/Users/u/database.db", True),  # WSL のホスト共有(9p)
        ("/mnt/nas/local/database.db", False),  # より深いマウントが優先
    ],
)
@pytest.mark.skipif(os.name == "nt", reason="Linux の mountinfo による判定")
def test_network_filesystems_are_detected_from_mountinfo(path, expected):
    assert is_network_path(path, mountinfo=MOUNTINFO) is expected


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("\\\\server\\share\\db.sqlite", True),
        ("//server/share/db.sqlite", True),
        ("\\\\?\\UNC\\server\\share\\db.sqlite", True),
        ("\\\\?\\C:\\data\\db.sqlite", False),  # Windows の拡張パス(ローカル)
        ("\\\\.\\C:\\db.sqlite", False),
        ("C:\\data\\db.sqlite", False),
    ],
)
def test_unc_paths(path, expected):
    assert engine_mod._is_unc_path(path) is expected
    if expected:
        assert is_network_path(path)


def test_triple_slash_is_not_unc():
    assert not engine_mod._is_unc_path("///abs/path.db")


def test_network_share_uses_delete_journal(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(engine_mod, "is_network_path", lambda path: True)
    with caplog.at_level(logging.INFO, logger="monitor_app.db"):
        db = Database(_url(tmp_path / "app.db"))
    try:
        assert _pragmas(db)[:2] == ("delete", 2)
        assert "ネットワーク共有" in caplog.text
    finally:
        db.dispose()


def test_explicit_non_wal_mode_is_kept_on_network_share(tmp_path, monkeypatch):
    monkeypatch.setattr(engine_mod, "is_network_path", lambda path: True)
    db = Database(_url(tmp_path / "app.db"), sqlite_journal_mode="truncate")
    try:
        assert _pragmas(db)[0] == "truncate"
    finally:
        db.dispose()


def test_unreadable_mountinfo_means_local(monkeypatch, tmp_path):
    def boom(*_a, **_k):
        raise OSError("no /proc")

    monkeypatch.setattr(engine_mod, "open", boom, raising=False)
    assert engine_mod._linux_fs_type(str(tmp_path)) is None


# ---------------------------------------------------------------------------
# 保存先フォルダ・URL の扱い・説明
# ---------------------------------------------------------------------------
def test_parent_directories_are_created(tmp_path):
    path = tmp_path / "a" / "b" / "app.db"
    db = Database(_url(path))
    try:
        with db.connect() as conn:
            conn.execute(text("CREATE TABLE t (id INTEGER)"))
        assert path.exists()
    finally:
        db.dispose()


def test_relative_paths_are_created_under_the_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = Database("sqlite:///instances/database.db")
    try:
        assert (tmp_path / "instances").is_dir()
        assert db.sqlite_path == tmp_path / "instances" / "database.db"
        assert db.describe() == f"SQLite {tmp_path / 'instances' / 'database.db'}"
    finally:
        db.dispose()


def test_tilde_is_expanded_for_sqlite(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    db = Database("sqlite:///~/data/app.db")
    try:
        with db.connect() as conn:
            conn.execute(text("CREATE TABLE t (id INTEGER)"))
        assert (tmp_path / "data" / "app.db").exists()
        assert db.sqlite_path == tmp_path / "data" / "app.db"
    finally:
        db.dispose()


def test_uri_databases_are_not_touched(tmp_path):
    target = tmp_path / "nodir" / "x.db"
    db = Database(f"sqlite:///file:{target.as_posix()}?uri=true")
    try:
        assert not target.parent.exists()  # file: URI の場所は作らない
        assert db.sqlite_path is None
        assert db.describe() == f"SQLite file:{target.as_posix()}"
    finally:
        db.dispose()


def test_read_only_uri_does_not_change_the_journal_mode(tmp_path):
    path = tmp_path / "x.db"
    with closing(sqlite3.connect(path)) as raw, raw:
        raw.execute("CREATE TABLE t (id INTEGER)")
    db = Database(f"sqlite:///file:{path.as_posix()}?mode=ro&uri=true")
    try:
        with db.readonly() as conn:
            assert conn.execute(text("PRAGMA journal_mode")).scalar() == "delete"
        assert db.journal_mode == "delete"
    finally:
        db.dispose()


def test_mkdir_failure_is_left_to_sqlite(tmp_path, caplog):
    blocker = tmp_path / "file.txt"
    blocker.write_text("not a directory")
    with caplog.at_level(logging.WARNING, logger="monitor_app.db"):
        db = Database(_url(blocker / "app.db"))
    try:
        assert "保存先フォルダを作成できません" in caplog.text
        with (
            pytest.raises(OperationalError, match="unable to open database file"),
            db.connect(),
        ):
            pass
    finally:
        db.dispose()


def test_describe_never_shows_passwords():
    db = Database(
        "postgresql+pg8000://monitor:s3cret@db.example:5432/app"
        "?sslmode=require&password=qp&sslkey=/k"
    )
    try:
        described = db.describe()
        assert "s3cret" not in described and "qp" not in described
        assert described == (
            "postgresql+pg8000://monitor:***@db.example:5432/app"
            "?password=***&sslkey=***&sslmode=require"
        )
    finally:
        db.dispose()
    assert Database("sqlite://").describe() == "SQLite(メモリ上。終了すると消えます)"


@pytest.mark.parametrize(
    "url",
    [
        "sqlite://",
        "sqlite:///:memory:",
        "sqlite:///file:shared?mode=memory&uri=true",
        "sqlite:///instances/app.db",
        "postgresql+pg8000://monitor:s3cret@db:5432/app?sslmode=require&password=qp",
        "mysql+pymysql://u:pw@h:3306/db?passwd=qp&ssl_key=/k.pem",
    ],
)
def test_describe_url_matches_the_database_description(url, tmp_path, monkeypatch):
    """起動時の表示(DB を開く前)と Database.describe() は同じ実装・同じ文言。"""
    monkeypatch.chdir(tmp_path)
    db = Database(url)
    try:
        assert describe_url(url) == db.describe()
        assert "s3cret" not in db.describe() and "qp" not in db.describe()
    finally:
        db.dispose()


def test_describe_url_masks_pwd_and_never_echoes_an_unparseable_url():
    described = describe_url("mssql+pyodbc://u@h/db?pwd=qp&driver=ODBC")
    assert "qp" not in described and "pwd=***" in described
    assert "driver=ODBC" in described
    assert "secret" not in describe_url("not a url secret")


def test_from_settings_uses_the_tuning_settings(tmp_path):
    settings = AppSettings(
        _env_file=None,
        database_url=_url(tmp_path / "app.db"),
        sqlite_journal_mode="truncate",
        sqlite_synchronous="off",
        sqlite_busy_timeout_ms=777,
    )
    db = Database.from_settings(settings)
    try:
        assert db.url == settings.database_url
        assert _pragmas(db) == ("truncate", 0, 777)
    finally:
        db.dispose()


# ---------------------------------------------------------------------------
# サーバー DB
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "url",
    [
        "postgresql+pg8000://u:p@db.example/app",
        "mysql+pymysql://u:p@db.example/app",
    ],
)
def test_server_databases_ping_and_recycle_pooled_connections(url):
    db = Database(url)  # create_engine は接続しない
    try:
        assert db.engine.pool._pre_ping is True
        assert db.engine.pool._recycle == 3600
        assert db.sqlite_path is None and not db.is_memory
    finally:
        db.dispose()


def test_mysql_sessions_keep_explicit_zero_ids():
    db = Database("mysql+pymysql://u:p@db.example/app")
    try:
        assert event.contains(db.engine, "connect", engine_mod._mysql_session_setup)
        cur = _FakeCursor(None)
        engine_mod._mysql_session_setup(SimpleNamespace(cursor=lambda: cur), None)
        assert "NO_AUTO_VALUE_ON_ZERO" in cur.sql[0]
    finally:
        db.dispose()
    pg = Database("postgresql+pg8000://u:p@db.example/app")
    try:
        assert not event.contains(pg.engine, "connect", engine_mod._mysql_session_setup)
    finally:
        pg.dispose()


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(round(seconds, 3))
        self.now += seconds


class _Conn:
    def __init__(self, owner) -> None:
        self.owner = owner

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, _stmt):
        self.owner.calls += 1
        return None


class _FlakyEngine:
    """``failures`` 回だけ接続に失敗するエンジンの代役。"""

    def __init__(self, error, failures: int) -> None:
        self.error = error
        self.failures = failures
        self.calls = 0
        self.dialect = SimpleNamespace(name="postgresql")

    def connect(self):
        if self.failures:
            self.failures -= 1
            raise self.error
        return _Conn(self)

    def dispose(self):
        pass


def _refused() -> OperationalError:
    return OperationalError(
        "SELECT 1", {}, ConnectionRefusedError("connection refused")
    )


@pytest.fixture
def clock(monkeypatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(
        engine_mod, "time", SimpleNamespace(monotonic=fake.monotonic, sleep=fake.sleep)
    )
    return fake


def _server_db(engine) -> Database:
    db = Database("postgresql+pg8000://u:secret@db.example/app")
    db.engine.dispose()
    db.engine = engine
    return db


def test_wait_until_ready_retries_with_backoff(clock, caplog):
    engine = _FlakyEngine(_refused(), failures=4)
    db = _server_db(engine)
    with caplog.at_level(logging.INFO, logger="monitor_app.db"):
        db.wait_until_ready(30)
    assert engine.calls == 1
    assert clock.sleeps == [0.5, 1.0, 2.0, 4.0]
    assert "データベースの起動を待っています" in caplog.text
    assert "secret" not in caplog.text  # パスワードを出さない
    assert "データベースに接続しました" in caplog.text


def test_wait_until_ready_gives_up_after_the_timeout(clock):
    error = _refused()
    db = _server_db(_FlakyEngine(error, failures=100))
    with pytest.raises(OperationalError) as excinfo:
        db.wait_until_ready(3)
    assert excinfo.value is error  # 元のエラーをそのまま
    # 最後は残り時間だけ待ってからもう 1 回試す(合計で timeout ちょうど)
    assert clock.sleeps == [0.5, 1.0, 1.5]


def test_wait_until_ready_caps_the_delay(clock):
    db = _server_db(_FlakyEngine(_refused(), failures=6))
    db.wait_until_ready(100, initial_delay=1, max_delay=3)
    assert clock.sleeps == [1, 2, 3, 3, 3, 3]


def test_wait_until_ready_returns_at_once_when_ready(clock):
    engine = _FlakyEngine(_refused(), failures=0)
    db = _server_db(engine)
    db.wait_until_ready(10)
    assert clock.sleeps == [] and engine.calls == 1


def _auth_errors():
    import pymysql

    errors = [
        OperationalError("SELECT 1", {}, pymysql.err.OperationalError(1045, "denied")),
    ]
    try:
        import pg8000.dbapi

        errors.append(
            OperationalError(
                "SELECT 1",
                {},
                pg8000.dbapi.ProgrammingError(
                    {"C": "28P01", "M": "password authentication failed"}
                ),
            )
        )
    except ImportError:  # pragma: no cover - pg8000 は必須依存
        pass
    errors.append(
        OperationalError(
            "SELECT 1",
            {},
            Exception('FATAL:  database "nodb" does not exist'),
        )
    )
    return errors


@pytest.mark.parametrize("error", _auth_errors())
def test_wait_until_ready_fails_fast_on_permanent_errors(clock, error):
    db = _server_db(_FlakyEngine(error, failures=100))
    with pytest.raises(OperationalError):
        db.wait_until_ready(30)
    assert clock.sleeps == []


def test_wait_until_ready_with_zero_timeout_does_not_wait(clock):
    db = _server_db(_FlakyEngine(_refused(), failures=1))
    with pytest.raises(OperationalError):
        db.wait_until_ready(0)
    assert clock.sleeps == []
