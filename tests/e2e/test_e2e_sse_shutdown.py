"""SSE の接続中にサーバーを止めても、ERROR のログを残さず配信を正しく閉じること。

打ち切られた SSE の応答を uvicorn は「ASGI callable returned without completing
response.」として ERROR で記録する。再起動・デプロイのたびに、表示中の画面の数だけ
ERROR が並ぶとログの監視(``journalctl -p err`` など)が誤って警報を出す。停止の合図で
配信を終え、最後のチャンクまで送って閉じる。
"""

from __future__ import annotations

import http.client
import os
import signal
import subprocess
import sys

import pytest
from contract import helpers

from e2e.test_e2e_server import (
    _free_port,
    _log_tail,
    _read_first_event,
    _wait_until_healthy,
)

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(os.name == "nt", reason="POSIX の SIGTERM を使う"),
]

CLIENTS = 3


def test_open_sse_streams_end_cleanly_on_sigterm(tmp_path):
    env = helpers.child_env()
    created = helpers.run_cli("startproject", "proj", cwd=tmp_path, env=env)
    assert created.returncode == 0, created.stderr.decode("utf-8", "replace")
    port = _free_port()
    log_path = tmp_path / "server.log"
    with log_path.open("wb") as log:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "monitor_app",
                "runserver",
                "--import-csv-if-empty",
                "--port",
                str(port),
            ],
            cwd=tmp_path / "proj",
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        streams: list[http.client.HTTPConnection] = []
        try:
            _wait_until_healthy(proc, port, log_path)
            responses = []
            for _ in range(CLIENTS):
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                streams.append(conn)
                conn.request("GET", "/api/views/orders_summary/stream")
                response = conn.getresponse()
                assert response.status == 200
                assert _read_first_event(response)["event"] == "message"
                responses.append(response)

            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=8)
            for response in responses:
                # 最後のチャンクまで届いている(途中で切れた応答なら IncompleteRead)
                response.read()
        finally:
            for conn in streams:
                conn.close()
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)
    text = log_path.read_text(encoding="utf-8", errors="replace")
    assert "ASGI callable returned without completing response" not in text, _log_tail(
        log_path
    )
    assert " ERROR " not in text, _log_tail(log_path)
