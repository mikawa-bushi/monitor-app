"""1 回で終わるコマンド(import-csv・check・migrate など)が SIGTERM ですぐ終わること。

コンテナでは monitor-app が PID 1 になり、ハンドラの無い SIGTERM はカーネルに無視
される。``docker stop`` が猶予時間いっぱい待って強制終了(終了コード 137)していた。
ここでは子プロセスを SIGTERM を無視した状態で起動して、PID 1 と同じ条件を作る。
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

import pytest
from contract import helpers
from typer.testing import CliRunner

from monitor_app.cli import app
from monitor_app.cli._common import SELF_MANAGED_SIGNAL_COMMANDS

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX のシグナル")


def _ignore_sigterm() -> None:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)  # PID 1 と同じく、既定では止まらない


def test_import_csv_waiting_for_the_database_stops_on_sigterm(tmp_path):
    project = helpers.start_project(tmp_path)
    env = helpers.child_env(
        MONITOR_DATABASE_URL="postgresql+pg8000://u:p@127.0.0.1:1/x",
        MONITOR_DB_WAIT_TIMEOUT="60",
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "monitor_app", "import-csv"],
        cwd=project,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        preexec_fn=_ignore_sigterm,
    )
    try:
        deadline = time.monotonic() + 20
        # 起動待ちに入る(ログの 1 行目)まで待つ
        line = b""
        while b"\xe5\xbe\x85\xe3\x81\xa3\xe3\x81\xa6" not in line:  # 「待って」
            assert time.monotonic() < deadline, "起動待ちに入りませんでした"
            line = proc.stderr.readline()
            assert line or proc.poll() is None
        began = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        _out, err = proc.communicate(timeout=10)
        took = time.monotonic() - began
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(10)
    assert proc.returncode == 143, err.decode("utf-8", "replace")
    assert took < 5
    assert "停止の指示(SIGTERM)を受けたため、import-csv を中止しました" in err.decode(
        "utf-8", "replace"
    )


def test_handlers_are_restored_after_each_command(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    before = signal.getsignal(signal.SIGTERM)
    result = CliRunner().invoke(app, ["startproject", "proj"])
    assert result.exit_code == 0, result.output
    assert signal.getsignal(signal.SIGTERM) is before


def test_server_commands_keep_their_own_signal_handling():
    # runserver・serve は uvicorn が停止のシグナルを受けて正常に止まり、受けたシグナルを
    # 元のハンドラへ送り直す(systemd は SIGTERM での終了を正常とみなす)。
    assert {"runserver", "serve"} == set(SELF_MANAGED_SIGNAL_COMMANDS)
