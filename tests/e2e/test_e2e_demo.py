"""``monitor-app demo`` の E2E: 実サーバを起動し、画面のデータが自動で動くことと停止を確かめる。

準備なしで起動し、起動直後の警告(安全在庫割れ・期限の近いロット)が出ていること、
入出庫の履歴が数秒ごとに増えること(lifespan のシミュレーター → DB → API)、SIGTERM で
正常に終わり一時フォルダが消えることを、本物の uvicorn で確かめる。
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
from contract import helpers

pytestmark = pytest.mark.e2e

BOOT_TIMEOUT = 30.0
STOP_TIMEOUT = 8.0
#: シミュレーターは 4 秒ごと。2 回分と少しのあいだに画面のデータが変わる。
CHANGE_TIMEOUT = 10.0


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _get(port: int, path: str) -> tuple[int, bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", path)
        res = conn.getresponse()
        return res.status, res.read()
    finally:
        conn.close()


def _json(port: int, path: str):
    status, body = _get(port, path)
    assert status == 200, path
    return json.loads(body)


def _log(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-3000:]
    except OSError:
        return ""


def test_demo_starts_moves_on_its_own_and_stops_cleanly(tmp_path):
    temp_root = tmp_path / "tmp"
    temp_root.mkdir()
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    env = helpers.child_env(
        TMPDIR=str(temp_root), TEMP=str(temp_root), TMP=str(temp_root)
    )
    port = _free_port()
    log_path = tmp_path / "demo.log"
    with log_path.open("wb") as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", "monitor_app", "demo", "--port", str(port)],
            cwd=cwd,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + BOOT_TIMEOUT
            while True:
                if proc.poll() is not None:
                    pytest.fail(
                        f"demo が終了しました(exit {proc.returncode})\n{_log(log_path)}"
                    )
                try:
                    if _get(port, "/api/health")[0] == 200:
                        break
                except OSError:
                    pass
                if time.monotonic() > deadline:
                    pytest.fail(f"demo が起動しません\n{_log(log_path)}")
                time.sleep(0.1)

            # アラートはバックグラウンドの評価(画面の更新間隔ごと)が出す
            deadline = time.monotonic() + CHANGE_TIMEOUT
            levels: set[str] = set()
            while levels != {"warning", "critical"} and time.monotonic() < deadline:
                time.sleep(0.2)
                levels = {a["level"] for a in _json(port, "/api/alerts")["alerts"]}
            assert levels == {"warning", "critical"}, _log(log_path)
            assert _get(port, "/")[0] == 200

            first = _json(port, "/api/views/recent_moves")["data"]
            assert first
            deadline = time.monotonic() + CHANGE_TIMEOUT
            latest = first
            while latest == first and time.monotonic() < deadline:
                time.sleep(0.5)
                latest = _json(port, "/api/views/recent_moves")["data"]
            assert latest != first, _log(log_path)
            task = _json(port, "/api/health")["tasks"]["warehouse demo"]
            assert task["last_tick"] is not None and task["last_error"] is None

            if os.name != "nt":
                proc.send_signal(signal.SIGTERM)
                proc.wait(timeout=STOP_TIMEOUT)
                assert proc.returncode == 0, _log(log_path)
                assert list(temp_root.glob("monitor-app-demo-*")) == []
                assert list(cwd.iterdir()) == []
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)
    output = log_path.read_text(encoding="utf-8", errors="replace")
    assert "データは架空" in output and "--template warehouse" in output
