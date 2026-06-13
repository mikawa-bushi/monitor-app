"""ビューページ(table.html)の UI 構造テスト(U04)。

chart 付き/なしの分岐・data-view 属性維持・chart-config 埋め込みを検証する。
"""

from fastapi.testclient import TestClient

from monitor_app import ChartDef, MonitorConfig, TableDef, ViewDef
from monitor_app.main import create_app
from monitor_app.services.importer import CsvImporter
from monitor_app.settings.runtime import AppSettings

import pytest
from pathlib import Path

SCAFFOLD_CSV = Path(__file__).parent.parent / "monitor_app" / "scaffold" / "csv"


@pytest.fixture
def config_with_chart() -> MonitorConfig:
    """chart 付きビューと chart なしビューを両方持つ設定。"""
    return MonitorConfig(
        tables={
            "orders": TableDef(
                columns=["id", "user_id", "product_id", "amount"],
                primary_key="id",
            ),
        },
        views={
            "chart_view": ViewDef(
                query="SELECT id, amount FROM orders",
                title="チャートビュー",
                chart=ChartDef(x="id", y="amount"),
            ),
            "plain_view": ViewDef(
                query="SELECT id, amount FROM orders",
                title="テーブルビュー",
            ),
        },
    )


@pytest.fixture
def client_with_chart(config_with_chart):
    settings = AppSettings(database_url="sqlite:///:memory:")
    app = create_app(config_with_chart, settings)
    return TestClient(app)


def test_chart_view_has_details_and_row_count(client_with_chart):
    """chart 付きビューは <details> と #row-count を含む。"""
    res = client_with_chart.get("/table/chart_view")
    assert res.status_code == 200
    assert "<details" in res.text
    assert 'id="row-count"' in res.text


def test_chart_view_has_data_view_attribute(client_with_chart):
    """chart 付きビューで data-view="{{ view_name }}" 属性が維持されている。"""
    res = client_with_chart.get("/table/chart_view")
    assert res.status_code == 200
    assert 'data-view="chart_view"' in res.text


def test_plain_view_has_no_details(client_with_chart):
    """chart なしビューはテーブルを <details> で包まない。"""
    res = client_with_chart.get("/table/plain_view")
    assert res.status_code == 200
    assert "<details" not in res.text


def test_plain_view_has_data_view_attribute(client_with_chart):
    """chart なしビューでも data-view 属性が維持されている。"""
    res = client_with_chart.get("/table/plain_view")
    assert res.status_code == 200
    assert 'data-view="plain_view"' in res.text


def test_chart_config_json_embedded(client_with_chart):
    """chart 付きビューは chart-config JSON スクリプトブロックを含む。"""
    res = client_with_chart.get("/table/chart_view")
    assert res.status_code == 200
    assert 'id="chart-config"' in res.text


def test_chart_config_not_in_plain_view(client_with_chart):
    """chart なしビューは chart-config を含まない。"""
    res = client_with_chart.get("/table/plain_view")
    assert res.status_code == 200
    assert 'id="chart-config"' not in res.text


def test_existing_table_page_still_works(client):
    """既存 test_pages.py::test_table_page と同等の確認(orders_summary が本文にある)。"""
    res = client.get("/table/orders_summary")
    assert res.status_code == 200
    assert "orders_summary" in res.text
