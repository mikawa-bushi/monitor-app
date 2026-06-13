"""UI セッション認証(#11)のテスト。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from monitor_app import MonitorConfig, TableDef, ViewDef
from monitor_app.main import create_app
from monitor_app.settings.runtime import AppSettings


def _client(**settings_kw) -> TestClient:
    config = MonitorConfig(
        tables={"users": TableDef(columns=["id", "name"], primary_key="id")},
        views={"users_view": ViewDef(query="SELECT * FROM users", title="U")},
    )
    settings = AppSettings(database_url="sqlite:///:memory:", **settings_kw)
    return TestClient(create_app(config, settings))


def test_protected_read_requires_auth():
    client = _client(api_key="secret", protect_reads=True)
    assert client.get("/api/views/users_view").status_code == 401
    ok = client.get("/api/views/users_view", headers={"X-API-Key": "secret"})
    assert ok.status_code == 200


def test_login_sets_cookie_and_grants_access():
    client = _client(api_key="secret", protect_reads=True)
    res = client.post("/api/login", data={"key": "secret"}, follow_redirects=False)
    assert res.status_code == 303
    assert "monitor_session" in client.cookies
    # 以後は Cookie で認証される(ブラウザ JS と同じ挙動)
    assert client.get("/api/views/users_view").status_code == 200


def test_login_wrong_key_no_cookie():
    client = _client(api_key="secret", protect_reads=True)
    res = client.post("/api/login", data={"key": "nope"}, follow_redirects=False)
    assert res.status_code == 303
    assert "/login" in res.headers["location"]
    assert "monitor_session" not in client.cookies
    assert client.get("/api/views/users_view").status_code == 401


def test_html_page_redirects_to_login_when_unauthed():
    client = _client(api_key="secret", protect_reads=True)
    res = client.get("/", follow_redirects=False)
    assert res.status_code == 303
    assert res.headers["location"] == "/login"
    # ログインページ自体は認証不要で見える
    assert client.get("/login").status_code == 200


def test_no_api_key_means_no_auth():
    client = _client()  # api_key=None(既定)
    assert client.get("/api/views/users_view").status_code == 200
    assert client.get("/").status_code == 200


def test_write_requires_auth_even_without_protect_reads():
    # protect_reads=False でも api_key 設定時は書き込みに認証必須
    client = _client(api_key="secret", protect_reads=False)
    assert client.get("/api/views/users_view").status_code == 200  # 読みは素通り
    assert client.post("/api/tables/users", json={"name": "x"}).status_code == 401
    ok = client.post(
        "/api/tables/users", json={"name": "x"}, headers={"X-API-Key": "secret"}
    )
    assert ok.status_code == 201
