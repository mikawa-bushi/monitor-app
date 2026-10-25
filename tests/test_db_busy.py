"""置換の取り込み中の書き込みのロック待ち(D4-06)。

- SQLite の置換は CSV をまず一時テーブルへ読み込み、本体の書き込みロックは入れ替えの
  間だけ取る(大きな CSV の解析中に画面・API の書き込みが busy_timeout で失敗しない)。
- ロック待ちの時間切れ(SQLite の database is locked、MySQL 1205/1213、PostgreSQL
  40001/40P01/55P03)は BUSY に分類し、API は 503 と Retry-After を返す(500 ではない)。
"""

from __future__ import annotations

import logging
import sqlite3

import pymysql
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

import monitor_app.services.importer as importer_mod
from monitor_app import MonitorConfig, TableDef
from monitor_app.api.errors import register_exception_handlers
from monitor_app.db import errors as db_errors
from monitor_app.db.engine import Database
from monitor_app.db.registry import TableRegistry
from monitor_app.services.importer import CsvImporter

STMT = "INSERT INTO t ..."


class _PgLike(Exception):
    def __init__(self, message: str, sqlstate: str) -> None:
        super().__init__(message)
        self.sqlstate = sqlstate


@pytest.mark.parametrize(
    "orig",
    [
        sqlite3.OperationalError("database is locked"),
        sqlite3.OperationalError("database table is locked"),
        pymysql.err.OperationalError(1205, "Lock wait timeout exceeded"),
        pymysql.err.OperationalError(1213, "Deadlock found"),
        _PgLike("could not obtain lock", "55P03"),
        _PgLike("deadlock detected", "40P01"),
        _PgLike("could not serialize access", "40001"),
    ],
    ids=lambda e: str(e)[:20],
)
def test_lock_timeouts_are_classified_as_busy(orig):
    assert db_errors.classify(OperationalError(STMT, {}, orig)) == db_errors.BUSY


def test_the_api_answers_busy_with_503_and_retry_after(caplog):
    app = FastAPI()
    register_exception_handlers(app)

    @app.post("/write")
    def _write():
        raise OperationalError(STMT, {}, sqlite3.OperationalError("database is locked"))

    with caplog.at_level(logging.INFO, logger="monitor_app.api"):
        res = TestClient(app, raise_server_exceptions=False).post("/write")
    assert res.status_code == 503
    assert res.headers["retry-after"] == "2"
    assert res.json()["detail"]["code"] == "DATABASE_BUSY"
    record = next(r for r in caplog.records if "DB busy" in r.getMessage())
    assert record.levelno == logging.WARNING and record.exc_info is None


# ---------------------------------------------------------------------------
# SQLite の置換は一時テーブルに読み込んでから入れ替える
# ---------------------------------------------------------------------------
@pytest.fixture
def env(tmp_path):
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()
    config = MonitorConfig(
        tables={"events": TableDef(columns={"id": "int", "v": "int"})}
    )
    db_file = tmp_path / "app.db"
    db = Database(f"sqlite:///{db_file.as_posix()}")
    registry = TableRegistry(config)
    registry.create_all(db)
    (csv_dir / "events.csv").write_text(
        "id,v\n" + "".join(f"{i},{i}\n" for i in range(1, 3001))
    )
    yield CsvImporter(config, registry, db, csv_dir), db, db_file
    db.dispose()


def _count(db) -> int:
    with db.connect() as conn:
        return conn.execute(text("SELECT COUNT(*) FROM events")).scalar()


def test_other_writers_can_commit_while_the_csv_is_being_read(env, monkeypatch):
    importer, db, db_file = env
    real = importer_mod.insert_in_chunks
    seen = []

    def write_meanwhile(conn, table, rows, **kw):
        # CSV を読んで変換している最中に、別の接続(画面・API)が書き込む
        other = sqlite3.connect(db_file, isolation_level=None, timeout=0)
        try:
            other.execute("BEGIN IMMEDIATE")
            other.execute("ROLLBACK")
            seen.append("committed")
        except sqlite3.OperationalError as exc:  # pragma: no cover - 修正前の動作
            seen.append(str(exc))
        finally:
            other.close()
        return real(conn, table, rows, **kw)

    monkeypatch.setattr(importer_mod, "insert_in_chunks", write_meanwhile)
    result = importer.import_table("events", importer.csv_dir / "events.csv")
    assert result.inserted == 3000 and _count(db) == 3000
    assert seen == ["committed"]


def test_replace_without_temp_tables_falls_back_to_one_transaction(env, monkeypatch):
    importer, db, _db_file = env

    def no_temp(conn, table):
        raise OperationalError("CREATE TEMP TABLE", {}, Exception("readonly tmp"))

    monkeypatch.setattr(importer_mod, "_stage_table", no_temp)
    assert importer.import_table("events", importer.csv_dir / "events.csv").inserted
    assert _count(db) == 3000


def test_temp_tables_are_dropped_after_the_import(env):
    importer, db, _db_file = env
    importer.import_all()
    importer.import_all()  # 2 回目も同じ名前の一時テーブルを作れる
    with db.engine.connect() as conn:
        leftover = conn.exec_driver_sql(
            "SELECT name FROM sqlite_temp_master WHERE type = 'table'"
        ).all()
    assert leftover == [] and _count(db) == 3000


def test_a_failed_staged_import_changes_nothing(env):
    importer, db, _db_file = env
    importer.import_all()
    (importer.csv_dir / "events.csv").write_text("id,v\n1,1\n1,2\n")
    with pytest.raises(Exception, match="主キー id=1"):
        importer.import_all()
    assert _count(db) == 3000
