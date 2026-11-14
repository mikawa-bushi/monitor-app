"""デプロイ用ファイルの生成(``monitor_app.deploy``・``monitor-app deploy``・
``startproject --deploy``)のテスト。

systemd のユニットは ``systemd-analyze verify`` があれば実際に検証する(無ければその
検査だけ飛ばす)。
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from contract import helpers
from typer.testing import CliRunner

from monitor_app import deploy as deploy_mod
from monitor_app.cli import app
from monitor_app.cli import deploy as cli_deploy
from monitor_app.cli.project import GITIGNORE
from monitor_app.deploy import (
    DeployError,
    is_ephemeral_python,
    native_target,
    parse_targets,
    plan_deploy_files,
    render_deploy_files,
)

SYSTEMD_FILES = {"monitor-app.service"}
WINDOWS_FILES = {"start-monitor.bat", "install-task.ps1"}
ALL_FILES = SYSTEMD_FILES | WINDOWS_FILES


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    helpers.isolate_from_host(monkeypatch, tmp_path)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def project(tmp_path, runner) -> Path:
    result = runner.invoke(app, ["startproject", "proj"])
    assert result.exit_code == 0, result.output
    return tmp_path / "proj"


def _by_name(files) -> dict:
    return {f.path.name: f for f in files}


def _read_bytes(path: Path) -> bytes:
    return path.read_bytes()


# ---------------------------------------------------------------------------
# 対象の指定
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("values", "expected"),
    [
        (["all"], ["systemd", "windows"]),
        (["windows,systemd"], ["systemd", "windows"]),
        (["Windows", " windows ,"], ["windows"]),
        (["systemd", "all"], ["systemd", "windows"]),
    ],
)
def test_parse_targets(values, expected):
    assert parse_targets(values) == expected


def test_parse_targets_rejects_unknown_names():
    with pytest.raises(DeployError, match="不明な対象です: kubernetes") as info:
        parse_targets(["systemd,kubernetes"])
    assert "systemd / windows / all" in str(info.value)


def test_invalid_port_is_rejected(tmp_path):
    with pytest.raises(DeployError, match="1〜65535"):
        plan_deploy_files(tmp_path, ["systemd"], port=70000)


# ---------------------------------------------------------------------------
# systemd
# ---------------------------------------------------------------------------
def _unit(files) -> str:
    return _by_name(files)["monitor-app.service"].content


def _directive(unit: str, key: str) -> str:
    values = [
        line.split("=", 1)[1]
        for line in unit.splitlines()
        if line.startswith(f"{key}=")
    ]
    assert len(values) == 1, (key, values)
    return values[0]


def test_systemd_unit_runs_this_python_in_the_project(tmp_path):
    unit = _unit(
        plan_deploy_files(tmp_path, ["systemd"], python="/opt/venv/bin/python")
    )
    project = tmp_path.resolve()
    assert _directive(unit, "ExecStart") == (
        "/opt/venv/bin/python -m monitor_app runserver --import-csv-if-empty"
    )
    assert _directive(unit, "WorkingDirectory") == str(project)
    assert _directive(unit, "Restart") == "on-failure"
    assert _directive(unit, "TimeoutStopSec") == "15"
    assert _directive(unit, "User") == deploy_mod._current_user()
    # .env はアプリ自身が読む(EnvironmentFile= を使わない理由はユニットのコメントに記載)
    assert not any(line.startswith("EnvironmentFile=") for line in unit.splitlines())
    unit_file = shlex.quote(str(project / "monitor-app.service"))
    assert f"sudo cp {unit_file} /etc/systemd/system/" in unit


def test_systemd_unit_defaults_to_the_running_interpreter(tmp_path):
    unit = _unit(plan_deploy_files(tmp_path, ["systemd"], port=8123))
    python = deploy_mod._systemd_arg(sys.executable)
    assert _directive(unit, "ExecStart").startswith(f"{python} -m monitor_app ")
    assert _directive(unit, "ExecStart").endswith("--import-csv-if-empty --port 8123")


def test_systemd_unit_escapes_spaces_percent_and_dollar(tmp_path):
    project = tmp_path / "my proj %i $HOME"
    python = "/opt/py dir/python 3"  # Linux のパス(Windows で実行しても同じ結果)
    unit = _unit(plan_deploy_files(project, ["systemd"], python=python))
    assert _directive(unit, "WorkingDirectory") == str(project.resolve()).replace(
        "%", "%%"
    )
    expected = '"' + python + '" -m monitor_app runserver --import-csv-if-empty'
    assert _directive(unit, "ExecStart") == expected
    odd = _unit(plan_deploy_files(tmp_path, ["systemd"], python="/opt/a$b%c/python"))
    assert _directive(odd, "ExecStart").startswith("/opt/a$$b%%c/python ")


@pytest.mark.parametrize("python", ["python", "bin/python", "/opt/x\n/python"])
def test_systemd_unit_rejects_unusable_interpreter_paths(tmp_path, python):
    with pytest.raises(DeployError):
        plan_deploy_files(tmp_path, ["systemd"], python=python)


def test_sudo_user_is_used_when_generating_as_root(monkeypatch):
    if not hasattr(os, "geteuid"):
        pytest.skip("POSIX のみ")
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setenv("SUDO_USER", "operator")
    assert deploy_mod._current_user() == "operator"


def _systemd_analyze_works() -> bool:
    if shutil.which("systemd-analyze") is None:
        return False
    return (
        subprocess.run(["systemd-analyze", "--version"], capture_output=True).returncode
        == 0
    )


@pytest.mark.skipif(not _systemd_analyze_works(), reason="systemd-analyze が無い")
@pytest.mark.parametrize("name", ["proj", "my proj %x $y"])
def test_systemd_unit_passes_systemd_analyze_verify(tmp_path, name):
    project = tmp_path / name
    project.mkdir()
    written = render_deploy_files(project, ["systemd"], port=8123)
    assert [p.name for p in written] == ["monitor-app.service"]
    proc = subprocess.run(
        ["systemd-analyze", "verify", "./monitor-app.service"],
        cwd=project,
        capture_output=True,
        text=True,
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output
    # 他のユニット(システム側)の警告は無視し、このユニットへの指摘が無いことを見る
    assert "monitor-app.service" not in output, output


# ---------------------------------------------------------------------------
# windows
# ---------------------------------------------------------------------------
def _windows(tmp_path, **kwargs) -> dict:
    return _by_name(plan_deploy_files(tmp_path, ["windows"], **kwargs))


def test_bat_starts_the_server_with_a_log_file(tmp_path):
    bat = _windows(tmp_path, python=r"C:\Python312\python.exe")["start-monitor.bat"]
    assert bat.encoding == "ascii"
    assert bat.newline == "\r\n"
    lines = bat.content.splitlines()
    assert lines[0] == "@echo off"
    assert 'cd /d "%~dp0"' in lines
    assert "set PYTHONUTF8=1" in lines
    assert "if not exist logs mkdir logs" in lines
    assert (
        r'"C:\Python312\python.exe" -m monitor_app runserver --import-csv-if-empty'
        r" >> logs\monitor.log 2>&1"
    ) in lines
    assert "exit /b %ERRORLEVEL%" in lines
    assert not any(line.startswith("chcp") for line in lines)


def test_bat_uses_a_relative_python_inside_the_project(tmp_path):
    python = str(tmp_path.resolve() / ".venv" / "Scripts" / "python.exe")
    content = _windows(tmp_path, python=python, port=8123)["start-monitor.bat"].content
    assert (
        r'"%~dp0.venv\Scripts\python.exe" -m monitor_app runserver'
        r" --import-csv-if-empty --port 8123 >> logs\monitor.log 2>&1"
    ) in content


def test_bat_with_non_ascii_path_switches_to_utf8(tmp_path):
    bat = _windows(tmp_path, python=r"C:\Users\山田\AppData\Local\Programs\python.exe")[
        "start-monitor.bat"
    ]
    assert bat.encoding == "utf-8"
    lines = bat.content.splitlines()
    # chcp はパスより前(cd より前)に実行する
    assert lines.index("chcp 65001 >nul") < lines.index('cd /d "%~dp0"')
    assert "山田" in bat.content


def test_bat_quotes_the_log_path_it_echoes(tmp_path):
    """「R&D」のように & を含むフォルダでも echo がコマンドの区切りにならない。"""
    content = _windows(tmp_path, python=r"C:\Python312\python.exe")[
        "start-monitor.bat"
    ].content
    echo = next(line for line in content.splitlines() if line.startswith("echo "))
    assert echo.endswith('"%CD%\\logs\\monitor.log"')


def test_scheduled_task_passes_the_bat_with_doubled_quotes(tmp_path):
    """cmd.exe /c は & ( ) などを含む引用符を外すため、""...."" で渡す。"""
    script = _windows(tmp_path)["install-task.ps1"].content
    assert "-Argument ('/c \"\"' + $bat + '\"\"')" in script


def test_bat_escapes_percent_in_paths(tmp_path):
    content = _windows(tmp_path, python=r"C:\100%\python.exe")[
        "start-monitor.bat"
    ].content
    assert r'"C:\100%%\python.exe"' in content


def test_powershell_script_is_written_with_bom_and_crlf(tmp_path):
    render_deploy_files(tmp_path, ["windows"])
    raw = _read_bytes(tmp_path / "install-task.ps1")
    assert raw.startswith(b"\xef\xbb\xbf")  # PowerShell 5.1 が UTF-8 と認識する
    assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")
    text = raw.decode("utf-8-sig")
    assert "Register-ScheduledTask" in text
    assert "start-monitor.bat" in text
    assert "ExecutionTimeLimit ([TimeSpan]::Zero)" in text
    bat = _read_bytes(tmp_path / "start-monitor.bat")
    assert bat.isascii() and b"\n" not in bat.replace(b"\r\n", b"")


def _powershell() -> str | None:
    return shutil.which("pwsh") or shutil.which("powershell")


@pytest.mark.skipif(_powershell() is None, reason="PowerShell が無い")
def test_powershell_script_parses(tmp_path):
    render_deploy_files(tmp_path, ["windows"])
    script = tmp_path / "install-task.ps1"
    command = (
        "$errors = $null; "
        "[System.Management.Automation.Language.Parser]::ParseFile("
        f"'{script}', [ref]$null, [ref]$errors) | Out-Null; "
        "if ($errors) { $errors | ForEach-Object { $_.Message }; exit 1 }"
    )
    proc = subprocess.run(
        [_powershell(), "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


# ---------------------------------------------------------------------------
# 書き出し(上書きしない・force)
# ---------------------------------------------------------------------------
def test_render_writes_every_file_once_and_never_overwrites(tmp_path):
    written = render_deploy_files(tmp_path, ["all"])
    assert {p.name for p in written} == ALL_FILES
    unit = tmp_path / "monitor-app.service"
    unit.write_text("# 編集済み\n", encoding="utf-8")

    assert render_deploy_files(tmp_path, ["systemd"]) == []
    assert unit.read_text(encoding="utf-8") == "# 編集済み\n"

    again = render_deploy_files(tmp_path, ["systemd"], force=True)
    assert {p.name for p in again} == SYSTEMD_FILES
    assert "ExecStart=" in unit.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 環境の判定
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("path", "ephemeral"),
    [
        ("/home/u/.cache/uv/archive-v0/AbC123/bin/python", True),
        ("/home/u/.cache/uv/builds-v0/.tmpX/bin/python", True),
        ("/home/u/.local/share/uv/tools/monitor-app/bin/python", False),
        ("/usr/bin/python3", False),
    ],
)
def test_is_ephemeral_python(path, ephemeral):
    assert is_ephemeral_python(path) is ephemeral


# ---------------------------------------------------------------------------
# CLI: monitor-app deploy
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("platform", "expected", "step"),
    [
        ("linux", SYSTEMD_FILES, "sudo systemctl enable --now monitor-app"),
        ("win32", WINDOWS_FILES, "install-task.ps1"),
    ],
)
def test_deploy_command_generates_this_os_by_default(
    project, runner, monkeypatch, platform, expected, step
):
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.chdir(project)
    result = runner.invoke(app, ["deploy"])
    assert result.exit_code == 0, result.output
    assert {p.name for p in project.iterdir()} & ALL_FILES == expected
    assert "デプロイ用のファイルを作成しました" in result.output
    assert step in result.output
    assert "作り直してください" not in result.output  # この OS 向けのものだけ


def test_deploy_command_without_a_target_stops_on_other_os(
    project, runner, monkeypatch
):
    monkeypatch.setattr(sys, "platform", "darwin")
    before = set(project.iterdir())
    result = runner.invoke(app, ["deploy", "--project", str(project)])
    assert result.exit_code == 1
    assert "対象を指定してください" in result.output
    assert "systemd / windows / all" in result.output
    assert set(project.iterdir()) == before


def test_docker_is_an_unknown_target(project, runner):
    before = set(project.iterdir())
    result = runner.invoke(app, ["deploy", "docker", "--project", str(project)])
    assert result.exit_code == 1
    assert "不明な対象です: docker" in result.output
    assert "systemd / windows / all" in result.output
    assert set(project.iterdir()) == before


def test_deploy_all_generates_everything(project, runner, monkeypatch):
    monkeypatch.chdir(project)
    result = runner.invoke(app, ["deploy", "all"])
    assert result.exit_code == 0, result.output
    assert {p.name for p in project.iterdir()} & ALL_FILES == ALL_FILES
    assert "sudo systemctl enable --now monitor-app" in result.output
    assert "install-task.ps1" in result.output


def test_files_for_another_os_come_with_a_warning(project, runner):
    target = next(t for t in ("systemd", "windows") if t != native_target())
    result = runner.invoke(app, ["deploy", target, "--project", str(project)])
    assert result.exit_code == 0, result.output
    assert f"monitor-app deploy {target}」を実行して作り直してください" in result.output
    assert result.output.count("作り直してください") == 1  # .bat と .ps1 で 1 回


def test_next_steps_quote_paths_with_spaces(tmp_path, runner):
    project = tmp_path / "my proj"
    project.mkdir()
    # systemd・Windows は config.py の無いフォルダには生成しない(#D5-10)
    (project / "config.py").write_text("# proj\n", encoding="utf-8")
    result = runner.invoke(app, ["deploy", "all", "--project", str(project)])
    assert result.exit_code == 0, result.output
    unit = shlex.quote(str(project.resolve() / "monitor-app.service"))
    assert f"sudo cp {unit} /etc/systemd/system/" in result.output


def test_deploy_command_with_target_port_and_project_option(project, runner):
    result = runner.invoke(
        app, ["deploy", "windows", "--project", str(project), "--port", "8123"]
    )
    assert result.exit_code == 0, result.output
    assert not (project / "monitor-app.service").exists()
    bat = (project / "start-monitor.bat").read_text(encoding="utf-8")
    assert "--import-csv-if-empty --port 8123 >>" in bat


def test_deploy_command_keeps_existing_files_unless_forced(project, runner):
    unit = project / "monitor-app.service"
    unit.write_text("# 手で編集\n", encoding="utf-8")
    result = runner.invoke(app, ["deploy", "systemd", "--project", str(project)])
    assert result.exit_code == 0, result.output
    assert "既にあるため上書きしませんでした: monitor-app.service" in result.output
    assert unit.read_text(encoding="utf-8") == "# 手で編集\n"

    forced = runner.invoke(
        app, ["deploy", "systemd", "--project", str(project), "--force"]
    )
    assert forced.exit_code == 0, forced.output
    assert "ExecStart=" in unit.read_text(encoding="utf-8")


def test_deploy_command_errors(tmp_path, runner):
    unknown = runner.invoke(app, ["deploy", "k8s"])
    assert unknown.exit_code == 1
    assert "不明な対象です: k8s" in unknown.output

    missing = runner.invoke(
        app, ["deploy", "systemd", "--project", str(tmp_path / "nope")]
    )
    assert missing.exit_code == 1
    assert "フォルダが見つかりません" in missing.output


def test_deploy_command_reports_an_unwritable_folder_briefly(project, runner):
    if os.name == "nt" or os.geteuid() == 0:
        pytest.skip("chmod で書き込みを禁止できる一般ユーザーのみ")
    project.chmod(0o555)
    try:
        result = runner.invoke(app, ["deploy", "systemd", "--project", str(project)])
    finally:
        project.chmod(0o755)
    assert result.exit_code == 1
    assert "ファイルを書き込めません" in result.output
    assert "Traceback" not in result.output
    assert not isinstance(result.exception, PermissionError)


def test_deploy_command_rejects_an_invalid_port(project, runner):
    result = runner.invoke(
        app, ["deploy", "systemd", "--project", str(project), "--port", "0"]
    )
    assert result.exit_code == 1
    assert "ポート番号は 1〜65535 です" in result.output
    assert not (project / "monitor-app.service").exists()


def test_deploy_warns_about_an_ephemeral_python(project, runner, monkeypatch):
    monkeypatch.setattr(cli_deploy, "is_ephemeral_python", lambda: False)
    lasting = runner.invoke(app, ["deploy", "systemd", "--project", str(project)])
    assert lasting.exit_code == 0, lasting.output
    assert "uv tool install" not in lasting.output

    monkeypatch.setattr(cli_deploy, "is_ephemeral_python", lambda: True)
    result = runner.invoke(
        app, ["deploy", "systemd", "--project", str(project), "--force"]
    )
    assert result.exit_code == 0, result.output
    assert "uv tool install monitor-app" in result.output


# ---------------------------------------------------------------------------
# CLI: startproject
# ---------------------------------------------------------------------------
def test_startproject_always_writes_a_gitignore(project):
    gitignore = (project / ".gitignore").read_text(encoding="utf-8")
    assert gitignore == GITIGNORE
    lines = set(gitignore.splitlines())
    assert {"instances/*.db*", "backups/", ".env", "logs/", "__pycache__/"} <= lines
    assert not (project / "monitor-app.service").exists()  # --deploy なしでは作らない


def test_startproject_with_deploy_targets(tmp_path, runner):
    result = runner.invoke(app, ["startproject", "proj", "--deploy", "windows"])
    assert result.exit_code == 0, result.output
    proj = tmp_path / "proj"
    assert {p.name for p in proj.iterdir()} & ALL_FILES == WINDOWS_FILES
    # 通常の起動手順のあとに、対象ごとの手順を出す
    output = result.output
    assert output.index("monitor-app runserver --import-csv-if-empty") < output.index(
        "[Windows]"
    )


def test_startproject_rejects_unknown_targets_before_creating_anything(
    tmp_path, runner
):
    result = runner.invoke(app, ["startproject", "proj", "--deploy", "docker"])
    assert result.exit_code == 1
    assert "不明な対象です: docker" in result.output
    assert not (tmp_path / "proj").exists()
