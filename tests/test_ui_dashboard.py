"""ダッシュボード UI のテスト(U03)。

index ページの新ダッシュボード構成を検証する。
- dashboard-config JSON に chart 付きビューだけが入っている
- #status-bar と #dashboard-grid が存在する
- chart なしビューが list_views セクションにリンクとして出る(title 表示)
- ビュー 0 件 config でも 200 で空状態文言が出る
- /static/js/dashboard.js と /static/css/dashboard.css が 200
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from monitor_app import MonitorConfig, TableDef, ViewDef
from monitor_app.main import create_app
from monitor_app.settings.declarative import ChartDef
from monitor_app.settings.runtime import AppSettings


@pytest.fixture
def chart_def() -> ChartDef:
    """テスト用チャート定義。"""
    return ChartDef(x="id", y="amount")


@pytest.fixture
def config_with_chart(chart_def) -> MonitorConfig:
    """chart 付きビューと chart なしビューを両方持つ config。"""
    return MonitorConfig(
        tables={
            "orders": TableDef(
                columns=["id", "amount"],
                primary_key="id",
            ),
        },
        views={
            "chart_view": ViewDef(
                query="SELECT id, amount FROM orders",
                title="注文グラフ",
                chart=chart_def,
            ),
            "plain_view": ViewDef(
                query="SELECT id, amount FROM orders",
                title="注文一覧",
            ),
        },
    )


@pytest.fixture
def config_no_views() -> MonitorConfig:
    """ビューが 1 つもない config。"""
    return MonitorConfig()


@pytest.fixture
def client_with_chart(config_with_chart) -> TestClient:
    settings = AppSettings(database_url="sqlite:///:memory:")
    app = create_app(config_with_chart, settings)
    return TestClient(app)


@pytest.fixture
def client_no_views(config_no_views) -> TestClient:
    settings = AppSettings(database_url="sqlite:///:memory:")
    app = create_app(config_no_views, settings)
    return TestClient(app)


# ---- テスト本体 ----------------------------------------------------------


def test_dashboard_config_contains_chart_views_only(client_with_chart):
    """dashboard-config JSON には chart 付きビューだけが入る。"""
    res = client_with_chart.get("/")
    assert res.status_code == 200
    assert "dashboard-config" in res.text

    # JSON を抽出して検証
    # <script id="dashboard-config" type="application/json">…</script>
    import re

    m = re.search(
        r'<script[^>]*id="dashboard-config"[^>]*>(.*?)</script>',
        res.text,
        re.DOTALL,
    )
    assert m, "dashboard-config script が見つからない"
    cfg = json.loads(m.group(1))

    view_names = [v["name"] for v in cfg["views"]]
    assert "chart_view" in view_names, "chart 付きビューが含まれていない"
    assert "plain_view" not in view_names, "chart なしビューが含まれている"


def test_dashboard_has_status_bar_and_grid(client_with_chart):
    """#status-bar と #dashboard-grid が HTML に存在する。"""
    res = client_with_chart.get("/")
    assert res.status_code == 200
    assert 'id="status-bar"' in res.text
    assert 'id="dashboard-grid"' in res.text


def test_chart_less_views_appear_in_list(client_with_chart):
    """chart なしビューがビュー一覧セクションにタイトル付きリンクとして出る。"""
    res = client_with_chart.get("/")
    assert res.status_code == 200
    # plain_view の title「注文一覧」がリンクテキストとして存在する
    assert "注文一覧" in res.text
    # chart_view はリンク一覧に出ない(dashboard-grid に入る)
    # ただし dashboard-grid カードのタイトルリンクとしては存在する
    # → plain_view の /table/plain_view リンクが存在すること
    assert "/table/plain_view" in res.text


def test_empty_views_returns_200_with_guidance(client_no_views):
    """ビュー 0 件 config でも 200 で空状態の config 誘導文言が出る。"""
    res = client_no_views.get("/")
    assert res.status_code == 200
    assert "config.py" in res.text or "ビューがありません" in res.text


def test_dashboard_js_accessible(client_with_chart):
    """/static/js/dashboard.js が 200 で返る。"""
    res = client_with_chart.get("/static/js/dashboard.js")
    assert res.status_code == 200


def test_dashboard_css_accessible(client_with_chart):
    """/static/css/dashboard.css が 200 で返る。"""
    res = client_with_chart.get("/static/css/dashboard.css")
    assert res.status_code == 200
