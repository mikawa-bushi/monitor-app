"""runserver と serve が共有する uvicorn の起動(:func:`monitor_app.cli.server.run_uvicorn`)。

どちらも 1 プロセス・停止の猶予 5 秒・実行時設定のログ形式とアクセスログとプロキシの
設定で起動し、SSE の終了処理のフックを先に読み込む。LAN に公開するときの案内も同じ。
停止のシグナルの扱いは呼び出し側(runserver は起動前だけ、serve は動作中も)。
"""

from __future__ import annotations

import signal
import socket
import sys
from pathlib import Path

import pytest
from contract import helpers
from typer.testing import CliRunner

from monitor_app.cli import app
from monitor_app.cli import server as server_mod
from monitor_app.logging_conf import build_logging_config

#: 実行時設定から決まり、両方のコマンドで同じになる uvicorn の引数。
SHARED = (
    "workers",
    "timeout_graceful_shutdown",
    "log_level",
    "log_config",
    "access_log",
)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    helpers.isolate_from_host(monkeypatch, tmp_path)
    monkeypatch.setattr(server_mod, "_in_container", lambda: False)
    monkeypatch.setenv("MONITOR_CONFIG_PATH", "")  # runserver が書き換える値の後始末


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def calls(monkeypatch) -> list:
    """uvicorn.run の呼び出し(サーバーは起動しない)と、そのときの SIGTERM のハンドラ。"""
    recorded: list = []

    def fake(target, **kwargs):
        recorded.append((target, kwargs, signal.getsignal(signal.SIGTERM)))

    monkeypatch.setattr("uvicorn.run", fake)
    return recorded


@pytest.fixture
def csv_folder(tmp_path) -> Path:
    folder = tmp_path / "data"
    folder.mkdir()
    (folder / "items.csv").write_text("id,v\n1,2\n", encoding="utf-8")
    return folder


def _serve(runner, folder: Path, *args: str):
    result = runner.invoke(app, ["serve", str(folder), "--no-watch", *args])
    assert result.exit_code == 0, result.output
    return result


def _runserver(runner, tmp_path, monkeypatch, *args: str):
    project = helpers.start_project(tmp_path)
    monkeypatch.chdir(project)
    result = runner.invoke(app, ["runserver", *args])
    assert result.exit_code == 0, result.output
    return result


def test_serve_and_runserver_share_the_uvicorn_options(
    runner, calls, csv_folder, tmp_path, monkeypatch
):
    _serve(runner, csv_folder)
    _runserver(runner, tmp_path, monkeypatch)
    (serve_app, serve_kw, _), (target, run_kw, _) = calls
    assert target == "monitor_app.main:app_factory" and run_kw["factory"] is True
    assert not isinstance(serve_app, str)  # serve は作ったアプリをそのまま渡す
    assert {k: serve_kw[k] for k in SHARED} == {k: run_kw[k] for k in SHARED}
    assert serve_kw["workers"] == 1 and serve_kw["timeout_graceful_shutdown"] == 5
    assert serve_kw["log_config"] == build_logging_config("INFO", uvicorn=True)
    assert "sse_starlette.sse" in sys.modules


def test_serve_honours_the_logging_and_proxy_settings(
    runner, calls, csv_folder, monkeypatch
):
    monkeypatch.setenv("MONITOR_ACCESS_LOG", "false")
    monkeypatch.setenv("MONITOR_LOG_FORMAT", "json")
    monkeypatch.setenv("MONITOR_FORWARDED_ALLOW_IPS", "10.0.0.1")
    _serve(runner, csv_folder)
    _app, kw, _ = calls[0]
    assert kw["access_log"] is False
    assert kw["forwarded_allow_ips"] == "10.0.0.1"
    assert kw["log_config"]["formatters"]["monitor"]["()"].endswith("JsonFormatter")


def test_stop_signal_handling_differs_as_designed(
    runner, calls, csv_folder, tmp_path, monkeypatch
):
    """runserver は uvicorn に元のハンドラを渡し、serve は動作中も後片付けのため受ける。"""
    original = signal.getsignal(signal.SIGTERM)
    _runserver(runner, tmp_path, monkeypatch)
    _serve(runner, csv_folder)
    (_, _, during_runserver), (_, _, during_serve) = calls
    assert during_runserver == original
    assert during_serve != original
    assert signal.getsignal(signal.SIGTERM) == original


def test_serve_shows_lan_urls_when_exposed(runner, calls, csv_folder, monkeypatch):
    monkeypatch.setattr(socket, "gethostname", lambda: "line1-pc")
    monkeypatch.setattr(server_mod, "_primary_ip", lambda: "192.168.10.20")
    result = _serve(runner, csv_folder, "--host", "0.0.0.0", "--port", "8123")
    assert "API キーが未設定" in result.output  # 従来の警告
    assert (
        "ほかの PC からは http://line1-pc:8123/ または http://192.168.10.20:8123/"
        " で開けます" in result.stdout
    )


def test_serve_on_loopback_shows_no_lan_hint(runner, calls, csv_folder):
    result = _serve(runner, csv_folder)
    assert "ほかの PC から" not in result.output
    assert "API キーが未設定" not in result.output


def test_serve_in_a_container_shows_the_container_hint(
    runner, calls, csv_folder, monkeypatch
):
    monkeypatch.setattr(server_mod, "_in_container", lambda: True)
    monkeypatch.setenv("MONITOR_API_KEY", "k")
    result = _serve(runner, csv_folder, "--host", "0.0.0.0")
    assert "コンテナの外からは" in result.stdout


@pytest.mark.parametrize(
    ("host", "expected"),
    [("127.0.0.1", None), ("localhost", None), ("::1", None)],
)
def test_access_hint_is_none_on_loopback(host, expected):
    assert server_mod.access_hint(host, 9990) is expected


def test_log_format_applies_to_every_command(tmp_path):
    """MONITOR_LOG_FORMAT=json は runserver 以外のコマンドのログにも効く。"""
    import json

    project = helpers.start_project(tmp_path)
    env = helpers.child_env(MONITOR_LOG_FORMAT="json")
    proc = helpers.run_cli("import-csv", cwd=project, env=env)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    records = [json.loads(line) for line in proc.stderr.decode("utf-8").splitlines()]
    assert any(r["logger"] == "monitor_app.importer" for r in records)
    # コマンドの結果(標準出力)は人が読む形のまま
    assert "✅ 取り込み完了: 合計 13 行" in proc.stdout.decode("utf-8")
