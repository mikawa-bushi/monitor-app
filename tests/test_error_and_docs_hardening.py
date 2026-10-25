"""処理されなかった例外の 500 と、API ドキュメント(/docs・/redoc・/openapi.json)。

- 500 にもセキュリティヘッダが付き、相関 ID だけの JSON を返す(例外の詳細は返さない)
- ドキュメントは版を固定した CDN のスクリプトを SRI つきで読み込み、Google Fonts は読まない
- MONITOR_PROTECT_READS ではログインが必要、MONITOR_API_DOCS=false なら出さない
"""

from __future__ import annotations

import logging

import pytest
from contract import helpers
from fastapi.testclient import TestClient

from monitor_app import MonitorConfig, TableDef, ViewDef
from monitor_app.main import create_app
from monitor_app.settings.runtime import AppSettings
from monitor_app.web import api_docs

CFG = MonitorConfig(
    app_title="Line <A>",
    tables={"users": TableDef(columns={"id": "int", "name": "str"}, primary_key="id")},
    views={"users_view": ViewDef(query="SELECT id, name FROM users")},
)
KEY = "k" * 32


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    helpers.isolate_from_host(monkeypatch, tmp_path)


def _app(**settings):
    return create_app(CFG, AppSettings(database_url="sqlite:///:memory:", **settings))


def _boom_app():
    app = _app()

    @app.get("/__boom")
    def _boom():
        raise RuntimeError("secret detail")

    return app


# ---------------------------------------------------------------------------
# 500(#D6-11)
# ---------------------------------------------------------------------------
def test_unhandled_500_has_security_headers_and_a_reference(caplog):
    with (
        caplog.at_level(logging.ERROR, logger="monitor_app.main"),
        TestClient(_boom_app(), raise_server_exceptions=False) as client,
    ):
        res = client.get("/__boom")
    assert res.status_code == 500
    assert res.headers["x-content-type-options"] == "nosniff"
    assert res.headers["x-frame-options"] == "SAMEORIGIN"
    assert res.headers["referrer-policy"] == "same-origin"
    detail = res.json()["detail"]
    assert detail["code"] == "INTERNAL_ERROR"
    assert "secret detail" not in res.text
    ref = detail["message"].split("ref=")[1].rstrip(")")
    assert any(ref in r.getMessage() for r in caplog.records)  # ログと突き合わせられる


def test_the_exception_still_reaches_the_server_and_tests():
    """例外は送り直す(uvicorn がトレースバックを記録し、TestClient は例外にする)。"""
    with TestClient(_boom_app()) as client, pytest.raises(RuntimeError):
        client.get("/__boom")


def test_errors_after_the_response_started_are_left_alone():
    app = _app()

    @app.get("/__late")
    def _late():
        from starlette.responses import StreamingResponse

        def body():
            yield b"partial"
            raise RuntimeError("late")

        return StreamingResponse(body())

    with TestClient(app, raise_server_exceptions=False) as client:
        res = client.get("/__late")
    # 送り始めた応答に 2 つ目の応答(500)を重ねない(本文の扱いはクライアントの版による)
    assert res.status_code == 200
    with TestClient(app) as client, pytest.raises(RuntimeError):
        client.get("/__late")


# ---------------------------------------------------------------------------
# API ドキュメント(#D6-12)
# ---------------------------------------------------------------------------
def test_docs_pin_cdn_versions_with_sri_and_no_google_fonts():
    with TestClient(_app()) as client:
        swagger = client.get("/docs")
        redoc = client.get("/redoc")
    assert swagger.status_code == 200 and redoc.status_code == 200
    for url, sri in (api_docs.SWAGGER_JS, api_docs.SWAGGER_CSS):
        assert f'"{url}" integrity="{sri}" crossorigin="anonymous"' in swagger.text
    url, sri = api_docs.REDOC_JS
    assert f'"{url}" integrity="{sri}" crossorigin="anonymous"' in redoc.text
    for page in (swagger.text, redoc.text):
        assert "@5/" not in page and "@2/" not in page  # 版を固定しない URL は使わない
        assert "fonts.googleapis.com" not in page
        assert "Line &lt;A&gt;" in page  # タイトルはエスケープする
    assert '"url": "/openapi.json"' in swagger.text
    assert 'spec-url="/openapi.json"' in redoc.text


def test_openapi_schema_is_unchanged_and_public_by_default():
    app = _app()
    with TestClient(app) as client:
        res = client.get("/openapi.json")
    assert res.status_code == 200
    assert res.json() == app.openapi()


def test_docs_need_login_when_reads_are_protected():
    with TestClient(
        _app(api_key=KEY, protect_reads=True), follow_redirects=False
    ) as client:
        for path in ("/docs", "/redoc"):
            res = client.get(path)
            assert res.status_code == 303 and res.headers["location"] == "/login"
        assert client.get("/openapi.json").status_code == 401
        headers = {"X-API-Key": KEY}
        assert client.get("/openapi.json", headers=headers).status_code == 200
        client.post("/api/login", data={"key": KEY})
        assert client.get("/docs").status_code == 200
        assert client.get("/openapi.json").status_code == 200


def test_api_docs_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("MONITOR_API_DOCS", "false")
    with TestClient(_app()) as client:
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert client.get(path).status_code == 404, path
        assert client.get("/api/health").status_code == 200


def test_sub_path_docs_point_at_the_prefixed_schema():
    with TestClient(
        _app(root_path="/monitor"), base_url="http://testserver/monitor"
    ) as client:
        assert '"url": "/monitor/openapi.json"' in client.get("/docs").text
        assert 'spec-url="/monitor/openapi.json"' in client.get("/redoc").text
        schema = client.get("/openapi.json").json()
    assert schema["servers"][0] == {"url": "/monitor"}
