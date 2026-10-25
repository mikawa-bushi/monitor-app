"""datetime 列の保存形式(SQLite は秒まで・小数秒は 0 でないときだけ)と寛容な読み出し。

- 表示・ビュー・グラフの軸に ``.000000`` を出さない(新しい保存形式)。
- 文字列の大小と時刻の前後が一致する(生 SQL の ORDER BY・範囲の絞り込み・保持期間)。
- 以前の版の文字列の列を datetime に変えても、一覧の API が 500 にならない(D3-04)。
- MySQL は DATETIME(6)(小数秒を丸めない — D3-05。実サーバーでの確認は
  tests/server_db/test_datetime_server.py)。
"""

from __future__ import annotations

import datetime as dt
import random
import sqlite3
from contextlib import closing

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert, select, text
from sqlalchemy.dialects import mysql, postgresql
from sqlalchemy.dialects import sqlite as sqlite_dialect
from sqlalchemy.exc import StatementError
from sqlalchemy.schema import CreateTable

from monitor_app import MonitorConfig, Retention, TableDef, ViewDef
from monitor_app.db.bulk import insert_rows
from monitor_app.db.engine import Database
from monitor_app.db.registry import TableRegistry
from monitor_app.db.schema import TYPE_MISMATCH, inspect_schema, type_family
from monitor_app.db.types import format_sqlite_datetime, parse_stored_datetime
from monitor_app.main import create_app
from monitor_app.services.retention import RetentionTask
from monitor_app.settings.runtime import AppSettings

T0 = dt.datetime(2026, 10, 21, 8, 0, 0)


def _config(**extra) -> MonitorConfig:
    return MonitorConfig(
        tables={"m": TableDef(columns={"id": "int", "ts": "datetime", "v": "float"})},
        views={
            "by_time": ViewDef(query="SELECT id, ts FROM m ORDER BY ts", title="t"),
            "recent": ViewDef(
                query="SELECT id FROM m WHERE ts >= '2026-10-21 08:00:00' ORDER BY id",
                title="r",
            ),
        },
        **extra,
    )


def _raw(path, sql="SELECT id, ts FROM m ORDER BY id"):
    with closing(sqlite3.connect(path)) as conn, conn:
        return conn.execute(sql).fetchall()


@pytest.fixture
def sqlite_db(tmp_path):
    path = tmp_path / "app.db"
    db = Database(f"sqlite:///{path.as_posix()}")
    registry = TableRegistry(_config())
    registry.create_all(db)
    yield db, registry, path
    db.dispose()


# ---------------------------------------------------------------------------
# 保存形式
# ---------------------------------------------------------------------------
def test_sqlite_stores_seconds_and_only_nonzero_fractions(sqlite_db):
    db, registry, path = sqlite_db
    with db.connect() as conn:
        insert_rows(
            conn,
            registry.get("m"),
            [
                {"id": 1, "ts": T0},
                {
                    "id": 2,
                    "ts": T0.replace(hour=23, minute=59, second=59, microsecond=700000),
                },
                {"id": 3, "ts": T0.replace(microsecond=1)},
            ],
        )
    assert _raw(path) == [
        (1, "2026-10-21 08:00:00"),
        (2, "2026-10-21 23:59:59.700000"),
        (3, "2026-10-21 08:00:00.000001"),
    ]
    with db.readonly() as conn:
        values = conn.execute(select(registry.get("m").c.ts)).scalars().all()
    assert values == [
        T0,
        T0.replace(hour=23, minute=59, second=59, microsecond=700000),
        T0.replace(microsecond=1),
    ]


def test_text_order_matches_time_order():
    """秒までの形と小数秒つきの形が混ざっても、文字列の大小 = 時刻の前後。"""
    rng = random.Random(7)
    values = [
        T0
        + dt.timedelta(
            seconds=rng.randint(0, 5), microseconds=rng.choice([0, 0, 1, 500000])
        )
        for _ in range(300)
    ]
    assert sorted(values, key=format_sqlite_datetime) == sorted(values)
    for a in values[:50]:
        for b in values[:50]:
            assert (format_sqlite_datetime(a) < format_sqlite_datetime(b)) == (a < b)


def test_aware_dates_and_strings_are_bound_in_the_same_form(sqlite_db):
    db, registry, path = sqlite_db
    table = registry.get("m")
    utc = dt.datetime(2026, 10, 21, 8, 0, tzinfo=dt.timezone.utc)
    with db.connect() as conn:
        conn.execute(insert(table), [{"id": 1, "ts": utc}])
        conn.execute(insert(table), [{"id": 2, "ts": dt.date(2026, 10, 22)}])
        conn.execute(insert(table), [{"id": 3, "ts": "2026/10/23 8:05"}])
    local = utc.astimezone().replace(tzinfo=None)
    assert _raw(path) == [
        (1, format_sqlite_datetime(local)),
        (2, "2026-10-22 00:00:00"),
        (3, "2026-10-23 08:05:00"),
    ]
    with pytest.raises(StatementError), db.connect() as conn:
        conn.execute(insert(table), [{"id": 4, "ts": "not a date"}])


def test_views_tables_and_exports_show_no_microsecond_padding(tmp_path):
    path = tmp_path / "app.db"
    app = create_app(
        _config(), AppSettings(database_url=f"sqlite:///{path.as_posix()}")
    )
    with TestClient(app) as client:
        res = client.post(
            "/api/ingest/m",
            json=[
                {"id": 1, "ts": "2026-10-21T08:00:00", "v": 1},
                {"id": 2, "ts": "2026/10/21 7:30", "v": 2},
                {"id": 3, "ts": "2026-10-21T09:15:30.250", "v": 3},
            ],
        )
        assert res.status_code == 200, res.text
        view = client.get("/api/views/by_time").json()
        recent = client.get("/api/views/recent").json()
        table = client.get("/api/tables/m").json()
        export = client.get("/api/views/by_time/export").text
        one = client.get("/api/tables/m/2026-10-21 08:00:00")
    assert [(r["id"], r["ts"]) for r in view["data"]] == [
        (2, "2026-10-21 07:30:00"),
        (1, "2026-10-21 08:00:00"),
        (3, "2026-10-21 09:15:30.250000"),
    ]
    assert [r["id"] for r in recent["data"]] == [1, 3]
    assert [r["ts"] for r in table["data"]] == [
        "2026-10-21T08:00:00",
        "2026-10-21T07:30:00",
        "2026-10-21T09:15:30.250000",
    ]
    assert ".000000" not in export
    assert "2026-10-21 07:30:00" in export
    assert one.status_code == 404  # 主キーは id(日時の主キーは下のテスト)


def test_datetime_primary_key_lookup_and_resend(tmp_path):
    config = MonitorConfig(
        tables={
            "s": TableDef(columns={"ts": "datetime", "v": "float"}, primary_key="ts")
        }
    )
    path = tmp_path / "app.db"
    app = create_app(config, AppSettings(database_url=f"sqlite:///{path.as_posix()}"))
    batch = [
        {"ts": "2026-10-21T08:00:00", "v": 1},
        {"ts": "2026-10-21T08:00:00.5", "v": 2},
    ]
    with TestClient(app) as client:
        first = client.post("/api/ingest/s?on_conflict=ignore", json=batch).json()
        again = client.post("/api/ingest/s?on_conflict=ignore", json=batch).json()
        got = client.get("/api/tables/s/2026-10-21T08:00:00.500")
    assert (first["inserted"], first["skipped"]) == (2, 0)
    assert (again["inserted"], again["skipped"]) == (0, 2)
    assert got.status_code == 200
    assert got.json()["data"]["v"] == 2


def test_retention_on_datetime_compares_mixed_forms_correctly(sqlite_db):
    db, registry, path = sqlite_db
    config = MonitorConfig(
        tables={
            "m": TableDef(
                columns={"id": "int", "ts": "datetime", "v": "float"},
                retention=Retention(keep_days=1, column="ts"),
            )
        }
    )
    now = dt.datetime(2026, 10, 22, 8, 0, 0, 500000)
    with db.connect() as conn:
        insert_rows(
            conn,
            registry.get("m"),
            [
                {"id": 1, "ts": T0},  # 1 日と 0.5 秒前 → 消す
                {"id": 2, "ts": T0.replace(microsecond=500000)},  # ちょうど境界 → 残す
                {"id": 3, "ts": T0.replace(microsecond=600000)},  # 新しい → 残す
                {"id": 4, "ts": None},
            ],
        )
    task = RetentionTask(config, registry, db, 60, clock=lambda: now)
    assert task.purge_once() == {"m": 1}
    assert [r[0] for r in _raw(path)] == [2, 3, 4]


# ---------------------------------------------------------------------------
# 以前の版の文字列の列を datetime に変えたとき(D3-04)
# ---------------------------------------------------------------------------
def test_legacy_text_values_are_read_leniently(tmp_path):
    path = tmp_path / "app.db"
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(
            "CREATE TABLE logs (id INTEGER NOT NULL PRIMARY KEY, ts VARCHAR(255))"
        )
        conn.executemany(
            "INSERT INTO logs VALUES (?, ?)",
            [
                (1, "2026/10/21 8:00"),
                (2, "2026-10-21T09:00:00"),
                (3, "2026-10-21 10:00:00.000000"),
                (4, "n/a"),
                (5, None),
                (6, 1761000000),
            ],
        )
    config = MonitorConfig(
        tables={"logs": {"columns": {"id": "int", "ts": "datetime"}}}
    )
    app = create_app(config, AppSettings(database_url=f"sqlite:///{path.as_posix()}"))
    with TestClient(app, raise_server_exceptions=False) as client:
        res = client.get("/api/tables/logs")
        one = client.get("/api/tables/logs/1")
    assert res.status_code == 200, res.text
    assert [r["ts"] for r in res.json()["data"]] == [
        "2026-10-21T08:00:00",
        "2026-10-21T09:00:00",
        "2026-10-21T10:00:00",
        "n/a",
        None,
        "1761000000",  # 文字列の列(TEXT)に入った数値は文字列のまま
    ]
    assert one.status_code == 200


def test_parse_stored_datetime_handles_offsets_and_garbage():
    utc = parse_stored_datetime("2026-10-21T08:00:00Z")
    expected = dt.datetime(2026, 10, 21, 8, tzinfo=dt.timezone.utc).astimezone()
    assert utc == expected.replace(tzinfo=None)
    assert parse_stored_datetime("2026-10-21") == dt.datetime(2026, 10, 21)
    assert parse_stored_datetime("??") == "??"
    assert parse_stored_datetime(None) is None
    assert parse_stored_datetime(T0) is T0


def test_type_mismatch_from_text_explains_existing_values(tmp_path):
    path = tmp_path / "app.db"
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(
            "CREATE TABLE logs (id INTEGER NOT NULL PRIMARY KEY, ts VARCHAR(255))"
        )
    db = Database(f"sqlite:///{path.as_posix()}")
    config = MonitorConfig(
        tables={"logs": {"columns": {"id": "int", "ts": "datetime"}}}
    )
    report = inspect_schema(db, TableRegistry(config))
    db.dispose()
    [problem] = report.problems
    assert problem.kind == TYPE_MISMATCH
    assert "表を作り直すか" in problem.message
    assert "2026-10-21 08:00:00" in problem.message
    assert "import-csv" in problem.message


# ---------------------------------------------------------------------------
# DB ごとの型
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("dialect", "ddl"),
    [
        (sqlite_dialect.dialect(), "ts DATETIME"),
        (mysql.dialect(), "ts DATETIME(6)"),
        (postgresql.dialect(), "ts TIMESTAMP WITHOUT TIME ZONE"),
    ],
)
def test_datetime_ddl_per_database(dialect, ddl):
    table = TableRegistry(_config()).get("m")
    assert ddl in str(CreateTable(table).compile(dialect=dialect))
    assert type_family(table.c.ts.type) == "datetime"


def test_columns_created_by_the_previous_format_still_work(sqlite_db):
    """SQLAlchemy の既定の形(.000000 付き)で書かれた値も読め、範囲の比較も正しい。"""
    db, registry, path = sqlite_db
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("INSERT INTO m (id, ts) VALUES (1, '2026-10-21 08:00:00.000000')")
        conn.execute("INSERT INTO m (id, ts) VALUES (2, '2026-10-21 07:59:59.999999')")
    table = registry.get("m")
    with db.readonly() as conn:
        values = dict(conn.execute(select(table.c.id, table.c.ts)).all())
        older = conn.execute(
            select(table.c.id).where(table.c.ts < T0).order_by(table.c.id)
        ).scalars()
        assert list(older) == [2]
    assert values == {1: T0, 2: T0 - dt.timedelta(microseconds=1)}
    with db.readonly() as conn:
        assert conn.execute(text("SELECT id FROM m ORDER BY ts")).scalars().all() == [
            2,
            1,
        ]
