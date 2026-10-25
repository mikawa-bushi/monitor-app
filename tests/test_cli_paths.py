"""``--config`` の場所とパスの基準(DX-06)。

- ``--config`` にはプロジェクトのフォルダも指定できる(中の config.py を使う)
- 作業フォルダの外の config.py を使うとき、そのフォルダの ``.env`` も読む。作業フォルダの
  ``.env`` と環境変数が優先(従来の読み方を変えない)。runserver が起動するアプリ
  (app_factory)も同じ規則で読む
- 相対パス(DB・CSV のフォルダなど)は従来どおり作業フォルダが基準。作業フォルダが
  config.py のフォルダと違えば 1 行で案内する
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from contract import helpers
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from monitor_app.cli import app
from monitor_app.cli._common import relative_path_hint, relative_paths
from monitor_app.settings.runtime import (
    AppSettings,
    load_app_settings,
    project_env_files,
)

#: --config を受け取るコマンド(config.py の場所の解決が共通)。
CONFIG_COMMANDS = [
    ["check"],
    ["import-csv"],
    ["sync-sources"],
    ["runserver"],
    ["migrate"],
    ["backup"],
    ["restore", "backup.db"],
]


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    helpers.isolate_from_host(monkeypatch, tmp_path)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def project(tmp_path) -> Path:
    return helpers.start_project(tmp_path)


@pytest.fixture
def elsewhere(tmp_path, monkeypatch) -> Path:
    """プロジェクトの外の作業フォルダ。"""
    folder = tmp_path / "elsewhere"
    folder.mkdir()
    monkeypatch.chdir(folder)
    return folder


@pytest.fixture
def uvicorn_calls(monkeypatch) -> list:
    calls: list = []
    monkeypatch.setattr("uvicorn.run", lambda target, **kw: calls.append((target, kw)))
    monkeypatch.setenv("MONITOR_CONFIG_PATH", "")  # runserver が書き換える値の後始末
    return calls


def _env(path: Path, **values: str) -> None:
    path.write_text("".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8")


def _abs_sqlite(path: Path) -> str:
    return helpers.sqlite_url(path)


# ---------------------------------------------------------------------------
# --config にフォルダ
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("command", CONFIG_COMMANDS, ids=lambda c: c[0])
def test_a_folder_without_config_py_is_reported(
    runner, elsewhere, uvicorn_calls, command
):
    result = runner.invoke(app, [*command, "--config", str(elsewhere)])
    assert result.exit_code == 1
    assert f"config.py が見つかりません: {elsewhere / 'config.py'}" in result.stderr
    assert uvicorn_calls == []


def test_import_csv_with_the_project_folder(project, runner, monkeypatch):
    monkeypatch.chdir(project)  # 相対パスの基準(DB・csv/)は作業フォルダ
    result = runner.invoke(app, ["import-csv", "--config", "."])
    assert result.exit_code == 0, result.output
    assert "✅ 取り込み完了: 合計 13 行" in result.stdout


def test_runserver_with_the_project_folder_points_the_app_at_config_py(
    project, runner, uvicorn_calls, monkeypatch
):
    monkeypatch.chdir(project.parent)
    _env(project / ".env", MONITOR_DATABASE_URL=_abs_sqlite(project / "app.db"))
    result = runner.invoke(app, ["runserver", "--config", "proj"])
    assert result.exit_code == 0, result.output
    assert os.environ["MONITOR_CONFIG_PATH"] == str(Path("proj") / "config.py")
    # app_factory も同じ規則で config.py のフォルダの .env を読む
    from monitor_app.main import app_factory

    application = app_factory()
    try:
        assert application.state.settings.database_url == _abs_sqlite(
            project / "app.db"
        )
        with TestClient(application) as client:
            assert client.get("/api/health").json()["status"] == "ok"
    finally:
        application.state.services.close()


# ---------------------------------------------------------------------------
# config.py のフォルダの .env
# ---------------------------------------------------------------------------
def test_project_env_is_read_when_running_from_another_folder(
    project, runner, elsewhere
):
    db = project / "instances" / "database.db"
    _env(
        project / ".env",
        MONITOR_DATABASE_URL=_abs_sqlite(db),
        MONITOR_CSV_DIR=str(project / "csv"),
    )
    result = runner.invoke(app, ["import-csv", "--config", str(project)])
    assert result.exit_code == 0, result.output
    assert f"データベース: SQLite {db}" in result.stdout
    assert "✅ 取り込み完了: 合計 13 行" in result.stdout
    assert not (elsewhere / "instances").exists()  # 作業フォルダに DB を作らない


def test_working_folder_env_and_environment_take_precedence(
    project, elsewhere, monkeypatch
):
    _env(project / ".env", MONITOR_PORT="1111", MONITOR_HOST="10.0.0.1")
    _env(elsewhere / ".env", MONITOR_PORT="2222")
    settings = load_app_settings(project / "config.py")
    assert (settings.port, settings.host) == (2222, "10.0.0.1")
    monkeypatch.setenv("MONITOR_PORT", "3333")
    assert load_app_settings(project / "config.py").port == 3333


def test_project_env_files_only_adds_a_different_existing_file(project, elsewhere):
    config_py = project / "config.py"
    assert project_env_files(config_py) is None  # プロジェクトに .env が無い
    _env(project / ".env", MONITOR_PORT="1")
    assert project_env_files(config_py) == (project / ".env", Path(".env"))
    assert project_env_files(None) is None
    os.chdir(project)  # 作業フォルダの .env と同じファイルなら足さない
    try:
        assert project_env_files("config.py") is None
        assert project_env_files(config_py) is None
    finally:
        os.chdir(elsewhere)


def test_settings_from_the_project_env_are_validated_briefly(
    project, runner, elsewhere
):
    _env(project / ".env", MONITOR_PORT="abc")
    result = runner.invoke(app, ["check", "--config", str(project)])
    assert result.exit_code == 1
    assert "MONITOR_PORT='abc'(整数を指定してください)" in result.stderr
    assert "Traceback" not in result.output


def test_check_finds_misspelled_keys_in_the_project_env(project, runner, elsewhere):
    _env(project / ".env", MONITOR_PROTECT_READ="true")
    result = runner.invoke(app, ["check", "--config", str(project)])
    assert result.exit_code == 1  # 閲覧の保護の綴りの誤りはエラー
    assert f"{project / '.env'} の MONITOR_PROTECT_READ" in result.stdout
    assert "MONITOR_PROTECT_READS の誤りではありませんか?" in result.stdout


def test_app_factory_without_a_project_env_keeps_the_default(tmp_path, monkeypatch):
    from monitor_app.main import app_factory

    config_py = tmp_path / "proj" / "config.py"
    config_py.parent.mkdir()
    config_py.write_text(
        "from monitor_app import MonitorConfig, TableDef\n"
        "config = MonitorConfig(tables={'t': TableDef(columns={'id': 'int'})})\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MONITOR_CONFIG_PATH", str(config_py))
    _env(tmp_path / ".env", MONITOR_DATABASE_URL=_abs_sqlite(tmp_path / "cwd.db"))
    application = app_factory()
    try:
        assert application.state.settings.database_url == _abs_sqlite(
            tmp_path / "cwd.db"
        )
    finally:
        application.state.services.close()


# ---------------------------------------------------------------------------
# 相対パスの基準の案内
# ---------------------------------------------------------------------------
def test_relative_paths_are_listed():
    settings = AppSettings(
        _env_file=None, database_url="sqlite:///instances/x.db", backup_dir="bk"
    )
    assert relative_paths(settings) == ["instances/x.db", "csv", "bk"]
    absolute = AppSettings(
        _env_file=None,
        database_url=_abs_sqlite(Path.cwd() / "x.db"),
        csv_dir=Path.cwd() / "csv",
    )
    assert relative_paths(absolute) == []
    for url in ("sqlite://", "sqlite:///:memory:", "postgresql+psycopg://u@h/db"):
        assert relative_paths(AppSettings(_env_file=None, database_url=url)) == ["csv"]


def test_hint_when_the_working_folder_is_not_the_project_folder(
    project, runner, elsewhere, caplog
):
    # 作業フォルダに無い csv/ を探して 0 行を取り込み、作業フォルダに空の DB を作る
    # 代わりに、取り込みの前に止める(以前は警告だけで続けていた)。
    # くわしい場合分けは test_config_folder_paths.py。
    result = runner.invoke(app, ["import-csv", "--config", str(project)])
    assert result.exit_code == 1, result.output
    assert "相対パス(csv)は今のフォルダ(" in result.stderr
    assert str(project) in result.stderr and str(Path.cwd()) in result.stderr
    assert "cd " in result.stderr
    assert not (elsewhere / "instances").exists()  # 空の DB を作らない


def test_no_hint_inside_the_project_or_with_absolute_paths(project, elsewhere):
    settings = AppSettings(_env_file=None)
    assert relative_path_hint(project / "config.py", settings) is not None
    absolute = AppSettings(
        _env_file=None,
        database_url=_abs_sqlite(project / "x.db"),
        csv_dir=project / "csv",
    )
    assert relative_path_hint(project / "config.py", absolute) is None
    os.chdir(project)
    try:
        assert relative_path_hint(Path("config.py"), settings) is None
    finally:
        os.chdir(elsewhere)


def test_hint_mentions_relative_source_paths(project, elsewhere):
    from monitor_app import MonitorConfig, TableDef
    from monitor_app.settings.declarative import SourceDef

    cfg = MonitorConfig(
        tables={"t": TableDef(columns={"id": "int"})},
        sources={"s": SourceDef(kind="jsonl", path="logs/a.jsonl", table="t")},
    )
    settings = AppSettings(
        _env_file=None,
        database_url=_abs_sqlite(project / "x.db"),
        csv_dir=project / "csv",
    )
    # 案内するのは、config.py のフォルダにしか無いパス(作業フォルダにも無いなら黙る)
    assert relative_path_hint(project / "config.py", settings, cfg) is None
    (project / "logs").mkdir()
    (project / "logs" / "a.jsonl").write_text("", encoding="utf-8")
    hint = relative_path_hint(project / "config.py", settings, cfg)
    assert hint is not None and "logs/a.jsonl" in hint


def test_relative_paths_ignore_an_unparsable_url():
    settings = AppSettings(_env_file=None, database_url="not a url")
    assert relative_paths(settings) == ["csv"]


def test_missing_folders_are_compared_by_their_names(tmp_path):
    from monitor_app.cli._common import same_dir

    assert same_dir(tmp_path / "a" / ".." / "b", tmp_path / "b")
    assert not same_dir(tmp_path / "a", tmp_path / "b")


def test_reload_also_watches_the_project_folder(
    project, runner, uvicorn_calls, monkeypatch
):
    monkeypatch.chdir(project.parent)
    result = runner.invoke(app, ["runserver", "--reload", "--config", "proj"])
    assert result.exit_code == 0, result.output
    _target, kw = uvicorn_calls[0]
    assert kw["reload"] is True
    assert kw["reload_dirs"] == [str(Path.cwd()), str(Path.cwd() / "proj")]
    # プロジェクトのフォルダで起動したときと --reload なしでは、uvicorn の既定のまま
    monkeypatch.chdir(project)
    assert runner.invoke(app, ["runserver", "--reload"]).exit_code == 0
    assert runner.invoke(app, ["runserver", "--config", str(project)]).exit_code == 0
    assert all("reload_dirs" not in kw for _t, kw in uvicorn_calls[1:])
