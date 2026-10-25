"""DB ドライバの例外の分類(db/errors.py)と、HTTP・CLI での表示のテスト。

ドライバごとに例外クラスが違う(pg8000 は外部キー違反や文字列長超過を
ProgrammingError で送出する)ため、SQLSTATE・MySQL のエラー番号・SQLite の
メッセージで分類できることを、各ドライバの実際の例外の形で確かめる。
"""

from __future__ import annotations

import logging
import sqlite3

import pg8000.dbapi
import pymysql
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import (
    DataError,
    DBAPIError,
    IntegrityError,
    InterfaceError,
    OperationalError,
    ProgrammingError,
)

from monitor_app.api.errors import register_exception_handlers
from monitor_app.cli._common import explain_error
from monitor_app.db import errors as db_errors
from monitor_app.exceptions import BackupError, SchemaMismatchError


@pytest.fixture(autouse=True)
def _propagate_monitor_logs(monkeypatch):
    """CLI のテストが configure_logging で monitor_app を propagate=False にしていても、
    caplog(root のハンドラ)で monitor_app.* のログを拾えるようにする。"""
    monkeypatch.setattr(logging.getLogger("monitor_app"), "propagate", True)


STMT = "INSERT INTO t (id, name) VALUES (?, ?)"


class _PsycopgLike(Exception):
    """psycopg / psycopg2 の例外の形(属性 sqlstate を持つ)。"""

    def __init__(self, message: str, sqlstate: str) -> None:
        super().__init__(message)
        self.sqlstate = sqlstate


def _pg8000(cls, code: str, message: str, detail: str | None = None):
    fields = {"S": "ERROR", "C": code, "M": message}
    if detail:
        fields["D"] = detail
    return cls(fields)


CASES = [
    # (SQLAlchemy の例外, 分類, メッセージに含む語句)
    (
        IntegrityError(
            STMT, {}, sqlite3.IntegrityError("UNIQUE constraint failed: t.id")
        ),
        db_errors.CONFLICT,
        "同じキーのレコードが既に存在します(主キーの重複)(t.id)",
    ),
    (
        IntegrityError(
            STMT, {}, sqlite3.IntegrityError("NOT NULL constraint failed: t.name")
        ),
        db_errors.CONFLICT,
        "必須の列に値がありません(t.name)",
    ),
    (
        IntegrityError(
            STMT, {}, sqlite3.IntegrityError("FOREIGN KEY constraint failed")
        ),
        db_errors.CONFLICT,
        "外部キー制約に違反しました",
    ),
    (
        IntegrityError(STMT, {}, sqlite3.IntegrityError("CHECK constraint failed: c")),
        db_errors.CONFLICT,
        "CHECK",
    ),
    (
        OperationalError(
            STMT, {}, sqlite3.OperationalError("no such column: users.dept")
        ),
        db_errors.SCHEMA,
        "(users.dept)",
    ),
    (
        OperationalError(
            STMT, {}, sqlite3.OperationalError("table users has no column named dept")
        ),
        db_errors.SCHEMA,
        "(dept)",
    ),
    (
        OperationalError(STMT, {}, sqlite3.OperationalError("no such table: ghost")),
        db_errors.SCHEMA,
        "(ghost)",
    ),
    (
        OperationalError(
            STMT,
            {},
            sqlite3.OperationalError(
                "ON CONFLICT clause does not match any PRIMARY KEY or UNIQUE constraint"
            ),
        ),
        db_errors.SCHEMA,
        "config.py と一致しません",
    ),
    (
        # "database is locked" はロック待ちの時間切れ(BUSY — tests/test_db_busy.py)
        OperationalError(STMT, {}, sqlite3.OperationalError("disk I/O error")),
        db_errors.OTHER,
        None,
    ),
    # pg8000: SQLSTATE 22/23 の多くを ProgrammingError で送出する
    (
        ProgrammingError(
            STMT,
            {},
            _pg8000(
                pg8000.dbapi.ProgrammingError,
                "22001",
                "value too long for type character varying(255)",
            ),
        ),
        db_errors.INVALID,
        "文字列が列の最大長を超えています(str 列は 255 文字まで)",
    ),
    (
        ProgrammingError(
            STMT,
            {},
            _pg8000(
                pg8000.dbapi.ProgrammingError,
                "23503",
                'insert or update on table "t" violates foreign key constraint',
                'Key (pid)=(999) is not present in table "p".',
            ),
        ),
        db_errors.CONFLICT,
        "外部キー制約に違反しました(参照先のレコードが存在しないか、"
        "ほかのレコードから参照されています)(pid)",
    ),
    (
        IntegrityError(
            STMT,
            {},
            _pg8000(
                pg8000.dbapi.IntegrityError,
                "23505",
                'duplicate key value violates unique constraint "t_pkey"',
                "Key (id)=(1) already exists.",
            ),
        ),
        db_errors.CONFLICT,
        "主キーの重複)(id)",
    ),
    (
        ProgrammingError(
            STMT,
            {},
            _pg8000(
                pg8000.dbapi.ProgrammingError, "42703", 'column "dept" does not exist'
            ),
        ),
        db_errors.SCHEMA,
        "(dept)",
    ),
    (
        ProgrammingError(
            STMT,
            {},
            _pg8000(
                pg8000.dbapi.ProgrammingError,
                "42P10",
                "there is no unique or exclusion constraint matching the ON CONFLICT",
            ),
        ),
        db_errors.SCHEMA,
        "config.py と一致しません",
    ),
    (
        ProgrammingError(
            STMT,
            {},
            _pg8000(
                pg8000.dbapi.ProgrammingError,
                "42P01",
                'relation "ghost" does not exist',
            ),
        ),
        db_errors.SCHEMA,
        "(ghost)",
    ),
    # psycopg: 属性 sqlstate
    (
        DataError(STMT, {}, _PsycopgLike("integer out of range", "22003")),
        db_errors.INVALID,
        "数値が列の範囲を超えています",
    ),
    (
        IntegrityError(
            STMT,
            {},
            _PsycopgLike(
                'null value in column "email" of relation "users" violates not-null',
                "23502",
            ),
        ),
        db_errors.CONFLICT,
        "必須の列に値がありません(email)",
    ),
    (
        DataError(
            STMT, {}, _PsycopgLike("invalid input syntax for type date", "22007")
        ),
        db_errors.INVALID,
        "日付・時刻の値が不正です",
    ),
    (
        DataError(STMT, {}, _PsycopgLike("invalid input syntax for integer", "22P02")),
        db_errors.INVALID,
        "値の形式が列の型と合いません",
    ),
    (
        ProgrammingError(
            STMT, {}, _PsycopgLike("column users.dept does not exist", "42703")
        ),
        db_errors.SCHEMA,
        "(users.dept)",
    ),
    # PyMySQL: エラー番号
    (
        DataError(
            STMT,
            {},
            pymysql.err.DataError(1406, "Data too long for column 'name' at row 1"),
        ),
        db_errors.INVALID,
        "(str 列は 255 文字まで)(name)",
    ),
    (
        IntegrityError(
            STMT,
            {},
            pymysql.err.IntegrityError(1062, "Duplicate entry '1' for key 't.PRIMARY'"),
        ),
        db_errors.CONFLICT,
        "(t.PRIMARY)",
    ),
    (
        IntegrityError(
            STMT,
            {},
            pymysql.err.IntegrityError(1451, "Cannot delete or update a parent row"),
        ),
        db_errors.CONFLICT,
        "外部キー制約に違反しました",
    ),
    (
        OperationalError(
            STMT,
            {},
            pymysql.err.OperationalError(1054, "Unknown column 'dept' in 'field list'"),
        ),
        db_errors.SCHEMA,
        "(dept)",
    ),
    (
        ProgrammingError(
            STMT,
            {},
            pymysql.err.ProgrammingError(1146, "Table 'monitor.ghost' doesn't exist"),
        ),
        db_errors.SCHEMA,
        "(monitor.ghost)",
    ),
    (
        OperationalError(
            STMT, {}, pymysql.err.OperationalError(2013, "Lost connection")
        ),
        db_errors.OTHER,
        None,
    ),
    (
        DataError(STMT, {}, pymysql.err.DataError(1264, "Out of range value")),
        db_errors.INVALID,
        "数値が列の範囲を超えています",
    ),
    (
        DataError(STMT, {}, Exception("something odd")),
        db_errors.INVALID,
        "値がデータベースの列の制限に合いません",
    ),
    (
        IntegrityError(STMT, {}, Exception("constraint")),
        db_errors.CONFLICT,
        "データベースの制約に違反しました",
    ),
]


@pytest.mark.parametrize(("exc", "kind", "phrase"), CASES)
def test_classification_and_messages(exc, kind, phrase):
    assert db_errors.classify(exc) == kind
    if kind == db_errors.CONFLICT:
        message = db_errors.conflict_message(exc)
    elif kind == db_errors.INVALID:
        message = db_errors.invalid_message(exc)
    elif kind == db_errors.SCHEMA:
        message = db_errors.schema_message(exc)
        assert message.endswith("monitor-app check で差分を確認してください")
    else:
        return
    assert phrase in message
    assert "INSERT" not in message  # SQL 文を含めない


def test_sqlstate_and_errno_extraction():
    pg = ProgrammingError(
        STMT, {}, _pg8000(pg8000.dbapi.ProgrammingError, "22001", "x")
    )
    assert db_errors.sqlstate(pg) == "22001"
    assert db_errors.mysql_errno(pg) is None
    my = DataError(STMT, {}, pymysql.err.DataError(1406, "too long"))
    assert db_errors.mysql_errno(my) == 1406
    assert (
        db_errors.sqlstate(IntegrityError(STMT, {}, sqlite3.IntegrityError("x")))
        is None
    )
    # 例外を直接渡してもよい(SQLAlchemy で包まれていない DBAPI 例外)
    assert db_errors.sqlstate(_PsycopgLike("x", "23505")) == "23505"


def test_driver_message_is_one_line_without_sql():
    pg = ProgrammingError(
        STMT,
        {"name": "secret-value"},
        _pg8000(pg8000.dbapi.ProgrammingError, "22001", "value too long"),
    )
    assert db_errors.driver_message(pg) == "value too long"
    multi = DataError(STMT, {}, _PsycopgLike("first line\nDETAIL: second", "22001"))
    assert db_errors.driver_message(multi) == "first line"
    assert db_errors.driver_message(OperationalError(STMT, {}, Exception(""))) == (
        "Exception"
    )


# ---------------------------------------------------------------------------
# HTTP の例外ハンドラ
# ---------------------------------------------------------------------------
@pytest.fixture
def client():
    app = FastAPI()
    register_exception_handlers(app)
    errors = {
        "conflict": CASES[0][0],
        "pg8000-fk": CASES[10][0],
        "invalid": CASES[9][0],
        "mysql-long": CASES[20][0],
        "schema": CASES[4][0],
        "other": CASES[8][0],
        "interface": InterfaceError(
            "SELECT 1", {}, Exception("Can't create a connection to host db")
        ),
        "generic": DBAPIError(STMT, {}, Exception("boom")),
        "mismatch": SchemaMismatchError("追加した行を読み戻せませんでした"),
    }

    @app.get("/raise/{name}")
    def _raise(name: str):
        raise errors[name]

    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize(
    ("name", "status", "code"),
    [
        ("conflict", 409, "CONFLICT"),
        ("pg8000-fk", 409, "CONFLICT"),
        ("invalid", 422, "INVALID_PAYLOAD"),
        ("mysql-long", 422, "INVALID_PAYLOAD"),
        ("schema", 500, "DB_SCHEMA_MISMATCH"),
        ("other", 500, "DATABASE_ERROR"),
        ("interface", 500, "DATABASE_ERROR"),
        ("generic", 500, "DATABASE_ERROR"),
        ("mismatch", 500, "DB_SCHEMA_MISMATCH"),
    ],
)
def test_db_errors_use_the_json_error_envelope(client, name, status, code):
    res = client.get(f"/raise/{name}")
    assert res.status_code == status
    assert res.headers["content-type"] == "application/json"
    detail = res.json()["detail"]
    assert detail["code"] == code
    assert detail["message"]
    assert "INSERT" not in detail["message"] and "SELECT" not in detail["message"]
    if status >= 500:
        assert "(ref=" in detail["message"]


def test_server_side_errors_are_logged_with_the_reference(client, caplog):
    with caplog.at_level(logging.INFO, logger="monitor_app.api"):
        res = client.get("/raise/other")
        ref = res.json()["detail"]["message"].split("ref=")[1].rstrip(")")
        schema = client.get("/raise/schema").json()["detail"]["message"]
        client.get("/raise/conflict")
    text = caplog.text
    assert f"[{ref}] DATABASE_ERROR on GET /raise/other: disk I/O error" in text
    assert "DB_SCHEMA_MISMATCH on GET /raise/schema: no such column" in text
    assert "monitor-app check" in schema
    assert "DB constraint violation on GET /raise/conflict" in text


# ---------------------------------------------------------------------------
# CLI のエラー表示
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("exc", "message", "hint"),
    [
        (
            CASES[20][0],
            "データベースが値を受け付けませんでした: (1406, "
            "\"Data too long for column 'name' at row 1\")",
            "255 文字",
        ),
        (
            CASES[4][0],
            "データベースの表定義が config.py と一致しません: "
            "no such column: users.dept",
            "monitor-app check",
        ),
        (
            CASES[10][0],
            'データベースの制約に違反しました: insert or update on table "t" '
            "violates foreign key constraint",
            "主キーの重複",
        ),
        (
            InterfaceError(
                "SELECT 1",
                {},
                Exception("Can't create a connection to host db and port 5432"),
            ),
            "データベースエラー: Can't create a connection to host db and port 5432",
            "MONITOR_DATABASE_URL の接続先",
        ),
    ],
)
def test_cli_explains_db_errors_briefly(exc, message, hint):
    got_message, got_hint = explain_error(exc)
    assert got_message == message
    assert hint in got_hint


def test_cli_shows_domain_errors_as_their_message():
    assert explain_error(BackupError("SQLite だけに対応しています")) == (
        "SQLite だけに対応しています",
        None,
    )
    assert explain_error(RuntimeError("想定外")) is None  # 未知のエラーは隠さない
