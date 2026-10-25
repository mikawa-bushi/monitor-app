"""CLI のスモークテスト: すべてのコマンドの ``--help`` と、引数不足時の表示。

インプロセス(``typer.testing.CliRunner``)に加え、利用者と同じ ``python -m monitor_app``
をサブプロセスで 1 回ずつ実行し、エントリポイントの配線まで確かめる。
"""

from __future__ import annotations

import pytest
import typer.main
from contract import helpers
from typer.testing import CliRunner

from monitor_app.cli import app

COMMANDS = sorted(typer.main.get_command(app).commands)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    helpers.isolate_from_host(monkeypatch, tmp_path)


def test_commands_are_registered():
    assert {"startproject", "check", "import-csv", "sync-sources", "runserver"} <= set(
        COMMANDS
    )


def test_app_help_lists_every_command():
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0, result.output
    for command in COMMANDS:
        assert command in result.output


@pytest.mark.parametrize("command", COMMANDS)
def test_command_help(command):
    result = CliRunner().invoke(app, [command, "--help"])
    assert result.exit_code == 0, result.output
    assert "Usage" in result.output


def test_help_via_python_m(tmp_path):
    proc = helpers.run_cli("--help", cwd=tmp_path)
    output = proc.stdout.decode("utf-8")
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    for command in COMMANDS:
        assert command in output


def test_missing_argument_shows_usage_without_traceback():
    result = CliRunner().invoke(app, ["startproject"])
    assert result.exit_code != 0
    assert isinstance(result.exception, SystemExit)  # 例外ではなく usage エラーで終了
    assert "Usage" in result.output
    assert "Traceback" not in result.output


def test_missing_argument_via_python_m(tmp_path):
    proc = helpers.run_cli("startproject", cwd=tmp_path)
    output = (proc.stdout + proc.stderr).decode("utf-8", "replace")
    assert proc.returncode != 0
    assert "Usage" in output
    assert "Traceback" not in output
    assert list(tmp_path.iterdir()) == []  # 何も作らない
