"""HTML ページと認証のテスト。"""

from pathlib import Path

from fastapi.testclient import TestClient

from monitor_app.main import create_app
from monitor_app.services.importer import CsvImporter
from monitor_app.settings.runtime import AppSettings

SCAFFOLD_CSV = Path(__file__).parent.parent / "monitor_app" / "scaffold" / "csv"


def test_index_page(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "ビュー一覧" in res.text


def test_table_page(client):
    res = client.get("/table/orders_summary")
    assert res.status_code == 200
    assert "orders_summary" in res.text


def test_table_page_missing_view(client):
    assert client.get("/table/ghost").status_code == 404


def test_api_key_protects_writes(config):
    settings = AppSettings(database_url="sqlite:///:memory:", api_key="secret")
    app = create_app(config, settings)
    CsvImporter(config, app.state.registry, app.state.db, SCAFFOLD_CSV).import_all()
    c = TestClient(app)

    # 読み取りは既定で保護されない
    assert c.get("/api/tables/users").status_code == 200
    # 書き込みはキーが必要
    payload = {"name": "X", "email": "x@y.z"}
    assert c.post("/api/tables/users", json=payload).status_code == 401
    assert (
        c.post(
            "/api/tables/users", headers={"X-API-Key": "bad"}, json=payload
        ).status_code
        == 401
    )
    assert (
        c.post(
            "/api/tables/users", headers={"X-API-Key": "secret"}, json=payload
        ).status_code
        == 201
    )
