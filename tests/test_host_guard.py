"""Host ヘッダの確認(DNS リバインディング対策)。

127.0.0.1 などこの PC だけで待ち受けるサーバーには、悪意のある Web ページが自分の
ドメイン名を 127.0.0.1 に向け直して、同じオリジンとして書き込み・削除を送れてしまう。
その要求は Host ヘッダが攻撃者のドメイン名になるため断る。LAN・Docker(0.0.0.0)、
リバースプロキシ(MONITOR_ROOT_PATH / MONITOR_FORWARDED_ALLOW_IPS)の既定の動作は変えない。

TestClient は ``base_url`` のホストを接続先のアドレス(ASGI の ``scope["server"]``)にも
使うため、``http://127.0.0.1:9990`` に Host ヘッダだけを変えて送り、ループバックに届いた
リバインディングの要求を再現する。最後に本物のサーバー(uvicorn)でも確かめる。
"""

from __future__ import annotations

import logging
import socket
import threading
import time

import pytest
from contract import helpers
from fastapi.testclient import TestClient

from monitor_app import MonitorConfig, TableDef, ViewDef
from monitor_app.main import create_app
from monitor_app.security import (
    host_header_name,
    host_policy,
    is_loopback_host,
    parse_allowed_hosts,
)
from monitor_app.settings.runtime import AppSettings

CFG = MonitorConfig(
    tables={"users": TableDef(columns={"id": "int", "name": "str"}, primary_key="id")},
    views={"users_view": ViewDef(query="SELECT id, name FROM users")},
)
LOOPBACK = "http://127.0.0.1:9990"
EVIL = {"Host": "rebind.attacker.example:9990"}


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    helpers.isolate_from_host(monkeypatch, tmp_path)


def _client(base_url: str = LOOPBACK, **settings) -> TestClient:
    app = create_app(CFG, AppSettings(database_url="sqlite:///:memory:", **settings))
    return TestClient(app, base_url=base_url, follow_redirects=False)


# ---------------------------------------------------------------------------
# HTTP(TestClient)
# ---------------------------------------------------------------------------
def test_rebound_host_cannot_write_read_or_delete_on_the_loopback_default():
    with _client() as client:
        assert client.post("/api/tables/users", json={"name": "a"}).status_code == 201
        for method, path, kwargs in (
            ("POST", "/api/tables/users", {"json": {"name": "planted"}}),
            ("GET", "/api/views/users_view", {}),
            ("DELETE", "/api/tables/users/1", {}),
            ("GET", "/", {}),
        ):
            res = client.request(method, path, headers=EVIL, **kwargs)
            assert res.status_code == 400, (method, path)
            assert "MONITOR_ALLOWED_HOSTS" in res.text
            assert "rebind.attacker.example" not in res.text  # 要求の値は返さない
            assert res.headers["x-content-type-options"] == "nosniff"
        rows = client.get("/api/views/users_view").json()["data"]
        assert [r["name"] for r in rows] == ["a"]


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1:9990",
        "localhost:9990",
        "LOCALHOST",
        "localhost.",
        "app.localhost:9990",
        "[::1]:9990",
        "192.168.1.20:9990",  # IP アドレスは DNS を使わないためリバインディングにならない
        "[fe80::1%25eth0]:9990",
    ],
)
def test_local_names_and_ip_addresses_are_accepted(host):
    with _client() as client:
        res = client.get("/api/health", headers={"Host": host})
    assert res.status_code == 200, host


def test_the_bound_host_name_and_this_machine_are_accepted(monkeypatch):
    monkeypatch.setattr(socket, "gethostname", lambda: "Shopfloor-PC")
    with _client(host="localhost") as client:
        assert client.get(
            "/api/health", headers={"Host": "shopfloor-pc:9990"}
        ).is_success


def test_allowed_hosts_extends_the_loopback_default():
    with _client(allowed_hosts="monitor.example.com, *.corp.example") as client:
        ok = {"Host": "monitor.example.com"}
        assert client.get("/api/health", headers=ok).status_code == 200
        sub = {"Host": "a.b.corp.example:8443"}
        assert client.get("/api/health", headers=sub).status_code == 200
        assert (
            client.get("/api/health", headers={"Host": "corp.example"}).status_code
            == 400
        )
        assert client.get("/api/health", headers=EVIL).status_code == 400


def test_allowed_hosts_star_turns_the_check_off():
    with _client(allowed_hosts="*") as client:
        assert client.get("/api/health", headers=EVIL).status_code == 200


@pytest.mark.parametrize(
    "settings",
    [
        {"host": "0.0.0.0"},  # LAN・Docker
        {"host": "::"},
        {"root_path": "/monitor"},  # リバースプロキシのサブパス(README の nginx の例)
        {"forwarded_allow_ips": "10.0.0.5"},  # プロキシの設定がある
    ],
)
def test_lan_docker_and_proxy_setups_are_unchanged_by_default(settings):
    base = LOOPBACK + settings.get("root_path", "")
    with _client(base_url=base, **settings) as client:
        res = client.get("/api/health", headers={"Host": "monitor.example.com"})
    assert res.status_code == 200


def test_an_explicit_list_is_enforced_on_every_interface():
    with _client(host="0.0.0.0", allowed_hosts="monitor.example.com") as client:
        assert client.get(
            "/api/health", headers={"Host": "monitor.example.com"}
        ).is_success
        assert client.get("/api/health", headers=EVIL).status_code == 400
        assert client.get("/api/health", headers={"Host": "10.0.0.7"}).is_success


def test_connections_that_did_not_arrive_on_loopback_are_not_checked():
    """runserver --host 0.0.0.0(MONITOR_HOST は既定のまま)で LAN から来た接続など。"""
    with _client(base_url="http://192.168.1.20:9990") as client:
        assert client.get("/api/health", headers={"Host": "pc.local"}).is_success
    with _client(base_url="http://testserver") as client:  # TestClient の既定
        assert client.get("/api/health").is_success


def test_requests_without_a_host_header_are_accepted():
    with _client() as client:
        request = client.build_request("GET", "/api/health")
        del request.headers["host"]
        assert client.send(request).status_code == 200


def test_a_rejected_host_is_logged_once_with_the_fix(caplog):
    with (
        caplog.at_level(logging.WARNING, logger="monitor_app.web"),
        _client() as client,
    ):
        for _ in range(3):
            client.get("/api/health", headers=EVIL)
    messages = [r.getMessage() for r in caplog.records if r.name == "monitor_app.web"]
    assert len(messages) == 1
    assert "rebind.attacker.example" in messages[0]
    assert "MONITOR_ALLOWED_HOSTS=rebind.attacker.example" in messages[0]


def test_allowed_hosts_is_a_documented_setting(monkeypatch):
    assert AppSettings().allowed_hosts is None
    monkeypatch.setenv("MONITOR_ALLOWED_HOSTS", "a.example,b.example")
    assert AppSettings().allowed_hosts == "a.example,b.example"


# ---------------------------------------------------------------------------
# 判定の部品
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("value", "name"),
    [
        ("Example.COM:8080", "example.com"),
        ("example.com.", "example.com"),
        ("[::1]:9990", "::1"),
        ("::1", "::1"),
        ("127.0.0.1", "127.0.0.1"),
    ],
)
def test_host_header_name(value, name):
    assert host_header_name(value) == name


def test_loopback_detection():
    assert is_loopback_host("127.0.0.2") and is_loopback_host("[::1]")
    assert is_loopback_host("localhost") and is_loopback_host("x.localhost")
    assert not is_loopback_host("0.0.0.0") and not is_loopback_host("10.0.0.1")
    assert not is_loopback_host("localhost.example.com") and not is_loopback_host(None)


def test_parse_allowed_hosts():
    assert parse_allowed_hosts(" A.example:443, *.B.example  c.example. ") == [
        "a.example",
        "*.b.example",
        "c.example",
    ]
    assert parse_allowed_hosts(None) == [] and parse_allowed_hosts("*") == ["*"]


def test_policy_selection():
    assert host_policy(bound_host="127.0.0.1").loopback_only
    assert host_policy(bound_host="0.0.0.0") is None
    assert host_policy(bound_host="127.0.0.1", root_path="/m") is None
    assert host_policy(bound_host="127.0.0.1", allowed_hosts="*") is None
    explicit = host_policy(bound_host="0.0.0.0", allowed_hosts="x.example")
    assert explicit is not None and not explicit.loopback_only
    assert explicit.allows("x.example") and not explicit.allows("y.example")
    assert explicit.applies_to(None)
    assert not host_policy(bound_host="127.0.0.1").applies_to(None)  # Unix ソケット


# ---------------------------------------------------------------------------
# 本物のサーバー(uvicorn)
# ---------------------------------------------------------------------------
def _raw_get(port: int, host: str) -> int:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(
            f"GET /api/health HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode()
        )
        data = b""
        while chunk := sock.recv(4096):
            data += chunk
    return int(data.split(b" ", 2)[1])


def test_real_server_on_loopback_rejects_a_rebound_host():
    import uvicorn

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    settings = AppSettings(
        database_url="sqlite:///:memory:", host="127.0.0.1", port=port
    )
    config = uvicorn.Config(
        create_app(CFG, settings),
        host="127.0.0.1",
        port=port,
        log_level="warning",
        lifespan="on",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            assert time.monotonic() < deadline, "サーバーが起動しない"
            time.sleep(0.05)
        assert _raw_get(port, f"127.0.0.1:{port}") == 200
        assert _raw_get(port, f"localhost:{port}") == 200
        assert _raw_get(port, f"rebind.attacker.example:{port}") == 400
    finally:
        server.should_exit = True
        thread.join(10)
