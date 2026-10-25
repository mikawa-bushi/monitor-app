"""実サーバの E2E: ``MONITOR_LOG_FORMAT=json`` で、uvicorn の起動メッセージとアクセス
ログを含む全行が JSON になること(アプリファクトリがログを設定し直しても形式が保たれる)と、
``/api/health`` の本文を確かめる。
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time

import pytest
from contract import helpers

from .test_e2e_server import _free_port, _get, _log_tail, _wait_until_healthy

pytestmark = pytest.mark.e2e


def _stop(proc: subprocess.Popen) -> None:
    if os.name != "nt":
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=8)
            return
        except subprocess.TimeoutExpired:
            pass
    proc.kill()
    proc.wait(timeout=10)


def test_json_logs_and_health_body(tmp_path):
    env = helpers.child_env(MONITOR_LOG_FORMAT="json")
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
                "--import-csv-if-empty",
                "--port",
                str(port),
            ],
            cwd=project,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            _wait_until_healthy(proc, port, log_path)
            status, body = _get(port, "/api/health")
            assert status == 200
            health = json.loads(body)
            assert health["status"] == "ok"
            assert health["database"] == "ok"
            assert "alert scheduler" in health["tasks"]  # scaffold にはアラートがある
            assert _get(port, "/api/views/orders_summary")[0] == 200
            time.sleep(0.2)  # アクセスログが書き出されるのを待つ
        finally:
            _stop(proc)

    lines = log_path.read_text(encoding="utf-8").splitlines()
    records = []
    for line in filter(None, lines):
        try:
            records.append(json.loads(line))
        except ValueError:
            pytest.fail(f"JSON でない行があります: {line!r}\n" + _log_tail(log_path))
    loggers = {r["logger"] for r in records}
    assert {"monitor_app.cli", "uvicorn", "uvicorn.access"} <= loggers
    messages = [r["message"] for r in records]
    assert any("を起動します" in m for m in messages)
    assert any(m.startswith("Started server process") for m in messages)
    access = [r for r in records if r["logger"] == "uvicorn.access"]
    assert {"path": "/api/views/orders_summary", "status": 200}.items() <= next(
        r for r in access if r.get("path") == "/api/views/orders_summary"
    ).items()
