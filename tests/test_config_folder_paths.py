"""``--config <別のフォルダ>`` で、プロジェクトの DB の代わりに空の DB を作って使わないこと。

相対パス(``instances/database.db``・``csv``)は従来どおり作業フォルダが基準。プロジェクトの
外から ``--config ../proj`` で実行すると、作業フォルダに空の DB を作り、0 行の取り込み・
0 件の表示を「成功」として続けていた(2.3.1 は DB を開けずに失敗していた)。

- config.py のフォルダにあって作業フォルダに無い DB(取り込むなら CSV のフォルダも)を
  使う設定なら、何も作らずに止めて ``cd <プロジェクト>`` を案内する。
- 作業フォルダにもあるパス(0.1.x の ``config/config.py`` をプロジェクトのフォルダから
  使う形)は、これまでどおり作業フォルダのものを使い、警告もしない。
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from contract import helpers
from typer.testing import CliRunner

from monitor_app.cli import app

LEGACY_V1 = Path(__file__).parent / "contract" / "fixtures" / "legacy_v1_config.py"
SCAFFOLD_CSV = Path(__file__).parent.parent / "monitor_app" / "scaffold" / "csv"


@pytest.fixture(autouse=True)
def _quiet_logging(monkeypatch):
    monkeypatch.setattr(
        "monitor_app.logging_conf.configure_logging", lambda *a, **k: None
    )


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def project_with_data(tmp_path, runner, monkeypatch) -> Path:
    """取り込み済み(DB あり)のプロジェクト。"""
    project = helpers.start_project(tmp_path)
    monkeypatch.chdir(project)
    assert runner.invoke(app, ["import-csv"]).exit_code == 0
    assert (project / "instances" / "database.db").is_file()
    return project


@pytest.fixture
def sibling(tmp_path, monkeypatch) -> Path:
    folder = tmp_path / "work"
    folder.mkdir()
    monkeypatch.chdir(folder)
    return folder


@pytest.fixture
def uvicorn_calls(monkeypatch) -> list:
    calls: list = []
    monkeypatch.setattr("uvicorn.run", lambda target, **kw: calls.append(kw))
    monkeypatch.setenv("MONITOR_CONFIG_PATH", "")
    return calls


def _assert_stopped(result, folder: Path, project: Path, names: str) -> None:
    assert result.exit_code == 1, result.output
    assert f"相対パス({names})は今のフォルダ({folder})を基準にしますが" in result.stderr
    assert f"config.py のフォルダ({project})にあります" in result.stderr
    assert f"cd {project}" in result.stderr
    assert not (folder / "instances").exists()  # 空の DB を作らない


def test_import_csv_from_a_sibling_folder_stops_without_creating_a_db(
    project_with_data, sibling, runner
):
    result = runner.invoke(app, ["import-csv", "--keep", "--config", "../proj"])
    _assert_stopped(result, sibling, project_with_data, "instances/database.db、csv")
    assert "取り込み完了" not in result.stdout


def test_runserver_from_the_parent_folder_does_not_serve_an_empty_db(
    project_with_data, runner, uvicorn_calls, monkeypatch
):
    parent = project_with_data.parent
    monkeypatch.chdir(parent)
    result = runner.invoke(app, ["runserver", "--config", "proj"])
    _assert_stopped(result, parent, project_with_data, "instances/database.db、csv")
    assert uvicorn_calls == []


@pytest.mark.parametrize(
    "command", [["migrate"], ["backup"], ["restore", "x.db", "--yes"]]
)
def test_maintenance_commands_stop_too(project_with_data, sibling, runner, command):
    result = runner.invoke(app, [*command, "--config", str(project_with_data)])
    assert result.exit_code == 1, result.output
    assert "instances/database.db" in result.stderr
    assert "config.py のフォルダ" in result.stderr
    assert not (sibling / "instances").exists()


def test_check_only_warns_about_the_mismatch(
    project_with_data, sibling, runner, caplog
):
    caplog.set_level(logging.WARNING, logger="monitor_app.cli")
    logging.getLogger("monitor_app").propagate = True
    runner.invoke(app, ["check", "--config", str(project_with_data)])
    warnings = [r.getMessage() for r in caplog.records if r.name == "monitor_app.cli"]
    assert any("config.py のフォルダ" in w for w in warnings)


def test_inside_the_project_nothing_changes(project_with_data, runner, caplog):
    caplog.set_level(logging.WARNING, logger="monitor_app.cli")
    logging.getLogger("monitor_app").propagate = True
    result = runner.invoke(app, ["import-csv", "--keep", "--config", "."])
    assert result.exit_code == 0, result.output
    assert not [r for r in caplog.records if r.name == "monitor_app.cli"]


def test_legacy_v01_layout_run_from_the_project_root_has_no_false_warning(
    tmp_path, runner, monkeypatch, caplog
):
    root = tmp_path / "v01"
    (root / "config").mkdir(parents=True)
    (root / "csv").mkdir()
    (root / "instances").mkdir()
    (root / "config" / "config.py").write_text(
        LEGACY_V1.read_text(encoding="utf-8"), encoding="utf-8"
    )
    for csv in SCAFFOLD_CSV.glob("*.csv"):
        (root / "csv" / csv.name).write_bytes(csv.read_bytes())
    monkeypatch.chdir(root)
    caplog.set_level(logging.WARNING, logger="monitor_app.cli")
    logging.getLogger("monitor_app").propagate = True
    result = runner.invoke(app, ["import-csv", "--config", "config/config.py"])
    assert result.exit_code == 0, result.output
    assert (root / "instances" / "database.db").is_file()
    runner.invoke(app, ["check", "--config", "config/config.py"])
    assert not [r for r in caplog.records if "config.py のフォルダ" in r.getMessage()]


def test_a_project_with_an_absolute_database_still_starts_from_elsewhere(
    tmp_path, runner, uvicorn_calls, monkeypatch, caplog
):
    project = helpers.start_project(tmp_path)
    db = project / "data.db"
    (project / ".env").write_text(
        f"MONITOR_DATABASE_URL={helpers.sqlite_url(db)}\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    caplog.set_level(logging.WARNING, logger="monitor_app.cli")
    logging.getLogger("monitor_app").propagate = True
    # CSV を取り込まない起動なら、CSV のフォルダのずれは警告だけ
    result = runner.invoke(app, ["runserver", "--config", "proj"])
    assert result.exit_code == 0, result.output
    assert len(uvicorn_calls) == 1
    assert any("相対パス(csv)" in r.getMessage() for r in caplog.records)
    # 取り込むなら止める(作業フォルダの無い csv/ から 0 行を取り込まない)
    result = runner.invoke(
        app, ["runserver", "--config", "proj", "--import-csv-if-empty"]
    )
    assert result.exit_code == 1, result.output
    assert len(uvicorn_calls) == 1


def test_paths_that_exist_in_both_folders_keep_the_working_folder(
    project_with_data, sibling, runner
):
    (sibling / "csv").mkdir()
    (sibling / "instances").mkdir()
    (sibling / "instances" / "database.db").touch()  # 作業フォルダ用の DB(空)
    result = runner.invoke(app, ["import-csv", "--keep", "--config", "../proj"])
    assert result.exit_code == 0, result.output
    # 従来どおり作業フォルダの DB を使う(プロジェクトの DB には触れない)
    assert (sibling / "instances" / "database.db").stat().st_size > 0
