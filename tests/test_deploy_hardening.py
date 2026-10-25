"""デプロイの堅牢化: API キーの生成・設定ファイルの確認・Windows のタスク・配布物と CI。

- ``monitor-app deploy`` / ``startproject --deploy`` は、.env に API キーが無ければ生成して書く
  (手元で試すだけの ``startproject`` ではキーは作らない)
- systemd・Windows の起動ファイルは、config.py の無いフォルダには生成しない(--force で生成)。
  ``--config`` で設定ファイルを指定でき、起動ファイルの runserver に渡る
- Windows の起動時のタスクは、既定で SYSTEM ではなく登録したユーザーの権限で動く
- Compose のイメージ名は MONITOR_IMAGE で変えられる
- sdist にテストが使うコンテナのスクリプトが入り、ワークフローのアクションは SHA で固定する
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from contract import helpers
from dotenv import dotenv_values
from typer.testing import CliRunner

from monitor_app import deploy as deploy_mod
from monitor_app.cli import app
from monitor_app.deploy import DeployError, ensure_api_key, plan_deploy_files

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - Python 3.10
    import tomli as tomllib

ROOT = Path(__file__).parent.parent


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    helpers.isolate_from_host(monkeypatch, tmp_path)
    monkeypatch.setattr(deploy_mod, "local_timezone_name", lambda: None)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def project(tmp_path) -> Path:
    path = tmp_path / "proj"
    path.mkdir()
    (path / "config.py").write_text("# proj\n", encoding="utf-8")
    return path


def _key(project: Path) -> str | None:
    return dotenv_values(project / ".env").get("MONITOR_API_KEY")


# ---------------------------------------------------------------------------
# API キー(#D6-17)
# ---------------------------------------------------------------------------
def test_deploy_generates_an_api_key_into_a_new_env(project, runner):
    result = runner.invoke(app, ["deploy", "docker", "--project", str(project)])
    assert result.exit_code == 0, result.output
    key = _key(project)
    assert key and len(key) >= 32
    assert key not in result.output  # キーそのものは表示しない
    assert str(project.resolve() / ".env") in result.output
    assert "ログイン" in result.output
    if os.name != "nt":
        assert stat.S_IMODE((project / ".env").stat().st_mode) == 0o600
    # 生成したキーで警告は出ない
    from monitor_app.settings.runtime import AppSettings

    os.chdir(project)
    assert AppSettings().api_key == key


def test_deploy_keeps_an_existing_key(project, runner):
    (project / ".env").write_text("MONITOR_API_KEY='" + "x" * 40 + "'\n", "utf-8")
    result = runner.invoke(app, ["deploy", "docker", "--project", str(project)])
    assert result.exit_code == 0, result.output
    assert _key(project) == "x" * 40
    assert "生成" not in result.output


def test_deploy_warns_about_a_placeholder_key_without_changing_it(project, runner):
    (project / ".env").write_text("MONITOR_API_KEY=change-me\n", encoding="utf-8")
    result = runner.invoke(app, ["deploy", "docker", "--project", str(project)])
    assert result.exit_code == 0, result.output
    assert "説明用の例の値" in result.output
    assert _key(project) == "change-me"


def test_blank_key_line_gets_a_generated_key_after_it(project):
    (project / ".env").write_text(
        "MONITOR_PORT=9991\nMONITOR_API_KEY=", encoding="utf-8"
    )
    result = ensure_api_key(project)
    assert result.generated and result.path == project.resolve() / ".env"
    values = dotenv_values(project / ".env")
    assert values["MONITOR_PORT"] == "9991"  # 既存の設定は残す
    assert values["MONITOR_API_KEY"] and len(values["MONITOR_API_KEY"]) >= 32
    assert ensure_api_key(project).generated is False  # 2 回目は何もしない


def test_unreadable_env_is_reported(project):
    (project / ".env").write_bytes(b"MONITOR_API_KEY=\xff\xfe\n")
    with pytest.raises(DeployError, match=r"\.env を読めません"):
        ensure_api_key(project)


def test_startproject_without_deploy_stays_keyless(tmp_path, runner):
    result = runner.invoke(app, ["startproject", "local"])
    assert result.exit_code == 0, result.output
    assert not (tmp_path / "local" / ".env").exists()


def test_startproject_with_deploy_writes_a_key(tmp_path, runner):
    result = runner.invoke(app, ["startproject", "srv", "--deploy", "docker"])
    assert result.exit_code == 0, result.output
    assert _key(tmp_path / "srv")
    assert ".env" in (tmp_path / "srv" / ".gitignore").read_text(encoding="utf-8")


def test_env_example_no_longer_suggests_change_me():
    text = (ROOT / "monitor_app" / "scaffold" / "env.example").read_text("utf-8")
    assert "MONITOR_API_KEY=change-me" not in text
    assert "secrets.token_urlsafe" in text


# ---------------------------------------------------------------------------
# 設定ファイル(#D5-10)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("target", ["systemd", "windows"])
def test_native_targets_refuse_a_folder_without_config(tmp_path, runner, target):
    empty = tmp_path / "empty"
    empty.mkdir()
    result = runner.invoke(app, ["deploy", target, "--project", str(empty)])
    assert result.exit_code == 1
    assert "config.py がありません" in result.output
    assert "--force" in result.output
    assert not any(empty.iterdir())  # 何も書かない

    forced = runner.invoke(app, ["deploy", target, "--project", str(empty), "--force"])
    assert forced.exit_code == 0, forced.output


def test_docker_only_still_warns_for_an_empty_folder(tmp_path, runner):
    empty = tmp_path / "empty"
    empty.mkdir()
    result = runner.invoke(app, ["deploy", "docker", "--project", str(empty)])
    assert result.exit_code == 0, result.output
    assert "デモのプロジェクト" in result.output


def test_config_option_is_passed_to_every_target(tmp_path, runner):
    legacy = tmp_path / "legacy"
    (legacy / "config").mkdir(parents=True)
    (legacy / "config" / "config.py").write_text("# 0.1.x\n", encoding="utf-8")
    result = runner.invoke(
        app,
        ["deploy", "all", "--project", str(legacy), "--config", "config/config.py"],
    )
    assert result.exit_code == 0, result.output
    unit = (legacy / "monitor-app.service").read_text(encoding="utf-8")
    assert re.search(r"^ExecStart=.* --config config/config\.py$", unit, re.M)
    bat = (legacy / "start-monitor.bat").read_bytes().decode("ascii")
    assert ' --config "config/config.py" >> logs\\monitor.log' in bat
    service = yaml.safe_load((legacy / "compose.yaml").read_text(encoding="utf-8"))[
        "services"
    ]["monitor-app"]
    assert service["command"] == ["--config", "config/config.py"]


def test_missing_config_option_file_is_an_error(project, runner):
    result = runner.invoke(
        app, ["deploy", "docker", "--project", str(project), "--config", "nope.py"]
    )
    assert result.exit_code == 1
    assert "設定ファイルが見つかりません" in result.output


def test_docker_needs_a_relative_config_path(project):
    with pytest.raises(DeployError, match="相対パス"):
        plan_deploy_files(project, ["docker"], config=str(project / "config.py"))
    with pytest.raises(DeployError):
        plan_deploy_files(project, ["docker"], config="../other/config.py")


def test_config_paths_are_escaped_for_systemd_and_batch(project):
    files = {
        f.path.name: f
        for f in plan_deploy_files(
            project,
            ["systemd", "windows"],
            python="/opt/venv/bin/python",
            config="my conf/100%.py",
        )
    }
    unit = files["monitor-app.service"].content
    assert '--config "my conf/100%%.py"' in unit
    assert '--config "my conf/100%%.py"' in files["start-monitor.bat"].content
    with pytest.raises(DeployError):
        plan_deploy_files(project, ["windows"], config='a"b.py')


# ---------------------------------------------------------------------------
# Windows・systemd の起動(#D6-06)
# ---------------------------------------------------------------------------
def test_windows_task_runs_as_the_installing_user_by_default(project):
    script = {f.path.name: f.content for f in plan_deploy_files(project, ["windows"])}[
        "install-task.ps1"
    ]
    assert "-LogonType S4U -RunLevel Limited" in script
    assert "[switch]$AsSystem" in script
    # SYSTEM は -AsSystem を付けたときだけ
    system_line = next(
        line for line in script.splitlines() if "-UserId 'SYSTEM'" in line
    )
    before = script[: script.index(system_line)].rstrip().splitlines()
    assert before[-2].strip() == "if ($AsSystem) {"
    assert before[-1].strip().startswith("Write-Host")  # 注意を表示してから
    assert "Test-WritableByEveryone" in script


def test_bat_and_unit_do_not_import_modules_from_the_project_folder(project):
    files = {
        f.path.name: f.content
        for f in plan_deploy_files(
            project, ["systemd", "windows"], python="/opt/venv/bin/python"
        )
    }
    assert "set PYTHONSAFEPATH=1" in files["start-monitor.bat"].splitlines()
    env_line = next(
        line
        for line in files["monitor-app.service"].splitlines()
        if line.startswith("Environment=")
    )
    assert "PYTHONSAFEPATH=1" in env_line.split()


def _powershell() -> str | None:
    return shutil.which("pwsh") or shutil.which("powershell")


@pytest.mark.skipif(_powershell() is None, reason="PowerShell が無い")
def test_install_task_script_parses(project):
    deploy_mod.render_deploy_files(project, ["windows"])
    script = project / "install-task.ps1"
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
# Compose のイメージ名(#D1-04)
# ---------------------------------------------------------------------------
def test_compose_image_name_can_be_changed_from_env(project):
    compose = {f.path.name: f for f in plan_deploy_files(project, ["docker"])}
    service = yaml.safe_load(compose["compose.yaml"].content)["services"]["monitor-app"]
    assert service["image"] == (
        "${MONITOR_IMAGE:-ghcr.io/mikawa-bushi/monitor-app}:"
        "${MONITOR_IMAGE_TAG:-latest}"
    )


def test_monitor_image_is_not_reported_as_an_unknown_setting(tmp_path):
    from monitor_app.settings.runtime import find_unknown_env_keys

    env = tmp_path / ".env"
    env.write_text("MONITOR_IMAGE=monitor-app\nMONITOR_IMAGE_TAG=dev\n", "utf-8")
    assert find_unknown_env_keys(environ={}, env_files=[env]) == []


def _docker_compose_works() -> bool:
    if shutil.which("docker") is None:
        return False
    probe = subprocess.run(["docker", "compose", "version"], capture_output=True)
    return probe.returncode == 0


@pytest.mark.skipif(not _docker_compose_works(), reason="docker compose が無い")
@pytest.mark.parametrize(
    ("dotenv", "image"),
    [
        ("", "ghcr.io/mikawa-bushi/monitor-app:latest"),
        ("MONITOR_IMAGE=monitor-app\n", "monitor-app:latest"),
        ("MONITOR_IMAGE_TAG=2.4.0\n", "ghcr.io/mikawa-bushi/monitor-app:2.4.0"),
    ],
)
def test_docker_compose_resolves_the_image(project, dotenv, image):
    deploy_mod.render_deploy_files(project, ["docker"])
    (project / ".env").write_text(dotenv, encoding="utf-8")
    environ = {
        k: v
        for k, v in os.environ.items()
        if k not in ("TZ", "MONITOR_PORT", "MONITOR_IMAGE", "MONITOR_IMAGE_TAG")
    }
    proc = subprocess.run(
        ["docker", "compose", "config", "--images"],
        cwd=project,
        capture_output=True,
        text=True,
        env=environ,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.split() == [image]


# ---------------------------------------------------------------------------
# 配布物・依存・CI(#D1-02・#D6-14・#D6-15)
# ---------------------------------------------------------------------------
def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_sdist_ships_the_scripts_the_docker_tests_use():
    include = _pyproject()["tool"]["hatch"]["build"]["targets"]["sdist"]["include"]
    assert "/tests" in include
    for test_file in sorted((ROOT / "tests").glob("test_docker_*.py")):
        for script in re.findall(r'"docker" / "([^"]+)"', test_file.read_text("utf-8")):
            assert f"/docker/{script}" in include, (test_file.name, script)
    assert {"/docker/entrypoint.sh", "/docker/healthcheck.py"} <= set(include)


def test_jinja2_floor_excludes_versions_with_known_vulnerabilities():
    spec = next(
        d for d in _pyproject()["project"]["dependencies"] if d.startswith("jinja2")
    )
    floor = re.search(r">=([0-9.]+)", spec).group(1)
    assert tuple(int(p) for p in floor.split(".")) >= (3, 1, 6)


WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda p: p.name)
def test_workflow_actions_are_pinned_to_commit_shas(workflow):
    for line in workflow.read_text(encoding="utf-8").splitlines():
        match = re.search(r"uses:\s*(\S+)", line)
        if not match or match.group(1).startswith("./"):
            continue
        ref = match.group(1).split("@", 1)[1]
        assert re.fullmatch(r"[0-9a-f]{40}", ref), line
        assert re.search(r"#\s*v\d", line), f"版のコメントが無い: {line}"


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda p: p.name)
def test_checkouts_do_not_persist_credentials(workflow):
    data = yaml.safe_load(workflow.read_text(encoding="utf-8"))
    for job in data["jobs"].values():
        for step in job.get("steps", []):
            if str(step.get("uses", "")).startswith("actions/checkout@"):
                assert step.get("with", {}).get("persist-credentials") is False


def test_trusted_publisher_contract_is_kept():
    build = ROOT / ".github" / "workflows" / "build.yml"
    if not build.is_file():
        pytest.skip("ワークフローが無い(sdist などリポジトリの外で実行している)")
    data = yaml.safe_load(build.read_text(encoding="utf-8"))
    publish = data["jobs"]["publish"]
    assert publish["environment"]["name"] == "pypi"
    assert publish["permissions"] == {"id-token": "write"}
    assert any(
        str(step.get("uses", "")).startswith("pypa/gh-action-pypi-publish@")
        for step in publish["steps"]
    )
    dependabot = yaml.safe_load(
        (ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8")
    )
    assert {u["package-ecosystem"] for u in dependabot["updates"]} == {"github-actions"}
