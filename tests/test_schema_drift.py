"""config.py と DB の表定義のずれ(DEPLOY-13 / CRIT-05)の検出と、加算的な列の追加。"""

from __future__ import annotations

import logging
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from contract import helpers, snapshot
from fastapi.testclient import TestClient
from sqlalchemy import Integer, Numeric, String, Text, inspect, text
from sqlalchemy.dialects import mysql
from sqlalchemy.sql.sqltypes import NullType

from monitor_app import MonitorConfig, TableDef
from monitor_app.db import schema as schema_mod
from monitor_app.db.engine import Database
from monitor_app.db.registry import TableRegistry
from monitor_app.db.schema import (
    EXTRA_COLUMN,
    MISSING_COLUMN,
    MISSING_TABLE,
    PRIMARY_KEY,
    TYPE_MISMATCH,
    apply_additive_migrations,
    inspect_schema,
    type_family,
)
from monitor_app.main import create_app
from monitor_app.settings.loader import load_config
from monitor_app.settings.runtime import AppSettings


@pytest.fixture(autouse=True)
def _propagate_monitor_logs(monkeypatch):
    """CLI のテストが configure_logging で monitor_app を propagate=False にしていても、
    caplog(root のハンドラ)で monitor_app.* のログを拾えるようにする。"""
    monkeypatch.setattr(logging.getLogger("monitor_app"), "propagate", True)


def _url(path: Path) -> str:
    return helpers.sqlite_url(path)


def _settings(url: str) -> AppSettings:
    return AppSettings(_env_file=None, database_url=url)


def _db_with(tmp_path, ddl: str) -> tuple[Database, Path]:
    path = tmp_path / "app.db"
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(ddl)
    return Database(_url(path)), path


def _kinds(report) -> list[tuple]:
    return [(p.kind, p.table, p.column, p.level) for p in report.problems]


def test_matching_schema_has_no_problems(tmp_path):
    config = MonitorConfig(
        tables={
            "a": TableDef(
                columns={
                    "id": "int",
                    "s": "str",
                    "f": "float",
                    "b": "bool",
                    "d": "date",
                    "t": "datetime",
                }
            )
        }
    )
    db = Database(_url(tmp_path / "app.db"))
    registry = TableRegistry(config)
    registry.create_all(db)
    report = inspect_schema(db, registry)
    assert report.problems == [] and report.ok
    db.dispose()


def test_golden_v2_3_1_database_has_no_problems(tmp_path):
    """v2.3.1 で作った DB(float は FLOAT、索引なし)をずれとして報告しない。"""
    proj = tmp_path / "golden"
    shutil.copytree(
        snapshot.GOLDEN_PROJECT, proj, ignore=shutil.ignore_patterns("*.sql")
    )
    db_path = proj / "database.db"
    with closing(sqlite3.connect(db_path)) as conn:
        conn.executescript(
            (snapshot.GOLDEN_PROJECT / "database.sql").read_text(encoding="utf-8")
        )
    db = Database(_url(db_path))
    report = inspect_schema(db, TableRegistry(load_config(proj / "config.py")))
    assert report.problems == []
    db.dispose()


def test_missing_column_is_reported_with_the_fix_and_can_be_added(tmp_path):
    db, _path = _db_with(
        tmp_path,
        "CREATE TABLE users (id INTEGER PRIMARY KEY, name VARCHAR(255));"
        "INSERT INTO users VALUES (1, 'a');",
    )
    config = MonitorConfig(
        tables={"users": TableDef(columns=["id", "name", "dept", "order"])}
    )
    registry = TableRegistry(config)
    report = inspect_schema(db, registry)
    assert _kinds(report) == [
        (MISSING_COLUMN, "users", "dept", "warning"),
        (MISSING_COLUMN, "users", "order", "warning"),
    ]
    assert not report.ok
    assert report.missing_columns() == {"users": ["dept", "order"]}
    first = report.problems[0]
    assert first.fix_sql == "ALTER TABLE users ADD COLUMN dept VARCHAR(255)"
    assert first.fix_sql in first.message
    assert report.problems[1].fix_sql.endswith('ADD COLUMN "order" VARCHAR(255)')

    executed = apply_additive_migrations(db, registry, report)

    assert executed == [first.fix_sql, report.problems[1].fix_sql]
    assert inspect_schema(db, registry).problems == []
    with db.connect() as conn:
        assert conn.execute(text("SELECT * FROM users")).all() == [(1, "a", None, None)]
    assert apply_additive_migrations(db, registry, inspect_schema(db, registry)) == []
    db.dispose()


def test_extra_columns(tmp_path):
    db, _ = _db_with(
        tmp_path,
        "CREATE TABLE t (id INTEGER PRIMARY KEY, name VARCHAR(255),"
        " memo TEXT, required_col TEXT NOT NULL, defaulted TEXT NOT NULL DEFAULT 'x');",
    )
    config = MonitorConfig(tables={"t": TableDef(columns=["id", "name"])})
    report = inspect_schema(db, TableRegistry(config))
    assert _kinds(report) == [
        (EXTRA_COLUMN, "t", "memo", "info"),
        (EXTRA_COLUMN, "t", "required_col", "warning"),
        (EXTRA_COLUMN, "t", "defaulted", "info"),
    ]
    assert "行の追加が失敗します" in report.problems[1].message
    db.dispose()


def test_extra_not_null_column_makes_inserts_a_409(tmp_path):
    db, path = _db_with(
        tmp_path,
        "CREATE TABLE t (id INTEGER PRIMARY KEY, name VARCHAR(255), x TEXT NOT NULL);",
    )
    db.dispose()
    config = MonitorConfig(tables={"t": TableDef(columns=["id", "name"])})
    client = TestClient(
        create_app(config, _settings(_url(path))), raise_server_exceptions=False
    )
    res = client.post("/api/tables/t", json={"name": "a"})
    assert res.status_code == 409
    assert res.json()["detail"]["message"] == "必須の列に値がありません(t.x)"


def test_type_mismatches(tmp_path):
    db, _ = _db_with(
        tmp_path,
        "CREATE TABLE m (id INTEGER PRIMARY KEY, temp VARCHAR(255), flag INTEGER,"
        " day DATETIME, ts DATE, n);",
    )
    config = MonitorConfig(
        tables={
            "m": TableDef(
                columns={
                    "id": "int",
                    "temp": "float",  # DX-08: float に直しても DB は VARCHAR のまま
                    "flag": "bool",  # 0/1 の整数列は真偽値として扱える
                    "day": "date",
                    "ts": "datetime",
                    "n": "int",  # 型の無い列(SQLite)は比べない
                }
            )
        }
    )
    report = inspect_schema(db, TableRegistry(config))
    assert [(p.kind, p.column, p.expected, p.actual) for p in report.problems] == [
        (TYPE_MISMATCH, "temp", "float", "VARCHAR(255)"),
        (TYPE_MISMATCH, "day", "date", "DATETIME"),
        (TYPE_MISMATCH, "ts", "datetime", "DATE"),
    ]
    assert "表を作り直すか" in report.problems[0].message
    db.dispose()


def test_legacy_table_without_primary_key(tmp_path):
    """v0.1.x(pandas の to_sql)で作った主キーの無い表(CRIT-05)。"""
    db, path = _db_with(
        tmp_path,
        "CREATE TABLE users (id BIGINT, name TEXT, email TEXT);"
        "INSERT INTO users VALUES (1, 'a', 'a@x'), (2, 'b', 'b@x');",
    )
    config = MonitorConfig(tables={"users": TableDef(columns=["id", "name", "email"])})
    report = inspect_schema(db, TableRegistry(config))
    assert _kinds(report) == [(PRIMARY_KEY, "users", "id", "warning")]
    assert "主キーがありません" in report.problems[0].message
    assert "作り直して" in report.problems[0].message
    db.dispose()

    client = TestClient(
        create_app(config, _settings(_url(path))), raise_server_exceptions=False
    )
    res = client.post("/api/tables/users", json={"name": "new", "email": "n@x"})
    # 存在しない id を返さず(以前は {"id": 3} を返し、GET すると 404)、取り消して知らせる
    assert res.status_code == 500
    detail = res.json()["detail"]
    assert detail["code"] == "DB_SCHEMA_MISMATCH"
    assert "monitor-app check" in detail["message"] and "(ref=" in detail["message"]
    assert helpers.query(path, "SELECT COUNT(*) FROM users") == [(2,)]
    assert client.get("/api/tables/users").status_code == 200  # 読み取りはできる


def test_different_primary_key(tmp_path):
    db, _ = _db_with(
        tmp_path, "CREATE TABLE p (code VARCHAR(10) PRIMARY KEY, id INTEGER);"
    )
    config = MonitorConfig(tables={"p": TableDef(columns={"id": "int", "code": "str"})})
    report = inspect_schema(db, TableRegistry(config))
    ((kind, _t, column, _level),) = _kinds(report)
    assert (kind, column) == (PRIMARY_KEY, "id")
    assert "config.py: id / DB: code" in report.problems[0].message
    db.dispose()


def test_missing_table_is_informational(tmp_path):
    db = Database(_url(tmp_path / "app.db"))
    config = MonitorConfig(tables={"new_table": TableDef(columns=["id"])})
    report = inspect_schema(db, TableRegistry(config))
    assert _kinds(report) == [(MISSING_TABLE, "new_table", None, "info")]
    assert report.ok
    db.dispose()


@pytest.mark.parametrize(
    ("sql_type", "family"),
    [
        (mysql.TINYINT(display_width=1), "bool"),
        (mysql.TINYINT(display_width=4), "int"),
        (mysql.DOUBLE(), "float"),
        (Numeric(10, 2), "float"),
        (Integer(), "int"),
        (Text(), "str"),
        (String(10), "str"),
        (NullType(), None),
    ],
)
def test_type_families(sql_type, family):
    assert type_family(sql_type) == family


# ---------------------------------------------------------------------------
# 起動時の警告
# ---------------------------------------------------------------------------
def test_startup_logs_each_problem_and_never_fails(tmp_path, caplog):
    url = _url(tmp_path / "app.db")
    create_app(
        MonitorConfig(tables={"users": TableDef(columns=["id", "name"])}),
        _settings(url),
    )
    drifted = MonitorConfig(tables={"users": TableDef(columns=["id", "name", "dept"])})
    with caplog.at_level(logging.INFO, logger="monitor_app.db"):
        app = create_app(drifted, _settings(url))
    warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "表定義のずれ" in r.message
    ]
    assert len(warnings) == 1
    assert "ALTER TABLE users ADD COLUMN dept VARCHAR(255)" in warnings[0].message
    # 自動では列を足さない(既存の表の定義を変えない)
    columns = [c["name"] for c in inspect(app.state.db.engine).get_columns("users")]
    assert columns == ["id", "name"]


def test_startup_survives_a_failing_schema_check(tmp_path, caplog, monkeypatch):
    def boom(db, registry):
        raise RuntimeError("inspector failed")

    monkeypatch.setattr("monitor_app.container.inspect_schema", boom)
    config = MonitorConfig(tables={"t": TableDef(columns=["id"])})
    with caplog.at_level(logging.WARNING, logger="monitor_app.db"):
        app = create_app(config, _settings(_url(tmp_path / "app.db")))
    assert "表定義を確認できませんでした: inspector failed" in caplog.text
    assert TestClient(app).get("/api/tables/t").status_code == 200


def test_report_log_levels(caplog):
    report = schema_mod.SchemaReport(
        problems=[
            schema_mod.SchemaProblem(MISSING_TABLE, "a", "info line", level="info"),
            schema_mod.SchemaProblem(MISSING_COLUMN, "b", "warning line"),
        ]
    )
    with caplog.at_level(logging.INFO, logger="monitor_app.db"):
        report.log()
    assert [(r.levelno, r.message) for r in caplog.records] == [
        (logging.INFO, "表定義のずれ: info line"),
        (logging.WARNING, "表定義のずれ: warning line"),
    ]
