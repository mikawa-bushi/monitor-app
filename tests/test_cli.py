"""Typer CLI の単体テスト(``CliRunner``)。

実サーバ起動やネットワークは行わず、scaffold 生成・設定検証・CSV 取り込み・
ソース同期・``runserver``(uvicorn はモック)の各コマンドの分岐を検証する。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from monitor_app.cli import app

SCAFFOLD_CONFIG = (
    Path(__file__).parent.parent / "monitor_app" / "scaffold" / "config.py"
)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def project(tmp_path, monkeypatch, runner) -> Path:
    """startproject で雛形を作り、その中を cwd にして返す。"""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["startproject", "proj"])
    assert result.exit_code == 0, result.output
    proj = tmp_path / "proj"
    monkeypatch.chdir(proj)
    return proj


# ---------------------------------------------------------------------------
# startproject
# ---------------------------------------------------------------------------
def test_startproject_creates_scaffold(tmp_path, monkeypatch, runner):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["startproject", "proj"])
    assert result.exit_code == 0, result.output

    proj = tmp_path / "proj"
    assert (proj / "config.py").exists()
    assert (proj / ".env.example").exists()
    assert (proj / "README.md").exists()
    assert (proj / "instances" / ".gitkeep").exists()
    # サンプル CSV がコピーされている。
    assert {p.name for p in (proj / "csv").glob("*.csv")} == {
        "users.csv",
        "products.csv",
        "orders.csv",
    }


def test_startproject_fails_if_dir_exists(tmp_path, monkeypatch, runner):
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["startproject", "proj"]).exit_code == 0
    again = runner.invoke(app, ["startproject", "proj"])
    assert again.exit_code == 1
    assert "既に存在" in again.output


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------
def test_check_ok_on_scaffold_config(runner):
    result = runner.invoke(app, ["check", "--config", str(SCAFFOLD_CONFIG)])
    assert result.exit_code == 0, result.output
    assert "OK" in result.output


def test_check_missing_config(tmp_path, monkeypatch, runner):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["check", "--config", "does_not_exist.py"])
    assert result.exit_code == 1
    assert "見つかりません" in result.output


def test_check_invalid_config(tmp_path, monkeypatch, runner):
    bad = tmp_path / "bad_config.py"
    bad.write_text("x = 1\n", encoding="utf-8")  # config も ALLOWED_TABLES も無い
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["check", "--config", str(bad)])
    assert result.exit_code == 1


# ---------------------------------------------------------------------------
# import-csv
# ---------------------------------------------------------------------------
def _count_rows(proj: Path, table: str) -> int:
    con = sqlite3.connect(proj / "instances" / "database.db")
    try:
        return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        con.close()


def test_import_csv_replace(project, runner):
    result = runner.invoke(app, ["import-csv"])
    assert result.exit_code == 0, result.output
    assert "取り込み完了" in result.output
    assert _count_rows(project, "users") > 0


def test_import_csv_keep_inserts(project, runner):
    # keep=True(追記)経路。空テーブルへの初回追記で行が入る。
    # 同一 CSV の再追記は主キー重複で失敗するのが正しい挙動なので、ここでは
    # 追記ブランチが通ること(行が入ること)だけを確認する。
    result = runner.invoke(app, ["import-csv", "--keep"])
    assert result.exit_code == 0, result.output
    assert _count_rows(project, "users") > 0


def test_import_csv_invalid_config(tmp_path, monkeypatch, runner):
    bad = tmp_path / "bad.py"
    bad.write_text("x = 1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["import-csv", "--config", str(bad)])
    assert result.exit_code == 1


# ---------------------------------------------------------------------------
# sync-sources
# ---------------------------------------------------------------------------
def test_sync_sources_without_sources(project, runner):
    result = runner.invoke(app, ["sync-sources"])
    assert result.exit_code == 0, result.output
    assert "sources が定義されていません" in result.output


def test_sync_sources_invalid_config(tmp_path, monkeypatch, runner):
    bad = tmp_path / "bad.py"
    bad.write_text("x = 1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["sync-sources", "--config", str(bad)])
    assert result.exit_code == 1


def test_sync_sources_imports_jsonl(tmp_path, monkeypatch, runner):
    # jsonl ソースを定義した config で、コネクタ経由の取り込み本体を通す。
    proj = tmp_path / "proj"
    (proj / "instances").mkdir(parents=True)
    jsonl = proj / "events.jsonl"
    jsonl.write_text(
        json.dumps({"id": 1, "name": "a", "value": 1.0, "ts": "t1"})
        + "\n"
        + json.dumps({"id": 2, "name": "b", "value": 2.0, "ts": "t2"})
        + "\n",
        encoding="utf-8",
    )
    (proj / "config.py").write_text(
        "from monitor_app import MonitorConfig, TableDef\n"
        "from monitor_app.settings.declarative import SourceDef\n"
        "config = MonitorConfig(\n"
        "    tables={'events': TableDef(\n"
        "        columns={'id': 'int', 'name': 'str', 'value': 'float', 'ts': 'str'},\n"
        "        primary_key='id')},\n"
        "    sources={'src': SourceDef(\n"
        f"        kind='jsonl', path=r'{jsonl}', table='events', mode='append')}},\n"
        ")\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(proj)

    result = runner.invoke(app, ["sync-sources"])
    assert result.exit_code == 0, result.output
    assert "同期完了" in result.output
    assert _count_rows(proj, "events") == 2


# ---------------------------------------------------------------------------
# runserver(uvicorn をモック)
# ---------------------------------------------------------------------------
def test_runserver_invokes_uvicorn(project, runner, monkeypatch):
    monkeypatch.setenv("MONITOR_CONFIG_PATH", "")  # 後始末を monkeypatch に任せる
    calls = []
    monkeypatch.setattr("uvicorn.run", lambda target, **kw: calls.append((target, kw)))

    result = runner.invoke(app, ["runserver"])
    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    target, kw = calls[0]
    assert target == "monitor_app.main:app_factory"
    assert kw["factory"] is True
    assert kw["host"] == "127.0.0.1"
    assert kw["port"] == 9990


def test_runserver_with_import_csv(project, runner, monkeypatch):
    monkeypatch.setenv("MONITOR_CONFIG_PATH", "")
    calls = []
    monkeypatch.setattr("uvicorn.run", lambda target, **kw: calls.append((target, kw)))

    result = runner.invoke(app, ["runserver", "--import-csv"])
    assert result.exit_code == 0, result.output
    assert len(calls) == 1  # サーバ起動は試みる
    assert _count_rows(project, "users") > 0  # 起動前に取り込まれている


def test_runserver_honors_host_and_port(project, runner, monkeypatch):
    monkeypatch.setenv("MONITOR_CONFIG_PATH", "")
    calls = []
    monkeypatch.setattr("uvicorn.run", lambda target, **kw: calls.append((target, kw)))

    result = runner.invoke(app, ["runserver", "--host", "0.0.0.0", "--port", "8123"])
    assert result.exit_code == 0, result.output
    _, kw = calls[0]
    assert kw["host"] == "0.0.0.0"
    assert kw["port"] == 8123
