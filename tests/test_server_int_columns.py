"""新しく作る表の整数列: PostgreSQL / MySQL では主キー・外部キー以外を BIGINT にする。

Unix 時刻のミリ秒など 32 ビットを超える整数は、SQLite には入るがサーバー DB の INTEGER
の列には入らず、取り込みがその行を読み飛ばしていた(D3-06 の続き)。主キー(連番)・
外部キー・外部キーの参照先は INTEGER のまま(MySQL は外部キーと参照先の型が同じで
ないと表を作れない)。SQLite の DDL は変えない。
"""

from __future__ import annotations

import pytest
from sqlalchemy.dialects import mysql, postgresql, sqlite
from sqlalchemy.schema import CreateTable

from monitor_app import MonitorConfig, TableDef
from monitor_app.db.registry import TableRegistry
from monitor_app.db.schema import type_family

CONFIG = MonitorConfig(
    tables={
        "users": TableDef(
            columns={"id": "int", "code": "int", "ts": "int"}, primary_key="id"
        ),
        "orders": TableDef(
            columns={"id": "int", "user_id": "int", "user_code": "int", "qty": "int"},
            foreign_keys={"user_id": "users.id", "user_code": "users.code"},
        ),
        "cat": TableDef(
            columns={"id": "int", "parent_id": "int"},
            foreign_keys={"parent_id": "cat.id"},
        ),
    }
)


def _ddl(table: str, dialect) -> dict[str, str]:
    sql = str(CreateTable(TableRegistry(CONFIG).get(table)).compile(dialect=dialect))
    columns = {}
    for line in sql.splitlines():
        parts = line.strip().rstrip(",").split()
        if len(parts) >= 2 and parts[0] in {
            "id",
            "code",
            "ts",
            "user_id",
            "user_code",
            "qty",
            "parent_id",
        }:
            columns[parts[0]] = parts[1]
    return columns


@pytest.mark.parametrize("dialect", [postgresql.dialect(), mysql.dialect()])
def test_server_databases_get_bigint_for_plain_integer_columns(dialect):
    users = _ddl("users", dialect)
    orders = _ddl("orders", dialect)
    assert users["ts"] == orders["qty"] == "BIGINT"
    # 主キー・外部キー・外部キーの参照先(users.code)は INTEGER(連番は SERIAL など)
    assert users["code"] == orders["user_id"] == orders["user_code"] == "INTEGER"
    assert users["id"] in {"SERIAL", "INTEGER"}
    # 自分自身を参照する外部キーも INTEGER
    assert _ddl("cat", dialect)["parent_id"] == "INTEGER"


def test_sqlite_ddl_is_unchanged():
    assert set(_ddl("users", sqlite.dialect()).values()) == {"INTEGER"}
    assert set(_ddl("orders", sqlite.dialect()).values()) == {"INTEGER"}


def test_bigint_columns_still_count_as_int_for_schema_checks():
    table = TableRegistry(CONFIG).get("users")
    assert type_family(table.c.ts.type) == "int"
    assert type_family(table.c.ts.type.dialect_impl(postgresql.dialect())) == "int"
