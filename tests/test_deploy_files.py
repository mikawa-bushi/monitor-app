"""デプロイ用ファイルの生成(``monitor_app.deploy``・``monitor-app deploy``・
``startproject --deploy``)のテスト。

systemd のユニットは ``systemd-analyze verify`` があれば実際に検証し、Compose の設定は
``docker compose config`` があれば検証する(どちらも無ければその検査だけ飛ばす)。
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from contract import helpers
from typer.testing import CliRunner

from monitor_app import deploy as deploy_mod
from monitor_app.cli import app
from monitor_app.cli import deploy as cli_deploy
from monitor_app.cli.project import GITIGNORE
from monitor_app.deploy import (
    DeployError,
    default_targets,
    foreign_targets,
    is_ephemeral_python,
    local_timezone_name,
    native_target,
    parse_targets,
    plan_deploy_files,
    render_deploy_files,
)

TEMPLATES = Path(deploy_mod.__file__).parent / "scaffold" / "deploy"
ALL_FILES = {
    "compose.yaml",
    ".dockerignore",
    "monitor-app.service",
    "start-monitor.bat",
    "install-task.ps1",
}


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    helpers.isolate_from_host(monkeypatch, tmp_path)
    monkeypatch.setattr(deploy_mod, "local_timezone_name", lambda: None)


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
        ([], ["docker", "systemd", "windows"]),
        (["all"], ["docker", "systemd", "windows"]),
        (["docker,systemd"], ["docker", "systemd"]),
        (["Windows", "docker"], ["docker", "windows"]),
        (["docker", " docker ,", "all"], ["docker", "systemd", "windows"]),
    ],
)
def test_parse_targets(values, expected):
    assert parse_targets(values) == expected


def test_parse_targets_rejects_unknown_names():
    with pytest.raises(DeployError, match="不明な対象です: kubernetes") as info:
        parse_targets(["docker,kubernetes"])
    assert "docker / systemd / windows / all" in str(info.value)


def test_invalid_port_is_rejected(tmp_path):
    with pytest.raises(DeployError, match="1〜65535"):
        plan_deploy_files(tmp_path, ["docker"], port=70000)


# ---------------------------------------------------------------------------
# docker
# ---------------------------------------------------------------------------
def test_compose_file_uses_the_published_image_and_mounts_the_project(tmp_path):
    files = _by_name(plan_deploy_files(tmp_path, ["docker"]))
    assert set(files) == {"compose.yaml", ".dockerignore"}
    text = files["compose.yaml"].content
    assert text == (TEMPLATES / "compose.yaml").read_text(encoding="utf-8")

    service = yaml.safe_load(text)["services"]["monitor-app"]
    # 既定は公開イメージ。.env の MONITOR_IMAGE で自分でビルドしたイメージにできる(#D1-04)
    assert service["image"] == (
        "${MONITOR_IMAGE:-ghcr.io/mikawa-bushi/monitor-app}:"
        "${MONITOR_IMAGE_TAG:-latest}"
    )
    assert service["ports"] == ["${MONITOR_PORT:-9990}:9990"]
    assert service["volumes"] == [".:/project"]
    # .env の MONITOR_HOST / MONITOR_PORT よりも優先して、コンテナ内の待ち受けを固定する
    assert service["environment"]["MONITOR_HOST"] == "0.0.0.0"
    assert service["environment"]["MONITOR_PORT"] == "9990"
    assert service["environment"]["TZ"] == "${TZ:-UTC}"
    # .env はアプリ自身が読む(Compose の env_file は $ を展開して値を変えてしまう)
    assert "env_file" not in service
    # イメージの HEALTHCHECK と同じスクリプト(/api/health を、--port のポートで確かめる)
    # (-I: root で動くため、PYTHONPATH などの環境変数・作業フォルダのモジュールを読まない)
    assert service["healthcheck"]["test"] == [
        "CMD",
        "python",
        "-I",
        "/opt/monitor-app/healthcheck.py",
    ]
    assert service["restart"] == "unless-stopped"
    assert service["stop_grace_period"] == "15s"
    assert service["logging"]["driver"] == "local"
    assert "postgresql+psycopg://" in text  # PostgreSQL の例(コメント)


def test_compose_port_and_timezone_are_filled_in(tmp_path, monkeypatch):
    monkeypatch.setattr(deploy_mod, "local_timezone_name", lambda: "Asia/Tokyo")
    files = _by_name(plan_deploy_files(tmp_path, ["docker"], port=8123))
    service = yaml.safe_load(files["compose.yaml"].content)["services"]["monitor-app"]
    assert service["ports"] == ["${MONITOR_PORT:-8123}:9990"]
    assert service["environment"]["TZ"] == "${TZ:-Asia/Tokyo}"
    assert service["environment"]["MONITOR_PORT"] == "9990"  # コンテナ内は変えない
    assert "http://localhost:8123/" in files["compose.yaml"].content


def test_dockerignore_keeps_secrets_and_databases_out(tmp_path):
    content = _by_name(plan_deploy_files(tmp_path, ["docker"]))[".dockerignore"].content
    lines = {line for line in content.splitlines() if line and not line.startswith("#")}
    assert {".env", "instances/*.db*", "logs/", ".git"} <= lines


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker が無い")
def test_compose_file_is_valid_for_docker_compose(tmp_path):
    probe = subprocess.run(["docker", "compose", "version"], capture_output=True)
    if probe.returncode != 0:
        pytest.skip("docker compose が無い")
    render_deploy_files(tmp_path, ["docker"], port=8123)
    proc = subprocess.run(
        ["docker", "compose", "config", "-q"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={k: v for k, v in os.environ.items() if k not in ("TZ", "MONITOR_PORT")},
    )
    assert proc.returncode == 0, proc.stderr
    assert "warn" not in proc.stderr.lower(), proc.stderr


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
    (tmp_path / "compose.yaml").write_text("# 編集済み\n", encoding="utf-8")

    assert render_deploy_files(tmp_path, ["docker"]) == []
    assert (tmp_path / "compose.yaml").read_text(encoding="utf-8") == "# 編集済み\n"

    again = render_deploy_files(tmp_path, ["docker"], force=True)
    assert {p.name for p in again} == {"compose.yaml", ".dockerignore"}
    assert "services:" in (tmp_path / "compose.yaml").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 環境の判定
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("tz", "link", "expected"),
    [
        ("Asia/Tokyo", "/etc/localtime", "Asia/Tokyo"),
        ("", "/usr/share/zoneinfo/Europe/Berlin", "Europe/Berlin"),
        ("", "/usr/share/zoneinfo/posix/America/New_York", "America/New_York"),
        (":/etc/localtime", "/var/db/timezone/zoneinfo/Asia/Tokyo", "Asia/Tokyo"),
        ("Bad Zone; rm", "/etc/localtime", None),
        ("", "/etc/localtime", None),
    ],
)
def test_local_timezone_name(monkeypatch, tz, link, expected):
    monkeypatch.setattr(deploy_mod, "local_timezone_name", local_timezone_name)
    monkeypatch.setenv("TZ", tz)
    monkeypatch.setattr(deploy_mod.os.path, "realpath", lambda path: link)
    assert deploy_mod.local_timezone_name() == expected


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
    ("platform", "native", "defaults"),
    [
        ("linux", "systemd", ["docker", "systemd"]),
        ("win32", "windows", ["docker", "windows"]),
        ("darwin", None, ["docker"]),
    ],
)
def test_default_targets_follow_the_os(platform, native, defaults):
    assert native_target(platform) == native
    assert default_targets(platform) == defaults
    other = [t for t in ("systemd", "windows") if t != native]
    assert foreign_targets(["docker", "systemd", "windows"], platform) == other


def test_deploy_command_generates_docker_and_this_os_by_default(
    project, runner, monkeypatch
):
    monkeypatch.chdir(project)
    result = runner.invoke(app, ["deploy"])
    assert result.exit_code == 0, result.output
    expected = {f.path.name for f in plan_deploy_files(project, default_targets())}
    created = {p.name for p in project.iterdir()} & ALL_FILES
    assert created == expected
    assert "デプロイ用のファイルを作成しました" in result.output
    assert "docker compose up -d" in result.output
    assert ("sudo systemctl enable --now monitor-app" in result.output) is (
        "systemd" in default_targets()
    )
    assert "作り直してください" not in result.output  # この OS 向けのものだけ


def test_deploy_all_generates_everything(project, runner, monkeypatch):
    monkeypatch.chdir(project)
    result = runner.invoke(app, ["deploy", "all"])
    assert result.exit_code == 0, result.output
    assert {p.name for p in project.iterdir()} >= ALL_FILES
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
    cd_line = next(line for line in result.output.splitlines() if "  cd " in line)
    assert str(project.resolve()) in cd_line
    assert cd_line.strip() != f"cd {project.resolve()}"  # 引用符で囲む


def test_deploy_command_with_target_port_and_project_option(project, runner):
    result = runner.invoke(
        app, ["deploy", "docker", "--project", str(project), "--port", "8123"]
    )
    assert result.exit_code == 0, result.output
    assert not (project / "monitor-app.service").exists()
    compose = (project / "compose.yaml").read_text(encoding="utf-8")
    assert "${MONITOR_PORT:-8123}:9990" in compose
    assert "http://localhost:8123/" in result.output


def test_deploy_command_keeps_existing_files_unless_forced(project, runner):
    (project / "compose.yaml").write_text("# 手で編集\n", encoding="utf-8")
    result = runner.invoke(app, ["deploy", "docker", "--project", str(project)])
    assert result.exit_code == 0, result.output
    assert "既にあるため上書きしませんでした: compose.yaml" in result.output
    assert (project / "compose.yaml").read_text(encoding="utf-8") == "# 手で編集\n"

    forced = runner.invoke(
        app, ["deploy", "docker", "--project", str(project), "--force"]
    )
    assert forced.exit_code == 0, forced.output
    assert "services:" in (project / "compose.yaml").read_text(encoding="utf-8")


def test_deploy_command_errors(tmp_path, runner):
    unknown = runner.invoke(app, ["deploy", "k8s"])
    assert unknown.exit_code == 1
    assert "不明な対象です: k8s" in unknown.output

    missing = runner.invoke(app, ["deploy", "--project", str(tmp_path / "nope")])
    assert missing.exit_code == 1
    assert "フォルダが見つかりません" in missing.output

    empty = tmp_path / "empty"
    empty.mkdir()
    warned = runner.invoke(app, ["deploy", "docker", "--project", str(empty)])
    assert warned.exit_code == 0, warned.output
    assert "config.py がありません" in warned.output


def test_deploy_command_reports_an_unwritable_folder_briefly(project, runner):
    if os.name == "nt" or os.geteuid() == 0:
        pytest.skip("chmod で書き込みを禁止できる一般ユーザーのみ")
    project.chmod(0o555)
    try:
        result = runner.invoke(app, ["deploy", "docker", "--project", str(project)])
    finally:
        project.chmod(0o755)
    assert result.exit_code == 1
    assert "ファイルを書き込めません" in result.output
    assert "Traceback" not in result.output
    assert not isinstance(result.exception, PermissionError)


def test_deploy_command_rejects_an_invalid_port(project, runner):
    result = runner.invoke(app, ["deploy", "--project", str(project), "--port", "0"])
    assert result.exit_code == 1
    assert "ポート番号は 1〜65535 です" in result.output
    assert not (project / "compose.yaml").exists()


def test_deploy_warns_about_an_ephemeral_python(project, runner, monkeypatch):
    monkeypatch.setattr(cli_deploy, "is_ephemeral_python", lambda: True)
    result = runner.invoke(app, ["deploy", "systemd", "--project", str(project)])
    assert result.exit_code == 0, result.output
    assert "uv tool install monitor-app" in result.output
    docker_only = runner.invoke(app, ["deploy", "docker", "--project", str(project)])
    assert "uv tool install" not in docker_only.output


# ---------------------------------------------------------------------------
# CLI: startproject
# ---------------------------------------------------------------------------
def test_startproject_always_writes_a_gitignore(project):
    gitignore = (project / ".gitignore").read_text(encoding="utf-8")
    assert gitignore == GITIGNORE
    lines = set(gitignore.splitlines())
    assert {"instances/*.db*", "backups/", ".env", "logs/", "__pycache__/"} <= lines
    assert not (project / "compose.yaml").exists()  # --deploy なしでは作らない


def test_startproject_with_deploy_targets(tmp_path, runner):
    result = runner.invoke(app, ["startproject", "proj", "--deploy", "docker,windows"])
    assert result.exit_code == 0, result.output
    proj = tmp_path / "proj"
    names = {p.name for p in proj.iterdir()}
    assert {
        "compose.yaml",
        ".dockerignore",
        "start-monitor.bat",
        "install-task.ps1",
    } <= names
    assert "monitor-app.service" not in names
    # 通常の起動手順のあとに、対象ごとの手順を出す
    output = result.output
    assert output.index("monitor-app runserver --import-csv-if-empty") < output.index(
        "[Docker]"
    )
    assert "[Windows]" in output


def test_startproject_rejects_unknown_targets_before_creating_anything(
    tmp_path, runner
):
    result = runner.invoke(app, ["startproject", "proj", "--deploy", "docker,foo"])
    assert result.exit_code == 1
    assert "不明な対象です: foo" in result.output
    assert not (tmp_path / "proj").exists()
