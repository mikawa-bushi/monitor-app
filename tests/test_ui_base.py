"""base.html / app.css / theme.js の UI 基盤テスト(U02)。

テスト対象:
- / と /table/<view_name> にサイドバー nav 要素とテーマトグルボタンが含まれる
- ビューリンクが title で表示される
- group 付き config でグループ見出しが出る
- theme.js が /static/js/theme.js で 200 を返す
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from monitor_app import MonitorConfig, TableDef, ViewDef
from monitor_app.main import create_app
from monitor_app.settings.runtime import AppSettings


def _make_client(config: MonitorConfig) -> TestClient:
    """MonitorConfig からテスト用クライアントを生成するヘルパ。"""
    settings = AppSettings(database_url="sqlite:///:memory:")
    app = create_app(config, settings)
    return TestClient(app)


# ---------------------------------------------------------------------------
# サイドバー nav 要素とテーマトグルボタン(conftest の client フィクスチャ使用)
# ---------------------------------------------------------------------------


def test_index_has_sidebar(client: TestClient) -> None:
    """/ のレスポンスにサイドバー要素(id="sidebar")が含まれる。"""
    res = client.get("/")
    assert res.status_code == 200
    assert 'id="sidebar"' in res.text


def test_index_has_theme_toggle(client: TestClient) -> None:
    """/ のレスポンスにテーマトグルボタン(id="theme-toggle")が含まれる。"""
    res = client.get("/")
    assert res.status_code == 200
    assert 'id="theme-toggle"' in res.text


def test_table_page_has_sidebar(client: TestClient) -> None:
    """/table/users_view にサイドバー要素が含まれる。"""
    res = client.get("/table/users_view")
    assert res.status_code == 200
    assert 'id="sidebar"' in res.text


def test_table_page_has_theme_toggle(client: TestClient) -> None:
    """/table/users_view にテーマトグルボタンが含まれる。"""
    res = client.get("/table/users_view")
    assert res.status_code == 200
    assert 'id="theme-toggle"' in res.text


# ---------------------------------------------------------------------------
# ビューリンクが title で表示される
# ---------------------------------------------------------------------------


def test_sidebar_shows_view_title(client: TestClient) -> None:
    """サイドバーのビューリンクに title(「ユーザー」)が含まれる。"""
    res = client.get("/")
    assert res.status_code == 200
    assert "ユーザー" in res.text


def test_table_page_sidebar_shows_view_title(client: TestClient) -> None:
    """/table/users_view のサイドバーにも「ユーザー」が含まれる。"""
    res = client.get("/table/users_view")
    assert res.status_code == 200
    assert "ユーザー" in res.text


# ---------------------------------------------------------------------------
# aria-current="page" が current_view のリンクに付く
# ---------------------------------------------------------------------------


def test_current_view_has_aria_current(client: TestClient) -> None:
    """/table/users_view では users_view リンクに aria-current="page" が付く。"""
    res = client.get("/table/users_view")
    assert res.status_code == 200
    assert 'aria-current="page"' in res.text


# ---------------------------------------------------------------------------
# group 付き config でグループ見出しが出る
# ---------------------------------------------------------------------------


def test_grouped_config_shows_group_labels() -> None:
    """group 付き ViewDef を持つ config でグループ見出しがレンダリングされる。"""
    config = MonitorConfig(
        views={
            "machine_log": ViewDef(
                query="SELECT 1 AS ts, 2 AS val",
                title="機械ログ",
                group="製造ライン",
            ),
            "users_view": ViewDef(
                query="SELECT 1 AS id",
                title="ユーザー",
            ),
        }
    )
    client = _make_client(config)
    res = client.get("/")
    assert res.status_code == 200
    assert "製造ライン" in res.text


def test_grouped_config_shows_ungrouped_as_other() -> None:
    """グループなしビューと named グループが混在するとき「その他」見出しが出る。"""
    config = MonitorConfig(
        views={
            "grouped_view": ViewDef(
                query="SELECT 1 AS ts",
                title="グループ付きビュー",
                group="設備",
            ),
            "plain_view": ViewDef(
                query="SELECT 1 AS x",
                title="グループなしビュー",
            ),
        }
    )
    client = _make_client(config)
    res = client.get("/")
    assert res.status_code == 200
    assert "その他" in res.text


# ---------------------------------------------------------------------------
# theme.js が静的ファイルとして提供される
# ---------------------------------------------------------------------------


def test_theme_js_served(client: TestClient) -> None:
    """GET /static/js/theme.js が 200 を返す。"""
    res = client.get("/static/js/theme.js")
    assert res.status_code == 200
