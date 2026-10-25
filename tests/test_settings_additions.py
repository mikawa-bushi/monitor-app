"""2.4 で追加した設定項目・宣言モデル・CLI オプションの基本動作。"""

from __future__ import annotations

import datetime as dt
import sqlite3
from contextlib import closing

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from monitor_app import MonitorConfig, Retention, TableDef, ViewDef
from monitor_app.cli import app
from monitor_app.services.coercion import CoercionError, coerce
from monitor_app.settings.runtime import AppSettings


# ---------------------------------------------------------------------------
# AppSettings
# ---------------------------------------------------------------------------
def test_new_settings_defaults():
    s = AppSettings(_env_file=None)
    assert s.sqlite_journal_mode == "wal"
    assert s.sqlite_synchronous == "normal"
    assert s.sqlite_busy_timeout_ms == 5000
    assert s.root_path == ""
    assert s.compress_responses is True
    assert s.access_log is True
    assert s.log_format == "text"
    assert s.smtp_security == "starttls"
    assert s.backup_dir is None


def test_sqlite_modes_are_case_insensitive(monkeypatch):
    monkeypatch.setenv("MONITOR_SQLITE_JOURNAL_MODE", "DELETE")
    monkeypatch.setenv("MONITOR_SQLITE_SYNCHRONOUS", " Full ")
    s = AppSettings(_env_file=None)
    assert s.sqlite_journal_mode == "delete"
    assert s.sqlite_synchronous == "full"


@pytest.mark.parametrize(
    "field,value",
    [("sqlite_journal_mode", "fast"), ("sqlite_synchronous", "sometimes")],
)
def test_invalid_sqlite_modes_rejected(field, value):
    with pytest.raises(ValidationError):
        AppSettings(_env_file=None, **{field: value})


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("", ""),
        ("/monitor", "/monitor"),
        ("/monitor/", "/monitor"),
        ("monitor", "/monitor"),
    ],
)
def test_root_path_normalized(raw, expected):
    assert AppSettings(_env_file=None, root_path=raw).root_path == expected


# ---------------------------------------------------------------------------
# 宣言モデル
# ---------------------------------------------------------------------------
def _cols():
    return {"id": "int", "ts": "datetime", "v": "float"}


def test_retention_requires_a_limit():
    with pytest.raises(ValidationError):
        Retention()


def test_retention_keep_days_requires_column():
    with pytest.raises(ValidationError):
        Retention(keep_days=7)


def test_retention_column_must_exist():
    with pytest.raises(ValidationError):
        TableDef(columns=_cols(), retention=Retention(keep_days=7, column="nope"))


def test_retention_accepts_valid_definitions():
    t = TableDef(columns=_cols(), retention=Retention(keep_days=7, column="ts"))
    assert t.retention.keep_days == 7
    t2 = TableDef(columns=_cols(), retention=Retention(keep_rows=1000))
    assert t2.retention.column is None


def test_view_max_rows_validated():
    assert ViewDef(query="SELECT 1").max_rows is None
    assert ViewDef(query="SELECT 1", max_rows=500).max_rows == 500
    with pytest.raises(ValidationError):
        ViewDef(query="SELECT 1", max_rows=0)


def test_datetime_column_type_and_coercion():
    cfg = MonitorConfig(tables={"m": TableDef(columns=_cols())})
    assert cfg.tables["m"].column_type("ts") == "datetime"
    assert coerce("2026-10-21 08:30:00", "datetime") == dt.datetime(2026, 10, 21, 8, 30)
    assert coerce("2026-10-21T08:30", "datetime") == dt.datetime(2026, 10, 21, 8, 30)
    assert coerce(dt.date(2026, 10, 21), "datetime") == dt.datetime(2026, 10, 21)
    assert coerce("", "datetime") is None
    with pytest.raises(CoercionError):
        coerce("not a time", "datetime")


# ---------------------------------------------------------------------------
# runserver --import-csv-if-empty
# ---------------------------------------------------------------------------
@pytest.fixture
def project(tmp_path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["startproject", "proj"]).exit_code == 0
    proj = tmp_path / "proj"
    monkeypatch.chdir(proj)
    return proj


def _count(proj, table):
    with closing(sqlite3.connect(proj / "instances" / "database.db")) as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_runserver_import_csv_if_empty_keeps_entered_rows(project, monkeypatch):
    runner = CliRunner()
    calls = []
    monkeypatch.setenv("MONITOR_CONFIG_PATH", "")
    monkeypatch.setattr("uvicorn.run", lambda target, **kw: calls.append(target))

    # 1 回目: 空のテーブルへ取り込む。
    result = runner.invoke(app, ["runserver", "--import-csv-if-empty"])
    assert result.exit_code == 0, result.output
    seeded = _count(project, "orders")
    assert seeded > 0

    # 運用中に入力された行(CSV に無い行)を足し、もう一度起動する。
    db_file = project / "instances" / "database.db"
    with closing(sqlite3.connect(db_file)) as conn, conn:
        conn.execute(
            "INSERT INTO orders (user_id, product_id, amount) VALUES (1, 1, 3)"
        )

    result = runner.invoke(app, ["runserver", "--import-csv-if-empty"])
    assert result.exit_code == 0, result.output
    assert _count(project, "orders") == seeded + 1  # CSV で上書きされない
    assert len(calls) == 2


def test_startproject_hint_uses_non_destructive_import(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["startproject", "demo"])
    assert result.exit_code == 0
    assert "runserver --import-csv-if-empty" in result.output
