"""v2.1 機能拡張(製造現場向け)のテスト。"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from monitor_app import (
    AlertRule,
    ChartDef,
    FormDef,
    KioskConfig,
    KpiCard,
    MonitorConfig,
    TableDef,
    ViewDef,
)
from monitor_app.main import create_app
from monitor_app.services.alert_service import AlertEngine
from monitor_app.services.importer import CsvImporter
from monitor_app.settings.runtime import AppSettings

SCAFFOLD_CSV = Path(__file__).parent.parent / "monitor_app" / "scaffold" / "csv"


@pytest.fixture
def feature_config() -> MonitorConfig:
    return MonitorConfig(
        tables={
            "products": TableDef(columns=["id", "name", "price"], primary_key="id"),
            "defects": TableDef(
                columns={"id": "int", "line": "str", "code": "str", "qty": "int"},
                primary_key="id",
                form=FormDef(
                    labels={"line": "ライン"},
                    choices={"code": ["キズ", "汚れ"]},
                    submit_label="記録",
                ),
            ),
        },
        views={
            "products_view": ViewDef(
                query="SELECT id, name, price FROM products",
                title="商品",
                labels={"name": "商品名", "price": "単価 (円)"},
                chart=ChartDef(type="line", x="name", y="price", ucl=5000),
            ),
        },
        alerts=[
            AlertRule(
                view="products_view",
                column="price",
                op=">",
                value=1000,
                level="critical",
                message="価格超過",
            )
        ],
        kpis={
            "count": KpiCard(
                title="商品数", query="SELECT COUNT(*) FROM products", target=3
            ),
        },
        kiosk=KioskConfig(views=["products_view"], rotate_seconds=10),
    )


@pytest.fixture
def fclient(feature_config) -> TestClient:
    app = create_app(
        feature_config,
        AppSettings(database_url="sqlite:///:memory:", audit_enabled=True),
    )
    CsvImporter(
        feature_config, app.state.registry, app.state.db, SCAFFOLD_CSV
    ).import_all()
    return TestClient(app)


# --- C 自動取り込み -------------------------------------------------------
class TestIngest:
    def test_single(self, fclient):
        res = fclient.post("/api/ingest/products", json={"name": "X", "price": "500"})
        assert res.json() == {"success": True, "inserted": 1}

    def test_bulk(self, fclient):
        res = fclient.post(
            "/api/ingest/products",
            json=[{"name": "Y", "price": 1}, {"name": "Z", "price": 2}],
        )
        assert res.json()["inserted"] == 2

    def test_unknown_table(self, fclient):
        assert fclient.post("/api/ingest/ghost", json={"x": 1}).status_code == 404


# --- A アラート -----------------------------------------------------------
class TestAlerts:
    def test_alert_in_view_payload(self, fclient):
        # scaffold の Apple=10000 > 1000 で発火する
        data = fclient.get("/api/views/products_view").json()
        assert any(a["message"] == "価格超過" for a in data["alerts"])

    def test_alerts_endpoint(self, fclient):
        fclient.get("/api/views/products_view")  # 評価を駆動
        alerts = fclient.get("/api/alerts").json()["alerts"]
        assert alerts and alerts[0]["level"] == "critical"

    def test_edge_detection(self):
        cfg = MonitorConfig(
            views={"v": ViewDef(query="SELECT 1")},
            alerts=[AlertRule(view="v", column="t", op=">", value=80)],
        )
        engine = AlertEngine(cfg, AppSettings())
        assert engine.evaluate_view("v", [{"t": 90}])[0].count == 1
        assert engine.evaluate_view("v", [{"t": 95}])  # まだ発火中
        assert engine.evaluate_view("v", [{"t": 50}]) == []  # 復帰
        assert engine.active_alerts() == []

    def test_scheduler_evaluates_without_viewer(self):
        # #13: 画面取得が無くてもバックグラウンド評価で発火する。
        from monitor_app.services.alert_scheduler import AlertScheduler

        cfg = MonitorConfig(
            views={"v": ViewDef(query="SELECT 1")},
            alerts=[AlertRule(view="v", column="t", op=">", value=80)],
        )
        engine = AlertEngine(cfg, AppSettings())

        class _StubViewService:
            def get_view(self, _name):
                return {"data": [{"t": 90}]}

        scheduler = AlertScheduler(engine, _StubViewService(), interval_seconds=1.0)
        assert engine.active_alerts() == []  # 誰もビューを開いていない
        scheduler.run_once()  # バックグラウンド評価
        alerts = engine.active_alerts()
        assert alerts and alerts[0]["level"] == "warning"


# --- E KPI / D チャート ---------------------------------------------------
class TestKpiChart:
    def test_kpis(self, fclient):
        kpis = fclient.get("/api/kpis").json()["kpis"]
        count = next(k for k in kpis if k["key"] == "count")
        assert count["value"] == 3.0 and count["status"] == "good"

    def test_chart_embedded_in_page(self, fclient):
        html = fclient.get("/table/products_view").text
        assert 'id="chart"' in html and "chart-config" in html

    def test_view_payload_includes_column_labels(self, fclient):
        # テーブルヘッダーの単位付き見出し。labels 未設定の列はキーに現れない。
        data = fclient.get("/api/views/products_view").json()
        assert data["column_labels"] == {"name": "商品名", "price": "単価 (円)"}


# --- B Kiosk --------------------------------------------------------------
class TestKiosk:
    def test_kiosk_page(self, fclient):
        html = fclient.get("/kiosk").text
        assert 'id="kiosk-config"' in html and "products_view" in html
        assert 'data-theme="dark"' in html


# --- F フォーム -----------------------------------------------------------
class TestForm:
    def test_form_page(self, fclient):
        html = fclient.get("/form/defects").text
        assert "ライン" in html and "<select" in html  # choices → select

    def test_form_schema(self, fclient):
        # 数値列は number 入力になる
        html = fclient.get("/form/defects").text
        assert 'type="number"' in html

    def test_form_missing_table(self, fclient):
        assert fclient.get("/form/ghost").status_code == 404


# --- G 監査 / H エクスポート ----------------------------------------------
class TestAuditExport:
    def test_audit_records_writes(self, fclient):
        fclient.post("/api/tables/products", json={"name": "N", "price": 1})
        fclient.delete("/api/tables/products/2")
        entries = fclient.get("/api/audit").json()["entries"]
        actions = {e["action"] for e in entries}
        assert {"create", "delete"} <= actions

    def test_audit_disabled(self, feature_config):
        app = create_app(feature_config, AppSettings(database_url="sqlite:///:memory:"))
        assert TestClient(app).get("/api/audit").json() == {"entries": []}

    def test_export_csv(self, fclient):
        res = fclient.get("/api/views/products_view/export?format=csv")
        assert res.status_code == 200
        assert "attachment" in res.headers["content-disposition"]
        assert res.text.splitlines()[0] == "id,name,price"
