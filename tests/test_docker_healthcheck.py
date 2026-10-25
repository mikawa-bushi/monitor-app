"""コンテナの HEALTHCHECK(docker/healthcheck.py)のテスト。

確かめるポートの決め方(サーバーの ``--port``・``MONITOR_PORT``・既定 9990)と、
/api/health の応答(200 / 503 / 接続できない)による終了コードを確かめる。
"""

from __future__ import annotations

import http.server
import importlib.util
import socket
import threading
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "docker" / "healthcheck.py"


@pytest.fixture(scope="module")
def healthcheck():
    spec = importlib.util.spec_from_file_location("monitor_healthcheck", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("args", "environ", "expected"),
    [
        (["monitor-app", "runserver", "--import-csv-if-empty"], {}, "9990"),
        (["monitor-app", "runserver"], {"MONITOR_PORT": "8123"}, "8123"),
        # docker run <image> --port 8000(エントリポイントが runserver に渡す)
        (["python", "monitor-app", "runserver", "--port", "8000"], {}, "8000"),
        (["monitor-app", "runserver", "--port=8001"], {"MONITOR_PORT": "9"}, "8001"),
        # 同じオプションが何度もあれば、サーバー(click)と同じく最後の値
        (["runserver", "--port", "1", "--port", "2"], {}, "2"),
        (["runserver", "--port"], {}, "9990"),  # 値が無い(起動に失敗する指定)
    ],
)
def test_port_from_args(healthcheck, args, environ, expected):
    assert healthcheck.port_from_args(args, environ) == expected


def test_server_args_reads_a_nul_separated_command_line(healthcheck, tmp_path):
    cmdline = tmp_path / "cmdline"
    cmdline.write_bytes(b"/opt/venv/bin/python3\0/opt/venv/bin/monitor-app\0--port\0")
    assert healthcheck.server_args(str(cmdline)) == [
        "/opt/venv/bin/python3",
        "/opt/venv/bin/monitor-app",
        "--port",
    ]
    assert healthcheck.server_args(str(tmp_path / "missing")) == []


@pytest.fixture
def health_server():
    """/api/health に指定のステータスで答える HTTP サーバー(ポートを返す)。"""
    servers = []

    def start(status: int) -> int:
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # http.server の名前
                self.send_response(status if self.path == "/api/health" else 404)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args):  # テストの出力を汚さない
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return server.server_address[1]

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize(("status", "code"), [(200, 0), (503, 1)])
def test_main_exit_code_follows_the_health_status(
    healthcheck, health_server, monkeypatch, status, code
):
    port = health_server(status)
    monkeypatch.setattr(healthcheck, "server_args", lambda: ["--port", str(port)])
    assert healthcheck.main() == code


def test_main_fails_when_nothing_listens(healthcheck, monkeypatch):
    # bind しただけ(listen していない)ポートへの接続は拒否される
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        monkeypatch.setattr(healthcheck, "server_args", lambda: [])
        monkeypatch.setenv("MONITOR_PORT", str(sock.getsockname()[1]))
        monkeypatch.setattr(healthcheck, "TIMEOUT", 2)
        assert healthcheck.main() == 1
