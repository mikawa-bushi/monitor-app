"""サーバー DB の起動を待っている間の ``docker stop`` / ``systemctl stop``(SIGTERM)で、
猶予時間を待たずに停止する(実プロセスで確かめる)。

- ``runserver``: 起動待ちは uvicorn がアプリを読み込む間(``build_services``)に動く。
  uvicorn はこの間の SIGTERM を記録するだけなので、待ちの側で打ち切る。
- ``runserver --import-csv-if-empty``: 起動前の取り込みのための待ち(``open_database``)。
  起動の準備中のハンドラ(``stop_signals``)が打ち切る。
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from contract import helpers

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(os.name == "nt", reason="POSIX の SIGTERM を送って確かめる"),
]

#: 待ちの開始を確かめてから SIGTERM を送り、停止までに許す秒数。
STOP_LIMIT = 2.0
BOOT_TIMEOUT = 30.0


def _closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _log(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")[-3000:]


def _project(tmp_path: Path) -> Path:
    created = helpers.run_cli(
        "startproject", "proj", cwd=tmp_path, env=helpers.child_env()
    )
    assert created.returncode == 0, created.stderr.decode("utf-8", "replace")
    return tmp_path / "proj"


@pytest.mark.parametrize(
    "extra",
    [[], ["--import-csv-if-empty"]],
    ids=["while-uvicorn-loads-the-app", "before-the-startup-import"],
)
def test_sigterm_while_waiting_for_the_database_stops_promptly(tmp_path, extra):
    project = _project(tmp_path)
    env = helpers.child_env(
        MONITOR_DATABASE_URL=f"postgresql+pg8000://u:p@127.0.0.1:{_closed_port()}/db",
        MONITOR_DB_WAIT_TIMEOUT="60",
    )
    log_path = tmp_path / "server.log"
    port = _closed_port()
    with log_path.open("wb") as log:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "monitor_app",
                "runserver",
                "--port",
                str(port),
                *extra,
            ],
            cwd=project,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + BOOT_TIMEOUT
            while "データベースの起動を待っています" not in _log(log_path):
                if proc.poll() is not None:
                    pytest.fail(f"待つ前に終了しました\n{_log(log_path)}")
                if time.monotonic() > deadline:
                    pytest.fail(f"起動待ちが始まりません\n{_log(log_path)}")
                time.sleep(0.1)
            started = time.monotonic()
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=STOP_LIMIT)
            except subprocess.TimeoutExpired:
                pytest.fail(
                    f"SIGTERM から {STOP_LIMIT} 秒以内に停止しませんでした\n"
                    + _log(log_path)
                )
            elapsed = time.monotonic() - started
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)
    output = _log(log_path)
    assert elapsed < STOP_LIMIT
    # 正常な停止(終了コード 0)か、既定の動作での SIGTERM による終了
    assert proc.returncode in (0, -signal.SIGTERM), output
    assert "Traceback" not in output, output
    assert "中止" in output, output
