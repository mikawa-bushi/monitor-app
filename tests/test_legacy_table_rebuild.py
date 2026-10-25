"""主キーの無い v0.x の表: check / migrate が作り直す具体的な手順を示し、その手順で
実際に直る(D5-09)。"""

from __future__ import annotations

import sqlite3
from contextlib import closing

from fastapi.testclient import TestClient

from monitor_app import MonitorConfig, TableDef
from monitor_app.db.engine import Database
from monitor_app.db.registry import TableRegistry
from monitor_app.db.schema import PRIMARY_KEY, inspect_schema
from monitor_app.main import create_app
from monitor_app.settings.runtime import AppSettings


def _config() -> MonitorConfig:
    return MonitorConfig(tables={"users": TableDef(columns=["id", "name", "email"])})


def test_missing_primary_key_message_gives_the_steps(tmp_path):
    path = tmp_path / "v0.db"
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("CREATE TABLE users (id BIGINT, name TEXT, email TEXT)")
    db = Database(f"sqlite:///{path.as_posix()}")
    try:
        [problem] = inspect_schema(db, TableRegistry(_config())).problems
    finally:
        db.dispose()
    assert problem.kind == PRIMARY_KEY
    message = problem.message
    for step in (
        "monitor-app backup",
        "ALTER TABLE users RENAME TO users_v0;",
        "monitor-app import-csv",
        "INSERT INTO users (id, name, email) SELECT id, name, email FROM users_v0;",
        "DROP TABLE users_v0;",
    ):
        assert step in message


def test_following_the_steps_fixes_the_table(tmp_path):
    path = tmp_path / "v0.db"
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("CREATE TABLE users (id BIGINT, name TEXT, email TEXT)")
        conn.execute("INSERT INTO users VALUES (1, 'a', 'a@x'), (2, 'b', 'b@x')")
    settings = AppSettings(database_url=f"sqlite:///{path.as_posix()}")
    # ② 名前を変える → ③ サーバーを 1 回起動して新しい表を作る → ④ 行を写す
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("ALTER TABLE users RENAME TO users_v0")
    with TestClient(create_app(_config(), settings)):
        pass
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(
            "INSERT INTO users (id, name, email) SELECT id, name, email FROM users_v0"
        )
    db = Database(settings.database_url)
    try:
        assert inspect_schema(db, TableRegistry(_config())).problems == []
    finally:
        db.dispose()
    with TestClient(create_app(_config(), settings)) as client:
        res = client.post("/api/tables/users", json={"name": "c", "email": "c@x"})
    assert res.status_code == 201
    assert res.json()["data"]["id"] == 3
