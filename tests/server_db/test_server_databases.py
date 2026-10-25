"""PostgreSQL / MySQL の実サーバーでの動作確認(CRIT-01 ほか)。

雛形プロジェクトの 2 回目の置換取り込み・取り込み後のフォーム登録(連番)・float の
精度・ソース同期・長すぎる文字列の 422・スキーマのずれの報告と列の追加・インジェストの
重複時の扱い・保持期間の削除・起動時の DB 待ち・切れたコネクションの再接続を、
pg8000 / psycopg / PyMySQL の各ドライバで確かめる。実行方法は conftest.py を参照。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from contract import helpers
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DBAPIError
from typer.testing import CliRunner

from monitor_app import MonitorConfig, Retention, TableDef, ViewDef
from monitor_app.cli import app as cli_app
from monitor_app.db.bulk import insert_rows
from monitor_app.db.engine import Database
from monitor_app.db.registry import TableRegistry
from monitor_app.db.schema import (
    FLOAT_PRECISION,
    MISSING_COLUMN,
    apply_additive_migrations,
    inspect_schema,
)
from monitor_app.main import create_app
from monitor_app.services.retention import RetentionTask
from monitor_app.settings.declarative import SourceDef
from monitor_app.settings.loader import load_config
from monitor_app.settings.runtime import AppSettings

pytestmark = pytest.mark.server_db

SCAFFOLD_COUNTS = {"users": 5, "products": 3, "orders": 5}


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    helpers.isolate_from_host(monkeypatch, tmp_path)
    # CLI のテストが monitor_app を propagate=False にしていても caplog で拾う
    monkeypatch.setattr(logging.getLogger("monitor_app"), "propagate", True)


@pytest.fixture
def project(tmp_path, monkeypatch, server_url) -> Path:
    """雛形プロジェクト(DB はテスト用のサーバー DB)。"""
    proj = helpers.start_project(tmp_path)
    monkeypatch.chdir(proj)
    monkeypatch.setenv("MONITOR_DATABASE_URL", server_url)
    return proj


def _settings(url: str, **overrides) -> AppSettings:
    return AppSettings(_env_file=None, database_url=url, **overrides)


def _count(url: str, table: str) -> int:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()
    finally:
        engine.dispose()


def _cli(*args: str):
    return CliRunner().invoke(cli_app, list(args))


# ---------------------------------------------------------------------------
# 取り込み・連番
# ---------------------------------------------------------------------------
def test_scaffold_replace_import_can_run_twice(project, server_url):
    """外部キーを強制する DB でも、2 回目の置換取り込みが成功する(子から削除)。"""
    first = _cli("import-csv")
    assert first.exit_code == 0, first.output
    second = _cli("import-csv")
    assert second.exit_code == 0, second.output
    for table, expected in SCAFFOLD_COUNTS.items():
        assert _count(server_url, table) == expected, table


def test_form_posts_after_an_explicit_id_import_get_new_ids(project, server_url):
    """CSV の id を入れた後の画面からの登録が、連番の衝突(500)にならない。"""
    assert _cli("import-csv").exit_code == 0
    client = TestClient(create_app(load_config("config.py"), _settings(server_url)))
    ids = []
    for amount in range(5):
        res = client.post(
            "/api/tables/orders",
            json={"user_id": 1, "product_id": 2, "amount": amount},
        )
        assert res.status_code == 201, res.text
        ids.append(res.json()["data"]["id"])
    assert ids == [6, 7, 8, 9, 10]

    # インジェストで id を明示した後も、続きから採番される。
    res = client.post(
        "/api/ingest/orders",
        json={"id": 50, "user_id": 1, "product_id": 1, "amount": 1},
    )
    assert res.status_code == 200, res.text
    res = client.post(
        "/api/tables/orders", json={"user_id": 1, "product_id": 1, "amount": 2}
    )
    assert res.status_code == 201, res.text
    assert res.json()["data"]["id"] == 51


def test_every_scaffold_view_and_kpi_runs(project, server_url):
    assert _cli("import-csv").exit_code == 0
    config = load_config("config.py")
    client = TestClient(create_app(config, _settings(server_url)))
    for view in config.views:
        res = client.get(f"/api/views/{view}")
        assert res.status_code == 200, f"{view}: {res.text[:300]}"
    kpis = client.get("/api/kpis").json()["kpis"]
    assert all(k["value"] is not None for k in kpis), kpis


def test_float_round_trip_is_exact(server_url):
    """float 列は倍精度(MySQL の単精度 FLOAT で値が丸められない)。"""
    config = MonitorConfig(
        tables={"m": TableDef(columns={"id": "int", "v": "float"}, primary_key="id")}
    )
    client = TestClient(create_app(config, _settings(server_url)))
    values = [1234567.89, 1761036123.5, 0.123456789]
    rows = [{"id": i + 1, "v": v} for i, v in enumerate(values)]
    assert client.post("/api/ingest/m", json=rows).status_code == 200
    data = client.get("/api/tables/m").json()["data"]
    assert [row["v"] for row in sorted(data, key=lambda r: r["id"])] == values


def test_sources_sync(project, server_url):
    """ソースの取り込みカーソル表(_ingest_state)がサーバー DB でも作れる。"""
    events = project / "events.jsonl"
    events.write_text(
        "".join(
            json.dumps({"ts": f"2026-10-21T08:0{i}:00", "v": i}) + "\n"
            for i in range(3)
        ),
        encoding="utf-8",
    )
    (project / "config.py").write_text(
        "from monitor_app import MonitorConfig, TableDef\n"
        "from monitor_app.settings.declarative import SourceDef\n"
        "config = MonitorConfig(\n"
        "    tables={'ev': TableDef(columns={'id': 'int', 'ts': 'str', 'v': 'float'})},\n"
        f"    sources={{'ev': SourceDef(kind='jsonl', path={str(events)!r},"
        " table='ev')},\n"
        ")\n",
        encoding="utf-8",
    )
    first = _cli("sync-sources")
    assert first.exit_code == 0, first.output
    assert "ev: 3 行" in first.output
    second = _cli("sync-sources")
    assert second.exit_code == 0, second.output
    assert _count(server_url, "ev") == 3
    assert _count(server_url, "_ingest_state") == 1


def test_too_long_string_is_a_422_json_error(server_url):
    config = MonitorConfig(
        tables={"u": TableDef(columns={"id": "int", "name": "str"}, primary_key="id")}
    )
    client = TestClient(
        create_app(config, _settings(server_url)), raise_server_exceptions=False
    )
    long_name = "x" * 300
    for res in (
        client.post("/api/tables/u", json={"name": long_name}),
        client.post("/api/ingest/u", json={"id": 1, "name": long_name}),
    ):
        assert res.status_code == 422, res.text
        assert res.headers["content-type"].startswith("application/json")
        body = res.json()["detail"]
        assert body["code"] == "INVALID_PAYLOAD"
        assert "255" in body["message"]
        assert "INSERT" not in body["message"]  # SQL を漏らさない


def test_deleting_a_referenced_row_is_a_409(project, server_url):
    assert _cli("import-csv").exit_code == 0
    client = TestClient(
        create_app(load_config("config.py"), _settings(server_url)),
        raise_server_exceptions=False,
    )
    res = client.delete("/api/tables/users/1")  # orders から参照されている
    assert res.status_code == 409, res.text
    assert res.json()["detail"]["code"] == "CONFLICT"
    assert "外部キー" in res.json()["detail"]["message"]


# ---------------------------------------------------------------------------
# インジェストの重複時の扱い(insert_rows の方言ごとの経路)
# ---------------------------------------------------------------------------
def test_ingest_on_conflict_modes(server_url):
    config = MonitorConfig(
        tables={"s": TableDef(columns={"id": "int", "v": "float"}, primary_key="id")}
    )
    client = TestClient(
        create_app(config, _settings(server_url)), raise_server_exceptions=False
    )
    batch = [{"id": i, "v": i} for i in range(1, 11)]
    assert client.post("/api/ingest/s", json=batch).json() == {
        "success": True,
        "inserted": 10,
    }

    retry = [*batch, {"id": 11, "v": 11}]
    res = client.post("/api/ingest/s", json=retry)
    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "CONFLICT"
    assert "1, 2, 3" in res.json()["detail"]["message"]

    res = client.post("/api/ingest/s", params={"on_conflict": "ignore"}, json=retry)
    assert res.json() == {"success": True, "inserted": 1, "skipped": 10}

    upd = [{"id": 1, "v": 100}, {"id": 12, "v": 12}, {"id": 1, "v": 101}]
    res = client.post("/api/ingest/s", params={"on_conflict": "update"}, json=upd)
    assert res.json() == {"success": True, "inserted": 1, "updated": 2}
    rows = {r["id"]: r["v"] for r in client.get("/api/tables/s").json()["data"]}
    assert len(rows) == 12
    assert rows[1] == 101.0  # 同じバッチ内では最後の値


def test_insert_rows_counts_on_each_driver(server_url):
    config = MonitorConfig(
        tables={"c": TableDef(columns={"id": "int", "name": "str"}, primary_key="id")}
    )
    registry = TableRegistry(config)
    db = Database(server_url)
    try:
        registry.create_all(db)
        table = registry.get("c")
        with db.connect() as conn:
            assert insert_rows(conn, table, [{"id": i} for i in range(1, 6)]) == 5
            rows = [{"id": i, "name": f"n{i}"} for i in range(3, 9)]
            assert insert_rows(conn, table, rows, on_conflict="ignore", chunk=2) == 3
            rows = [{"id": 8, "name": "new"}, {"id": 9, "name": "nine"}]
            assert insert_rows(conn, table, rows, on_conflict="update") == 1
            # 主キーなし(None)の行は採番される(PostgreSQL でも NOT NULL 違反にせず、
            # 明示した id の続きから採番する)
            assert insert_rows(conn, table, [{"id": None, "name": "auto"}]) == 1
            mixed = [{"id": 20, "name": "x"}, {"name": "auto2"}, {"id": 30}]
            assert insert_rows(conn, table, mixed) == 3
            assert insert_rows(conn, table, [{"name": "auto3"}]) == 1
        with db.connect() as conn:
            names = dict(conn.execute(text("SELECT id, name FROM c")).all())
        assert names[8] == "new" and names[9] == "nine"
        assert names[10] == "auto" and names[21] == "auto2" and names[31] == "auto3"
        assert len(names) == 14
    finally:
        db.dispose()


# ---------------------------------------------------------------------------
# スキーマのずれ
# ---------------------------------------------------------------------------
def test_schema_drift_report_and_additive_migration(server_url):
    old = MonitorConfig(tables={"users": TableDef(columns=["id", "name"])})
    db = Database(server_url)
    try:
        TableRegistry(old).create_all(db)
        new = MonitorConfig(tables={"users": TableDef(columns=["id", "name", "dept"])})
        registry = TableRegistry(new)
        report = inspect_schema(db, registry)
        assert [(p.kind, p.column) for p in report.problems] == [
            (MISSING_COLUMN, "dept")
        ]
        client = TestClient(
            create_app(new, _settings(server_url)), raise_server_exceptions=False
        )
        res = client.get("/api/tables/users")
        assert res.status_code == 500
        assert res.json()["detail"]["code"] == "DB_SCHEMA_MISMATCH"

        assert apply_additive_migrations(db, registry, report)
        assert inspect_schema(db, registry).problems == []
        assert client.get("/api/tables/users").status_code == 200
    finally:
        db.dispose()


def test_single_precision_float_is_reported_on_mysql(server_url):
    if not server_url.startswith("mysql"):
        pytest.skip("MySQL だけの確認")
    engine = create_engine(server_url)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE p (id INTEGER PRIMARY KEY, price FLOAT)"))
    engine.dispose()
    config = MonitorConfig(
        tables={"p": TableDef(columns={"id": "int", "price": "float"})}
    )
    db = Database(server_url)
    try:
        report = inspect_schema(db, TableRegistry(config))
        (problem,) = report.problems
        assert problem.kind == FLOAT_PRECISION
        assert "ALTER TABLE p MODIFY price DOUBLE" in problem.message
        assert problem.fix_sql == "ALTER TABLE p MODIFY price DOUBLE"
    finally:
        db.dispose()


# ---------------------------------------------------------------------------
# 保持期間
# ---------------------------------------------------------------------------
def test_retention_deletes_in_batches(server_url):
    now = datetime(2026, 10, 21, 12, 0, 0)
    config = MonitorConfig(
        tables={
            "r": TableDef(
                columns={"id": "int", "ts": "datetime", "v": "float"},
                retention=Retention(keep_days=1, column="ts"),
            ),
            "k": TableDef(
                columns={"id": "int", "v": "float"},
                retention=Retention(keep_rows=3),
            ),
        }
    )
    registry = TableRegistry(config)
    db = Database(server_url)
    try:
        registry.create_all(db)
        with db.connect() as conn:
            insert_rows(
                conn,
                registry.get("r"),
                [{"id": i, "ts": now - timedelta(hours=i), "v": i} for i in range(48)],
            )
            insert_rows(conn, registry.get("k"), [{"id": i} for i in range(1, 11)])
        task = RetentionTask(config, registry, db, 60, batch_size=7, clock=lambda: now)
        assert task.purge_once() == {"r": 23, "k": 7}
        with db.connect() as conn:
            assert conn.execute(text("SELECT MAX(id) FROM r")).scalar() == 24
            assert conn.execute(text("SELECT MIN(id) FROM k")).scalar() == 8
    finally:
        db.dispose()


# ---------------------------------------------------------------------------
# 接続(起動時の待ち・切れたコネクション)
# ---------------------------------------------------------------------------
def test_wait_until_ready(server_url):
    db = Database(server_url)
    try:
        db.wait_until_ready(5)  # 起動済みならすぐ返る
    finally:
        db.dispose()

    from sqlalchemy.engine import make_url

    wrong_password = make_url(server_url).set(password="wrong-password")
    db = Database(wrong_password.render_as_string(hide_password=False))
    started = datetime.now()
    try:
        # 認証の失敗は待たずに失敗する(PyMySQL 1.1 は cryptography が無いと MySQL 8 の
        # caching_sha2_password で誤ったパスワードを RuntimeError にする)
        with pytest.raises((DBAPIError, RuntimeError)):
            db.wait_until_ready(30)
    finally:
        db.dispose()
    assert datetime.now() - started < timedelta(seconds=10)


def test_pool_pre_ping_replaces_connections_killed_by_the_server(server_url):
    db = Database(server_url)
    try:
        with db.connect() as conn:
            if db.dialect == "postgresql":
                pid = conn.execute(text("SELECT pg_backend_pid()")).scalar()
            else:
                pid = conn.execute(text("SELECT CONNECTION_ID()")).scalar()
        killer = create_engine(server_url)
        with killer.begin() as conn:
            if db.dialect == "postgresql":
                conn.execute(text("SELECT pg_terminate_backend(:p)"), {"p": pid})
            else:
                conn.execute(text(f"KILL {int(pid)}"))
        killer.dispose()
        # プールに残った切断済みのコネクションは貸し出し前の確認で作り直される
        with db.connect() as conn:
            assert conn.execute(text("SELECT 1")).scalar() == 1
    finally:
        db.dispose()


def test_startup_on_an_existing_database_reports_no_drift(server_url, caplog):
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
        },
        views={"v": ViewDef(query="SELECT * FROM a")},
    )
    create_app(config, _settings(server_url)).state.services.close()
    caplog.clear()
    services = create_app(config, _settings(server_url)).state.services
    services.close()
    drift = [r for r in caplog.records if "表定義のずれ" in r.getMessage()]
    assert drift == []
    tables = inspect(create_engine(server_url)).get_table_names()
    assert "a" in tables


def test_sources_config_with_sqlite_source(server_url, tmp_path):
    """sqlite ソース(ウォーターマーク付き)もサーバー DB へ取り込める。"""
    import sqlite3

    src = tmp_path / "src.db"
    conn = sqlite3.connect(src)
    with conn:  # with はコミットだけで閉じないため、下で close する
        conn.execute("CREATE TABLE m (id INTEGER PRIMARY KEY, v REAL)")
        conn.executemany("INSERT INTO m VALUES (?, ?)", [(1, 1.5), (2, 2.5)])
    conn.close()
    config = MonitorConfig(
        tables={"m": TableDef(columns={"id": "int", "v": "float"})},
        sources={
            "m": SourceDef(
                kind="sqlite",
                path=str(src),
                table="m",
                source_table="m",
                watermark_column="id",
            )
        },
    )
    services = create_app(config, _settings(server_url)).state.services
    try:
        assert services.connectors["m"].poll_once() == 2
        assert services.connectors["m"].poll_once() == 0
    finally:
        services.close()
    assert _count(server_url, "m") == 2


def test_explicit_zero_id_is_kept(server_url):
    """主キー 0 を明示した行が、そのまま 0 で保存される(MySQL でも採番されない)。"""
    config = MonitorConfig(
        tables={"z": TableDef(columns={"id": "int", "v": "float"}, primary_key="id")}
    )
    client = TestClient(create_app(config, _settings(server_url)))
    rows = [{"id": 0, "v": 0.5}, {"id": 1, "v": 1.5}]
    assert client.post("/api/ingest/z", json=rows).json()["inserted"] == 2
    data = client.get("/api/tables/z").json()["data"]
    assert sorted((r["id"], r["v"]) for r in data) == [(0, 0.5), (1, 1.5)]
