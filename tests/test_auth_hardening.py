"""認証の堅牢化: 空の API キー・ASCII 以外のキー・セッションの期限と取り消し・弱いキーの警告。

- 空の ``MONITOR_API_KEY`` は未設定と同じ(誰でも作れる Cookie を通さない。起動時の警告も出る)
- ASCII 以外のキーやヘッダで 500 にならない(一致しなければ 401、日本語のキーも使える)
- セッション Cookie は発行時刻つきで 12 時間で失効し、ログアウトしたものは使えない
- 説明用の値・短すぎる・ASCII 以外のキーを ``check`` と起動時に警告する
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time

import pytest
from contract import helpers
from fastapi.testclient import TestClient

from monitor_app import MonitorConfig, TableDef, ViewDef
from monitor_app.api import deps
from monitor_app.checks import run_checks
from monitor_app.main import create_app
from monitor_app.security import api_key_problems, generate_api_key
from monitor_app.settings.runtime import AppSettings

CFG = MonitorConfig(
    tables={"users": TableDef(columns={"id": "int", "name": "str"}, primary_key="id")},
    views={"users_view": ViewDef(query="SELECT id, name FROM users")},
)
STRONG_KEY = "k" * 32


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    helpers.isolate_from_host(monkeypatch, tmp_path)


def _client(**settings) -> TestClient:
    app = create_app(CFG, AppSettings(database_url="sqlite:///:memory:", **settings))
    return TestClient(app, raise_server_exceptions=False, follow_redirects=False)


# ---------------------------------------------------------------------------
# 空の API キー(#D6-01)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("value", ["", "   "])
def test_blank_api_key_from_the_environment_counts_as_unset(monkeypatch, value):
    monkeypatch.setenv("MONITOR_API_KEY", value)
    assert AppSettings().api_key is None
    assert AppSettings(api_key=value).api_key is None


def test_blank_api_key_means_no_auth_and_no_session_cookie(monkeypatch):
    """以前は空のキーで認証が有効に見え、HMAC("") の公開された定数の Cookie が通った。"""
    monkeypatch.setenv("MONITOR_API_KEY", "")
    with _client() as client:
        assert client.app.state.settings.api_key is None
        res = client.post("/api/login", data={"key": "x"})
        assert res.status_code == 303 and "set-cookie" not in res.headers
    # LAN に公開していれば check が「キーが未設定」と警告する
    issues = run_checks(CFG, AppSettings(host="0.0.0.0", api_key=""))
    assert "insecure-bind" in [i.code for i in issues]


def test_runserver_warns_about_a_blank_api_key(monkeypatch, caplog):
    from monitor_app.cli import server as cli_server

    monkeypatch.setenv("MONITOR_API_KEY", "")
    settings = AppSettings(database_url="sqlite:///:memory:")
    with caplog.at_level(logging.WARNING, logger="monitor_app.cli"):
        cli_server.log_startup(settings, "0.0.0.0", 9990)
    assert any("MONITOR_API_KEY" in r.getMessage() for r in caplog.records)


def test_session_token_refuses_an_empty_key():
    with pytest.raises(ValueError):
        deps.session_token("")
    assert not deps.session_is_valid("1.abc", "")
    assert not deps.api_key_matches("anything", "")


def test_auth_is_off_when_settings_hold_an_empty_key_anyway():
    """検証を通らずに空のキーが入っても(model_construct など)、Cookie は作られない。"""
    settings = AppSettings.model_construct(
        **(
            AppSettings(database_url="sqlite:///:memory:").model_dump()
            | {"api_key": ""}
        )
    )
    with TestClient(create_app(CFG, settings), follow_redirects=False) as client:
        res = client.post("/api/login", data={"key": "x"})
        assert res.status_code == 303
        assert deps.SESSION_COOKIE not in client.cookies


# ---------------------------------------------------------------------------
# ASCII 以外のキー(#D6-07)
# ---------------------------------------------------------------------------
def test_non_ascii_header_with_an_ascii_key_is_401_not_500():
    with _client(api_key=STRONG_KEY) as client:
        res = client.post(
            "/api/tables/users",
            json={"name": "x"},
            headers=[(b"X-API-Key", "café".encode())],
        )
    assert res.status_code == 401


def test_japanese_key_works_for_login_and_utf8_header():
    key = "工場のパスワード-0123456789"
    with _client(api_key=key) as client:
        wrong = client.post(
            "/api/tables/users", json={"name": "x"}, headers={"X-API-Key": "nope"}
        )
        assert wrong.status_code == 401
        ok = client.post(
            "/api/tables/users",
            json={"name": "x"},
            headers=[(b"X-API-Key", key.encode("utf-8"))],
        )
        assert ok.status_code == 201
        res = client.post("/api/login", data={"key": key})
        assert res.status_code == 303 and res.headers["location"] == "/"
        assert deps.SESSION_COOKIE in client.cookies
        assert client.post("/api/tables/users", json={"name": "y"}).status_code == 201


def test_login_with_a_non_ascii_wrong_key_is_a_normal_failure():
    with _client(api_key=STRONG_KEY) as client:
        res = client.post("/api/login", data={"key": "違うキー"})
    assert res.status_code == 303
    assert res.headers["location"] == "/login?error=1"


# ---------------------------------------------------------------------------
# セッションの期限と取り消し(#D6-13)
# ---------------------------------------------------------------------------
def test_session_token_carries_the_issue_time_and_expires():
    now = int(time.time())
    token = deps.session_token(STRONG_KEY, issued_at=now)
    assert token.startswith(f"{now}.")
    assert deps.session_is_valid(token, STRONG_KEY, now=now + 10)
    assert deps.session_is_valid(token, STRONG_KEY, now=now + deps.SESSION_MAX_AGE)
    assert not deps.session_is_valid(
        token, STRONG_KEY, now=now + deps.SESSION_MAX_AGE + 1
    )
    # 別のキー・改ざん・発行時刻のすり替えは通らない
    assert not deps.session_is_valid(token, STRONG_KEY + "x", now=now)
    tampered = token[:-1] + ("1" if token.endswith("0") else "0")
    assert not deps.session_is_valid(tampered, STRONG_KEY, now=now)
    signature = token.split(".", 1)[1]
    assert not deps.session_is_valid(f"{now + 100}.{signature}", STRONG_KEY, now=now)
    # 未来の発行時刻(時計のずれの範囲を超える)は受け付けない
    future = deps.session_token(STRONG_KEY, issued_at=now + 3600)
    assert not deps.session_is_valid(future, STRONG_KEY, now=now)


def test_the_old_static_cookie_is_no_longer_accepted():
    legacy = hmac.new(
        STRONG_KEY.encode(), b"monitor-session", hashlib.sha256
    ).hexdigest()
    with _client(api_key=STRONG_KEY, protect_reads=True) as client:
        client.cookies.set(deps.SESSION_COOKIE, legacy)
        assert client.get("/api/views/users_view").status_code == 401


def test_expired_session_cookie_is_rejected():
    old = deps.session_token(
        STRONG_KEY, issued_at=int(time.time()) - deps.SESSION_MAX_AGE - 5
    )
    with _client(api_key=STRONG_KEY, protect_reads=True) as client:
        client.cookies.set(deps.SESSION_COOKIE, old)
        assert client.get("/api/views/users_view").status_code == 401


def test_login_cookie_max_age_matches_the_server_side_expiry():
    with _client(api_key=STRONG_KEY) as client:
        res = client.post("/api/login", data={"key": STRONG_KEY})
    assert f"max-age={deps.SESSION_MAX_AGE}" in res.headers["set-cookie"].lower()


def test_logout_revokes_the_session_on_the_server():
    with _client(api_key=STRONG_KEY, protect_reads=True) as client:
        client.post("/api/login", data={"key": STRONG_KEY})
        token = client.cookies[deps.SESSION_COOKIE]
        assert client.get("/api/views/users_view").status_code == 200
        client.post("/api/logout")
        assert deps.SESSION_COOKIE not in client.cookies
        # ブラウザが消したはずの Cookie を控えておいて送り直しても使えない
        replay = {"Cookie": f"{deps.SESSION_COOKIE}={token}"}
        assert client.get("/api/views/users_view", headers=replay).status_code == 401
        # もう一度ログインすれば使える(新しい発行時刻のトークン)
        time.sleep(1.05)
        client.post("/api/login", data={"key": STRONG_KEY})
        assert client.cookies[deps.SESSION_COOKIE] != token
        assert client.get("/api/views/users_view").status_code == 200


def test_logout_without_a_valid_cookie_is_harmless():
    with _client(api_key=STRONG_KEY) as client:
        client.cookies.set(deps.SESSION_COOKIE, "garbage")
        assert client.post("/api/logout").status_code == 303
        assert client.app.state.revoked_sessions == {}


def test_revoked_sessions_do_not_pile_up(monkeypatch):
    with _client(api_key=STRONG_KEY, protect_reads=True) as client:
        revoked = client.app.state.revoked_sessions = {"1.expired": 0.0}
        client.post("/api/login", data={"key": STRONG_KEY})
        client.post("/api/logout")
        assert "1.expired" not in revoked and len(revoked) == 1


# ---------------------------------------------------------------------------
# 弱いキーの警告(#D6-17)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("key", "codes"),
    [
        ("change-me", ["api-key-placeholder"]),
        ("CHANGEME", ["api-key-placeholder"]),
        ("<推測されにくい長い文字列>", ["api-key-placeholder", "api-key-non-ascii"]),
        ("short", ["api-key-short"]),
        ("工場のパスワード-0123456789", ["api-key-non-ascii"]),
        (STRONG_KEY, []),
        (None, []),
    ],
)
def test_api_key_problems(key, codes):
    problems = api_key_problems(key)
    assert [p.code for p in problems] == codes
    if key:
        assert all(key not in p.message for p in problems)  # キーの値は出さない


def test_generated_keys_are_long_random_and_shell_safe():
    keys = {generate_api_key() for _ in range(20)}
    assert len(keys) == 20
    for key in keys:
        assert len(key) >= 32 and api_key_problems(key) == []
        assert all(c.isalnum() or c in "-_" for c in key)


def test_check_warns_about_a_placeholder_key():
    issues = run_checks(CFG, AppSettings(api_key="change-me"))
    found = [i for i in issues if i.code == "api-key-placeholder"]
    assert found and found[0].level == "warning"
    assert run_checks(CFG, AppSettings(api_key=STRONG_KEY)) == []


def test_server_start_warns_about_a_weak_key(caplog):
    def start(key: str) -> None:
        app = create_app(
            CFG, AppSettings(database_url="sqlite:///:memory:", api_key=key)
        )
        app.state.services.close()

    with caplog.at_level(logging.WARNING, logger="monitor_app.main"):
        start("abc")
    assert any("短すぎます" in r.getMessage() for r in caplog.records)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="monitor_app.main"):
        start(STRONG_KEY)
    assert not caplog.records
