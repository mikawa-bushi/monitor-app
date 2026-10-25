"""v1 形式の設定の ``FAVICON_PATH='favicon.ico'`` を、画面では同梱の img/favicon.ico で表示する。

v1 の既定値 ``favicon.ico`` は v2 の配置(static/img/)では 404 になっていた。読み込んだ
設定値は変えず(tests/test_contract.py が固定している)、ページを描くときだけ置き換える。
"""

from __future__ import annotations

import re

import pytest
from contract import snapshot
from fastapi.testclient import TestClient

from monitor_app import MonitorConfig, TableDef, ViewDef
from monitor_app.main import create_app
from monitor_app.settings.loader import load_config
from monitor_app.settings.runtime import AppSettings
from monitor_app.web import static_asset_path

_ICON = re.compile(r'<link rel="icon" href="([^"]+)"')


def _client(config: MonitorConfig, tmp_path) -> TestClient:
    url = f"sqlite:///{(tmp_path / 'f.db').as_posix()}"  # lifespan の終了で閉じる
    return TestClient(create_app(config, AppSettings(_env_file=None, database_url=url)))


def _icon_href(html: str) -> str:
    match = _ICON.search(html)
    assert match, "favicon の link が無い"
    return match.group(1)


def test_legacy_v1_favicon_is_served_from_the_packaged_image(tmp_path):
    config = load_config(snapshot.LEGACY_V1_CONFIG)
    assert config.favicon_path == "favicon.ico"  # 設定値はそのまま
    with _client(config, tmp_path) as client:
        for page in ("/", "/kiosk", "/login", "/table/users_view"):
            response = client.get(page)
            assert response.status_code == 200, page
            href = _icon_href(response.text)
            assert href.startswith("/static/img/favicon.ico?v="), (page, href)
            assert client.get(href).status_code == 200
    assert config.favicon_path == "favicon.ico"


def _config(favicon_path: str) -> MonitorConfig:
    return MonitorConfig(
        tables={"t": TableDef(columns={"id": "int"})},
        views={"v": ViewDef(query="SELECT * FROM t")},
        favicon_path=favicon_path,
    )


@pytest.mark.parametrize(
    ("configured", "shown"),
    [
        ("img/favicon.ico", "img/favicon.ico"),  # 既定値(そのまま)
        ("favicon.ico", "img/favicon.ico"),  # v1 の既定値
        ("css/app.css", "css/app.css"),  # 存在するファイルはそのまま
        ("img/custom.png", "img/custom.png"),  # 無いファイルは設定のまま(404)
        ("brand/favicon.ico", "img/favicon.ico"),  # 同じ名前が img/ にあればそれ
    ],
)
def test_static_asset_path(configured, shown):
    assert static_asset_path(configured) == shown


def test_pages_use_the_configured_favicon_when_it_exists(tmp_path):
    with _client(_config("img/favicon.ico"), tmp_path) as client:
        assert _icon_href(client.get("/").text).startswith("/static/img/favicon.ico?v=")
