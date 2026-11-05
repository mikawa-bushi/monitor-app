"""CLI のヘルプ: コマンドの並び順と、すべてのコマンド・オプションの説明。

``monitor-app --help`` はまず試す → 始め方 → 起動 → 点検・取り込み → 保守 → 配備の順に
並べる。
すべてのコマンドとオプションに日本語の短い説明があること(空の説明を残さない)。
"""

from __future__ import annotations

import re

import pytest
import typer.main
from contract import helpers
from typer.testing import CliRunner

from monitor_app.cli import app

#: ``--help`` のコマンドの並び(この順に登録する)。
ORDER = [
    "demo",
    "startproject",
    "serve",
    "runserver",
    "check",
    "import-csv",
    "sync-sources",
    "migrate",
    "backup",
    "restore",
    "deploy",
]
GROUP = typer.main.get_command(app)
#: 日本語(ひらがな・カタカナ・漢字)を含むか。
_JAPANESE = re.compile(r"[぀-ヿ一-鿿]")


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    helpers.isolate_from_host(monkeypatch, tmp_path)


def test_commands_are_listed_in_the_documented_order():
    assert list(GROUP.commands) == ORDER
    result = CliRunner().invoke(app, ["--help"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
    positions = [result.output.index(f" {name} ") for name in ORDER]
    assert positions == sorted(positions)


@pytest.mark.parametrize("name", ORDER)
def test_every_command_and_option_has_a_japanese_help(name):
    command = GROUP.commands[name]
    assert command.help and _JAPANESE.search(command.help), name
    for param in command.params:
        assert param.help and _JAPANESE.search(param.help), f"{name} {param.name}"


@pytest.mark.parametrize(
    ("name", "options"),
    [
        ("check", ["--config", "--data", "--strict", "--fix-schema"]),
        ("import-csv", ["--config", "--keep", "--if-empty", "--force"]),
        ("runserver", ["--import-csv", "--import-csv-if-empty", "--force"]),
        ("migrate", ["--config", "--dry-run"]),
        ("backup", ["--config", "--dest", "--keep"]),
        ("restore", ["--config", "--yes"]),
        ("startproject", ["--deploy", "--from-csv", "--template"]),
        ("demo", ["--host", "--port", "--open"]),
    ],
)
def test_new_options_are_shown_in_the_command_help(name, options):
    result = CliRunner().invoke(app, [name, "--help"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
    for option in options:
        assert option in result.output, option


def test_restore_requires_a_file_argument():
    result = CliRunner().invoke(app, ["restore"])
    assert result.exit_code == 2
    assert "Usage" in result.output and "Traceback" not in result.output


@pytest.mark.parametrize("name", ORDER)
def test_help_via_python_m(tmp_path, name):
    proc = helpers.run_cli(name, "--help", cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    assert "Usage" in proc.stdout.decode("utf-8")
