"""v2.3.1 の利用者のプロジェクトと DB を、現行コードでそのまま使い続けられることの検証。

``tests/contract/fixtures/v2_3_1_project/`` は v2.3.1 で作ったプロジェクトを凍結した
ゴールデンフィクスチャ。``startproject`` の config.py に sources を 1 件足し、
``import-csv`` → ``sync-sources`` → API からの作業者入力(監査ログ有効)まで行った DB を
SQL ダンプ(``database.sql``)として持つ(生成手順は ``tests/contract/_make_upgrade_fixture.py``)。

リファクタで DB の読み書きや起動処理が変わっても、既存の DB の行・監査ログ・取り込み
カーソルが失われずに読めることを守る。利用者と同じく、プロジェクト直下を作業
ディレクトリにして既定の設定(相対パスの DB URL・csv/・.env)で動かす。
"""

from __future__ import annotations

import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from contract import helpers, snapshot
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from monitor_app.cli import app as cli_app
from monitor_app.db.engine import Database
from monitor_app.db.ingest_state import IngestStateStore
from monitor_app.main import create_app
from monitor_app.settings.loader import load_config
from monitor_app.settings.runtime import AppSettings

GOLDEN = snapshot.GOLDEN_PROJECT
DB_RELATIVE = Path("instances") / "database.db"

#: ゴールデン DB の作業者入力(生成時に API で書き込んだもの)。
#: orders: id 6 を作成して数量を 3 → 4 に更新し、id 2 を削除。users: id 6 を作成。
ORDER_IDS = [1, 3, 4, 5, 6]
AUDIT_TRAIL = [  # 新しい順(/api/audit の並び)
    ("orders", "2", "delete"),
    ("users", "6", "create"),
    ("orders", "6", "update"),
    ("orders", "6", "create"),
]


@pytest.fixture
def project(tmp_path, monkeypatch) -> Path:
    """フィクスチャを複製し、SQL ダンプから instances/database.db を復元して cd する。"""
    proj = tmp_path / "project"
    shutil.copytree(GOLDEN, proj, ignore=shutil.ignore_patterns("*.sql"))
    (proj / "instances").mkdir()
    with closing(sqlite3.connect(proj / DB_RELATIVE)) as conn:
        conn.executescript((GOLDEN / "database.sql").read_text(encoding="utf-8"))
    helpers.isolate_from_host(monkeypatch, proj)
    return proj


@pytest.fixture
def client(project):
    """既存 DB の上でアプリを起動する(lifespan = バックグラウンド処理も含めて起動)。"""
    app = create_app(load_config("config.py"), AppSettings())
    with TestClient(app) as test_client:
        yield test_client


def _query(project: Path, sql: str) -> list[tuple]:
    return helpers.query(project / DB_RELATIVE, sql)


def _order_ids(project: Path) -> list[int]:
    return [row[0] for row in _query(project, "SELECT id FROM orders ORDER BY id")]


def _cli(*args: str):
    return CliRunner().invoke(cli_app, list(args))


def test_config_loads_and_check_passes(project):
    config = load_config("config.py")
    assert set(config.tables) == {"users", "products", "orders", "line_events"}
    assert "line_events" in config.sources
    result = _cli("check")
    assert result.exit_code == 0, result.output


def test_app_starts_without_touching_existing_tables(project, client):
    ddl_before = _query(project, "SELECT name, sql FROM sqlite_master ORDER BY name")

    assert client.get("/api/health").json()["status"] == "ok"
    orders = client.get("/api/tables/orders").json()
    assert [row["id"] for row in orders["data"]] == ORDER_IDS
    assert orders["data"][-1] == {"id": 6, "user_id": 2, "product_id": 1, "amount": 4.0}
    users = client.get("/api/tables/users").json()["data"]
    assert users[-1] == {"id": 6, "name": "山田 太郎", "email": "yamada@example.com"}

    # 起動しても既存テーブルを作り直さない(行も定義も保たれる。索引などの追加は可)
    ddl_after = dict(_query(project, "SELECT name, sql FROM sqlite_master"))
    for name, sql in ddl_before:
        assert ddl_after.get(name) == sql, f"{name} の定義が変わりました"


def test_every_view_and_kpi_is_readable(project, client):
    config = load_config("config.py")
    for view, vdef in config.views.items():
        res = client.get(f"/api/views/{view}")
        assert res.status_code == 200, f"{view}: {res.text[:200]}"
        expected = _query(project, f"SELECT COUNT(*) FROM ({vdef.query})")[0][0]
        assert len(res.json()["data"]) == expected, view
    batch = client.get("/api/views/batch", params={"names": ",".join(config.views)})
    assert set(batch.json()["views"]) == set(config.views)

    kpis = {k["key"]: k for k in client.get("/api/kpis").json()["kpis"]}
    assert set(kpis) == set(config.kpis)
    for key, card in config.kpis.items():
        expected = _query(project, card.query)[0][0]
        assert kpis[key]["value"] == pytest.approx(float(expected)), key


def test_crud_round_trip_on_orders(project, client):
    created = client.post(
        "/api/tables/orders", json={"user_id": 1, "product_id": 3, "amount": 2}
    )
    assert created.status_code == 201
    new_id = created.json()["data"]["id"]
    assert new_id == max(ORDER_IDS) + 1  # 既存の最大 id の続きから採番される

    assert client.get(f"/api/tables/orders/{new_id}").json()["data"]["amount"] == 2.0
    updated = client.put(f"/api/tables/orders/{new_id}", json={"amount": 9})
    assert updated.json()["data"]["amount"] == 9.0
    assert client.delete(f"/api/tables/orders/{new_id}").status_code == 200
    assert client.get(f"/api/tables/orders/{new_id}").status_code == 404

    assert _order_ids(project) == ORDER_IDS  # 既存の作業者入力は巻き込まれない


def test_audit_log_written_by_v2_3_1_is_readable(client):
    entries = client.get("/api/audit").json()["entries"]
    trail = [(e["table_name"], e["record_id"], e["action"]) for e in entries]
    assert trail == AUDIT_TRAIL
    update = entries[2]
    assert update["before"]["amount"] == 3.0
    assert update["after"]["amount"] == 4.0
    assert entries[0]["after"] is None  # delete は after を持たない

    # 新しい書き込みは既存の履歴の上に積まれる
    client.put("/api/tables/users/6", json={"email": "taro@example.com"})
    entries = client.get("/api/audit", params={"table": "users"}).json()["entries"]
    assert [(e["record_id"], e["action"]) for e in entries] == [
        ("6", "update"),
        ("6", "create"),
    ]


def test_reimport_csv_with_keep_appends_new_rows_only(project, monkeypatch):
    """``import-csv --keep`` は既存行(作業者入力を含む)を残したまま追記する。

    主キーが衝突しない CSV だけを置く(衝突時の挙動は既知の不具合 DX-01 として
    tests/test_known_issues.py で扱う)。
    """
    incoming = project / "incoming"
    incoming.mkdir()
    (incoming / "products.csv").write_text(
        "id,name,price\n4,Durian,1200\n5,Elderberry,300\n", encoding="utf-8"
    )
    monkeypatch.setenv("MONITOR_CSV_DIR", "incoming")
    result = _cli("import-csv", "--keep")
    assert result.exit_code == 0, result.output

    assert _query(project, "SELECT id, name FROM products ORDER BY id") == [
        (1, "Apple"),
        (2, "Banana"),
        (3, "Cherry"),
        (4, "Durian"),
        (5, "Elderberry"),
    ]
    assert _order_ids(project) == ORDER_IDS
    assert _query(project, "SELECT COUNT(*) FROM users") == [(6,)]


def test_ingest_cursor_written_by_v2_3_1_is_honoured(project):
    """``_ingest_state`` のカーソルが読め、同期で既読分を取り込み直さない。"""
    jsonl = project / "sources" / "line_events.jsonl"
    # 既定の DB URL と同じく作業ディレクトリ(プロジェクト直下)からの相対パスで開く
    store = IngestStateStore(Database(helpers.sqlite_url(DB_RELATIVE)))
    # JSONL のカーソルはバイトオフセット(生成時に全行を読み終えた位置 = ファイル長)
    assert store.get("line_events") == str(jsonl.stat().st_size)

    result = _cli("sync-sources")
    assert result.exit_code == 0, result.output
    assert _query(project, "SELECT COUNT(*) FROM line_events") == [(3,)]

    with jsonl.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write('{"ts": "2026-10-01T08:03:00", "line": "第2ライン", "value": 99.1}\n')
    result = _cli("sync-sources")
    assert result.exit_code == 0, result.output
    rows = _query(project, "SELECT id, line, value FROM line_events ORDER BY id")
    assert rows[-2:] == [(3, "第2ライン", 98.7), (4, "第2ライン", 99.1)]
    assert store.get("line_events") == str(jsonl.stat().st_size)
