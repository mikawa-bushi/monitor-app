"""実サーバの E2E スモーク: 雛形作成 → 起動 → API → SSE → SIGTERM で停止。

``python -m monitor_app runserver`` を本物のサブプロセスとして起動し、TestClient では
通らない部分(uvicorn の起動とシグナル処理、ソケット越しの SSE のワイヤ形式)を確かめる。
SSE の接続中に SIGTERM を送っても停止がハングしないこと(DEPLOY-05。uvicorn 0.47 未満で
発生)もここで守る。
"""

from __future__ import annotations

import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from contract import helpers, snapshot

pytestmark = pytest.mark.e2e

BOOT_TIMEOUT = 20.0  # 起動して /api/health が 200 を返すまで
STOP_TIMEOUT = 8.0  # SIGTERM から終了まで


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _get(port: int, path: str, timeout: float = 5.0) -> tuple[int, bytes]:
    # プロキシ設定(HTTP_PROXY)の影響を受けない http.client を使う
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request("GET", path)
        res = conn.getresponse()
        return res.status, res.read()
    finally:
        conn.close()


def _log_tail(path: Path, lines: int = 40) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "--- サーバのログ(末尾) ---\n" + "\n".join(text.splitlines()[-lines:])


def _wait_until_healthy(proc: subprocess.Popen, port: int, log_path: Path) -> None:
    deadline = time.monotonic() + BOOT_TIMEOUT
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            pytest.fail(
                f"サーバが起動途中で終了しました(exit {proc.returncode})\n"
                + _log_tail(log_path)
            )
        try:
            if _get(port, "/api/health", timeout=1.0)[0] == 200:
                return
        except OSError:
            pass  # まだ listen していない
        time.sleep(0.1)
    pytest.fail(
        f"{BOOT_TIMEOUT:.0f} 秒以内に /api/health が 200 になりませんでした\n"
        + _log_tail(log_path)
    )


def _read_first_event(response: http.client.HTTPResponse) -> dict:
    """空行(イベント境界)まで読み、最初のイベントを返す(ping のコメント行は飛ばす)。"""
    raw = bytearray()
    while True:
        line = response.readline()
        if not line:
            raise AssertionError("最初のイベントを受け取る前にストリームが閉じました")
        raw += line
        if line in (b"\r\n", b"\n"):
            events = snapshot.parse_sse(bytes(raw))
            if events:
                return events[0]


def _assert_stops_on_sigterm(proc: subprocess.Popen, log_path: Path) -> None:
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=STOP_TIMEOUT)
    except subprocess.TimeoutExpired:
        pytest.fail(
            f"SSE の接続中に SIGTERM を送ってから {STOP_TIMEOUT:.0f} 秒以内に"
            "停止しませんでした(DEPLOY-05: 停止のハング)\n" + _log_tail(log_path)
        )
    # uvicorn は終了処理のあとシグナルを再送出するため、-SIGTERM で終わる版もある
    assert proc.returncode in (0, -signal.SIGTERM), _log_tail(log_path)


def test_server_boots_streams_and_stops_on_sigterm(tmp_path):
    env = helpers.child_env()
    created = helpers.run_cli("startproject", "proj", cwd=tmp_path, env=env)
    assert created.returncode == 0, created.stderr.decode("utf-8", "replace")
    project = tmp_path / "proj"
    port = _free_port()
    log_path = tmp_path / "server.log"

    with log_path.open("wb") as log:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "monitor_app",
                "runserver",
                "--import-csv",
                "--port",
                str(port),
            ],
            cwd=project,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        stream = None
        try:
            _wait_until_healthy(proc, port, log_path)

            status, body = _get(port, "/api/views/orders_summary")
            assert status == 200, body
            view = json.loads(body)
            # --import-csv で scaffold の CSV(注文 5 件)が取り込まれている
            assert len(view["data"]) == 5

            stream = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            stream.request(
                "GET",
                "/api/views/orders_summary/stream",
                headers={"Accept": "text/event-stream"},
            )
            response = stream.getresponse()
            assert response.status == 200
            content_type = response.getheader("content-type")
            assert snapshot.mime_type(content_type) == "text/event-stream"
            event = _read_first_event(response)
            assert event["event"] == "message"
            payload = json.loads(event["data"])
            assert set(view) <= set(payload)
            assert "alerts" in payload

            # Windows には POSIX の SIGTERM が無い(finally で強制終了するだけにする)
            if os.name != "nt":
                _assert_stops_on_sigterm(proc, log_path)  # SSE は開いたまま
        finally:
            if stream is not None:
                stream.close()
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)
