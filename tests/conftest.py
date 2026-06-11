"""テスト共通のフィクスチャ。

アプリファクトリにテスト用設定(in-memory SQLite)を注入し、サンプル CSV を
取り込んだ状態の TestClient を提供する。
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from monitor_app import MonitorConfig, TableDef, ViewDef
from monitor_app.main import create_app
from monitor_app.services.importer import CsvImporter
from monitor_app.settings.runtime import AppSettings

SCAFFOLD_CSV = Path(__file__).parent.parent / "monitor_app" / "scaffold" / "csv"


@pytest.fixture
def config() -> MonitorConfig:
    return MonitorConfig(
        tables={
            "users": TableDef(columns=["id", "name", "email"], primary_key="id"),
            "products": TableDef(columns=["id", "name", "price"], primary_key="id"),
            "orders": TableDef(
                columns=["id", "user_id", "product_id", "amount"],
                primary_key="id",
                foreign_keys={"user_id": "users.id", "product_id": "products.id"},
            ),
        },
        views={
            "users_view": ViewDef(query="SELECT * FROM users", title="ユーザー"),
            "orders_summary": ViewDef(
                query=(
                    "SELECT o.id, u.name AS user, o.amount FROM orders o "
                    "JOIN users u ON o.user_id = u.id"
                ),
                title="注文",
            ),
        },
    )


@pytest.fixture
def app(config):
    settings = AppSettings(database_url="sqlite:///:memory:")
    application = create_app(config, settings)
    CsvImporter(
        config, application.state.registry, application.state.db, SCAFFOLD_CSV
    ).import_all()
    return application


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)
