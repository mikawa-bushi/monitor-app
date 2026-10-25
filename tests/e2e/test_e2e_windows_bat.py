"""Windows の E2E: 生成した start-monitor.bat で実サーバが起動し、ログが
``logs\\monitor.log`` に追記されることを確かめる(Windows でのみ実行する)。
"""

from __future__ import annotations

import os
import subprocess

import pytest
from contract import helpers

from monitor_app.deploy import render_deploy_files

from .test_e2e_server import _free_port, _get, _wait_until_healthy

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        os.name != "nt", reason="Windows のみ(cmd.exe で .bat を実行する)"
    ),
]


def test_start_monitor_bat_runs_the_server_and_writes_the_log(tmp_path):
    env = helpers.child_env()
    created = helpers.run_cli("startproject", "proj", cwd=tmp_path, env=env)
    assert created.returncode == 0, created.stderr.decode("utf-8", "replace")
    project = tmp_path / "proj"
    port = _free_port()
    written = render_deploy_files(project, ["windows"], port=port)
    assert {p.name for p in written} == {"start-monitor.bat", "install-task.ps1"}
    log_path = project / "logs" / "monitor.log"

    # 別のフォルダから起動しても、.bat が自分のフォルダへ移動してから起動する
    proc = subprocess.Popen(
        ["cmd.exe", "/c", str(project / "start-monitor.bat")],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_until_healthy(proc, port, log_path)
        status, _body = _get(port, "/api/views/orders_summary")
        assert status == 200
    finally:
        # cmd.exe とその子(python)をまとめて止める
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True
        )
        proc.wait(timeout=20)

    text = log_path.read_text(encoding="utf-8")  # PYTHONUTF8=1 で UTF-8 になる
    assert "を起動します" in text
    assert "Started server process" in text  # uvicorn のログも同じファイルへ
