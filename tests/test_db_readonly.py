"""Database.readonly() の読み取り専用ガードのテスト。

ガードは URL の接頭辞ではなくエンジンの方言名で選ぶ(``mariadb+pymysql://`` も
対象になる)。サーバー DB は接続せず、発行される SQL だけを確かめる。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from monitor_app.db.engine import Database


class _RecordingConnection:
    def __init__(self) -> None:
        self.sql: list[str] = []

    def __enter__(self) -> _RecordingConnection:
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def execute(self, statement) -> None:
        self.sql.append(str(statement))


@pytest.mark.parametrize(
    ("url", "dialect"),
    [
        ("postgresql+pg8000://user:pw@db.example/monitor", "postgresql"),
        ("mysql+pymysql://user:pw@db.example/monitor", "mysql"),
        ("mariadb+pymysql://user:pw@db.example/monitor", "mariadb"),
    ],
)
def test_server_databases_start_a_read_only_transaction(monkeypatch, url, dialect):
    db = Database(url)  # create_engine は接続しない
    try:
        assert db.engine.dialect.name == dialect
        conn = _RecordingConnection()
        monkeypatch.setattr(db.engine, "connect", lambda: conn)
        with db.readonly() as got:
            assert got is conn
            assert conn.sql == ["SET TRANSACTION READ ONLY"]
        assert conn.sql == ["SET TRANSACTION READ ONLY"]  # 終了時の解除は不要
    finally:
        db.dispose()


def test_unknown_dialect_gets_no_extra_statements():
    db = Database("sqlite://")
    real_engine = db.engine
    conn = _RecordingConnection()
    db.engine = SimpleNamespace(
        dialect=SimpleNamespace(name="duckdb"), connect=lambda: conn
    )
    try:
        with db.readonly():
            pass
        assert conn.sql == []
    finally:
        real_engine.dispose()


def test_sqlite_query_only_is_enabled_inside_and_reset_after():
    db = Database("sqlite://")  # StaticPool: 同じコネクションを使い回す
    try:
        with db.readonly() as conn:
            assert conn.execute(text("PRAGMA query_only")).scalar() == 1
            with pytest.raises(OperationalError):
                conn.execute(text("CREATE TABLE t (id INTEGER)"))
        with db.connect() as conn:
            # 解除済みなので、同じコネクションで書き込める。
            assert conn.execute(text("PRAGMA query_only")).scalar() == 0
            conn.execute(text("CREATE TABLE t (id INTEGER)"))
    finally:
        db.dispose()
