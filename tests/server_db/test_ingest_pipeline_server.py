"""取り込みの流れ(モード・1 トランザクション・文字コードの読み直し・ヘッダの検査・
追記監視・ソースのカーソル・日時の列)を PostgreSQL / MySQL の実サーバーで確かめる。

外部キーを強制する DB で、置換・追記・if-empty の再実行、途中の失敗の取り消し
(SAVEPOINT を含む)、有効行 0 件のテーブルを残した置換のやり直しが正しく動くこと。
実行方法は conftest.py を参照。
"""

from __future__ import annotations

import logging
import sqlite3

import pytest
from contract import helpers
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from typer.testing import CliRunner

import monitor_app.services.importer as importer_mod
from monitor_app import MonitorConfig, TableDef
from monitor_app.cli import app as cli_app
from monitor_app.db import errors as db_errors
from monitor_app.db.engine import Database
from monitor_app.db.ingest_state import IngestStateStore
from monitor_app.db.registry import TableRegistry
from monitor_app.exceptions import ConflictError
from monitor_app.services.connectors import SqliteSourceConnector
from monitor_app.services.importer import CsvImporter, TailState
from monitor_app.settings.declarative import SourceDef

pytestmark = pytest.mark.server_db

CSV = {
    "users": "id,name,email\n1,a,a@x\n2,b,b@x\n",
    "products": "id,name,price\n1,apple,100\n2,banana,50\n",
    "orders": "id,user_id,product_id,amount\n1,1,1,2\n2,2,2,1\n3,1,2,5\n",
}


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    helpers.isolate_from_host(monkeypatch, tmp_path)
    monkeypatch.setattr(logging.getLogger("monitor_app"), "propagate", True)


def _config() -> MonitorConfig:
    return MonitorConfig(
        tables={
            "orders": TableDef(
                columns=["id", "user_id", "product_id", "amount"],
                foreign_keys={"user_id": "users.id", "product_id": "products.id"},
            ),
            "users": TableDef(columns=["id", "name", "email"]),
            "products": TableDef(columns=["id", "name", "price"]),
        }
    )


@pytest.fixture
def env(tmp_path, server_url):
    config = _config()
    db = Database(server_url)
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()
    for name, body in CSV.items():
        (csv_dir / f"{name}.csv").write_text(body, encoding="utf-8")
    registry = TableRegistry(config)
    yield config, registry, db, csv_dir
    db.dispose()


def _counts(db) -> dict:
    with db.connect() as conn:
        return {
            t: conn.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar()
            for t in ("users", "products", "orders")
        }


def _names(db, table):
    with db.connect() as conn:
        return [
            r[0] for r in conn.execute(text(f"SELECT name FROM {table} ORDER BY id"))
        ]


def test_every_mode_can_be_rerun_with_enforced_foreign_keys(env):
    config, registry, db, csv_dir = env
    importer = CsvImporter(config, registry, db, csv_dir)
    for kwargs in ({}, {}, {"keep": True}, {"mode": "if-empty"}, {"mode": "replace"}):
        importer.import_all(**kwargs)
        assert _counts(db) == {"users": 2, "products": 2, "orders": 3}, kwargs
    report = importer.import_all(keep=True)
    assert report.total_inserted == 0 and report.total_duplicates == 7


def test_zero_valid_rows_keeps_the_table_and_replaces_the_rest(env):
    config, registry, db, csv_dir = env
    importer = CsvImporter(config, registry, db, csv_dir)
    importer.import_all()
    (csv_dir / "users.csv").write_text("id,name,email\n1,A,A@x\n2,B,B@x\n")
    (csv_dir / "products.csv").write_text("id,name,price\n")
    report = importer.import_all()
    assert {r.table: r.inserted for r in report.results} == {
        "orders": 3,
        "users": 2,
        "products": 0,
    }
    assert _names(db, "users") == ["A", "B"]
    assert _names(db, "products") == ["apple", "banana"]


def test_refused_replace_keeps_rows(env):
    config, registry, db, csv_dir = env
    importer = CsvImporter(config, registry, db, csv_dir)
    importer.import_all()
    (csv_dir / "users.csv").write_text("id,name\n1,X\n2,Y\n")
    report = importer.import_all()
    assert next(r for r in report.results if r.table == "users").refused
    assert _names(db, "users") == ["a", "b"]


def test_a_failure_after_savepoints_rolls_back_everything(env):
    """追記の SAVEPOINT(文字コードの読み直し用)を使っても全体は 1 トランザクション。"""
    config, registry, db, csv_dir = env
    importer = CsvImporter(config, registry, db, csv_dir)
    importer.import_all()
    (csv_dir / "users.csv").write_text(CSV["users"] + "3,c,c@x\n")
    (csv_dir / "orders.csv").write_text(
        "id,user_id,product_id,amount\n9,99,1,1\n"  # 存在しない user_id
    )
    # CSV のファイル名と行番号つきの ConflictError(元のドライバの例外は __cause__ —
    # pg8000 は ProgrammingError で返す)
    with pytest.raises(ConflictError, match=r"orders\.csv 2行目") as info:
        importer.import_all(keep=True)
    assert isinstance(info.value.__cause__, DBAPIError)
    assert db_errors.classify(info.value.__cause__) == db_errors.CONFLICT
    assert _counts(db) == {"users": 2, "products": 2, "orders": 3}


def test_late_decode_errors_reread_with_the_fallback(env, monkeypatch):
    config, registry, db, csv_dir = env
    monkeypatch.setattr(importer_mod, "INSERT_CHUNK", 200)
    importer = CsvImporter(config, registry, db, csv_dir)
    importer.import_all()
    body = "id,name,email\n1,ﾃｽ,x\n2,b,b@x\n" + "".join(
        f"{i},name-{i:06d},e{i}@x\n" for i in range(3, 3000)
    )
    (csv_dir / "users.csv").write_bytes((body + "3000,漢字,k@x\n").encode("cp932"))
    report = importer.import_all(keep=True)
    users = next(r for r in report.results if r.table == "users")
    assert (users.encoding, users.inserted, users.duplicates) == ("cp932", 2998, 2)
    names = _names(db, "users")
    assert names[0] == "a" and names[-1] == "漢字" and len(names) == 3000


def test_cli_import_csv_modes_on_the_scaffold(tmp_path, monkeypatch, server_url):
    proj = helpers.start_project(tmp_path)
    monkeypatch.chdir(proj)
    monkeypatch.setenv("MONITOR_DATABASE_URL", server_url)
    runner = CliRunner()
    for args in (["import-csv"], ["import-csv", "--keep"], ["import-csv", "--keep"]):
        result = runner.invoke(cli_app, args)
        assert result.exit_code == 0, result.output


def test_tailer_appends_into_a_server_database(tmp_path, server_url):
    config = MonitorConfig(
        tables={"m": TableDef(columns={"id": "int", "ts": "datetime", "v": "float"})}
    )
    db = Database(server_url)
    registry = TableRegistry(config)
    registry.create_all(db)
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()
    path = csv_dir / "m.csv"
    path.write_bytes(b"id,ts,v\n1,2026/10/21 8:00,1.5\n")
    importer = CsvImporter(config, registry, db, csv_dir)
    state = TailState()
    assert importer.tail_append("m", path, state) == 1
    with path.open("ab") as fh:
        fh.write(b"2,2026/10/21 10:00,2.5\n3,2026/10/21 9:00,3")
    assert importer.tail_append("m", path, state) == 1  # 3 行目は書きかけ
    with path.open("ab") as fh:
        fh.write(b".5\n")
    assert importer.tail_append("m", path, state) == 1
    path.write_bytes(b"id,ts,v\n1,2026/10/21 8:00,1.5\n4,2026-10-21T11:00:00,4\n")
    assert importer.tail_append("m", path, state) == 1  # 縮小 → 先頭から(1 は既存)
    with db.connect() as conn:
        ids = conn.execute(text("SELECT id FROM m ORDER BY ts")).scalars().all()
        assert ids == [1, 3, 2, 4]
    db.dispose()


def test_connector_cursor_is_typed_and_transactional(tmp_path, server_url):
    src = tmp_path / "src.db"
    conn = sqlite3.connect(src)
    conn.execute("CREATE TABLE s (id, ts, v)")
    conn.executemany("INSERT INTO s VALUES (?, ?, ?)", [(1, 10, 1.0), (2, None, 2.0)])
    conn.commit()
    sdef = SourceDef(
        kind="sqlite",
        path=str(src),
        table="events",
        source_table="s",
        watermark_column="ts",
    )
    config = MonitorConfig(
        tables={"events": TableDef(columns={"id": "int", "ts": "int", "v": "float"})},
        sources={"src": sdef},
    )
    db = Database(server_url)
    registry = TableRegistry(config)
    registry.create_all(db)
    state = IngestStateStore(db)
    state.create()
    connector = SqliteSourceConnector("src", sdef, config, registry, db, state)
    assert connector.poll_once() == 1
    assert state.get("src") == '{"v":10}'
    conn.execute("INSERT INTO s VALUES (3, 11, 3.0)")
    conn.commit()
    conn.close()
    assert connector.poll_once() == 1
    assert connector.poll_once() == 0
    engine = create_engine(server_url)
    try:
        with engine.connect() as c:
            assert c.execute(
                text("SELECT id FROM events ORDER BY id")
            ).scalars().all() == [
                1,
                3,
            ]
    finally:
        engine.dispose()
    db.dispose()


def test_if_empty_cli_keeps_entered_rows(tmp_path, monkeypatch, server_url):
    proj = helpers.start_project(tmp_path)
    monkeypatch.chdir(proj)
    monkeypatch.setenv("MONITOR_DATABASE_URL", server_url)
    monkeypatch.setattr("uvicorn.run", lambda *a, **k: None)
    runner = CliRunner()
    assert runner.invoke(cli_app, ["runserver", "--import-csv-if-empty"]).exit_code == 0
    engine = create_engine(server_url)
    try:
        with engine.begin() as c:
            c.execute(
                text(
                    "INSERT INTO orders (user_id, product_id, amount) VALUES (1, 1, 3)"
                )
            )
        result = runner.invoke(cli_app, ["runserver", "--import-csv-if-empty"])
        assert result.exit_code == 0, result.output
        with engine.connect() as c:
            assert c.execute(text("SELECT COUNT(*) FROM orders")).scalar() == 6
    finally:
        engine.dispose()
