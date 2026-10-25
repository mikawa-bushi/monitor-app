"""``import-csv`` の取り込みの要約と ``--if-empty`` / ``--force``、``runserver --force``。

取り込みの前に設定・DB・どの CSV をどのテーブルへ入れるかを表示し、テーブルごとに
列の対応(一致・表記ゆれ・CSV に無い・取り込まない)と警告を示す。置換を中止した
テーブルがあれば終了コード 1(スクリプトが気付けるように)。
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from contract import helpers
from typer.testing import CliRunner

from monitor_app.cli import app
from monitor_app.cli import server as server_mod


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    helpers.isolate_from_host(monkeypatch, tmp_path)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def project(tmp_path, monkeypatch) -> Path:
    proj = helpers.start_project(tmp_path)
    monkeypatch.chdir(proj)
    return proj


def _rows(proj: Path, sql: str) -> list[tuple]:
    with closing(sqlite3.connect(proj / "instances" / "database.db")) as conn:
        return conn.execute(sql).fetchall()


def _count(proj: Path, table: str) -> int:
    return _rows(proj, f"SELECT COUNT(*) FROM {table}")[0][0]


# ---------------------------------------------------------------------------
# 取り込みの前の要約
# ---------------------------------------------------------------------------
def test_summary_shows_config_database_and_matched_csv_files(project, runner):
    result = runner.invoke(app, ["import-csv"])
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    here = Path.cwd()  # 表示は作業フォルダからの絶対パス(macOS の /private/var も同じ)
    assert lines[0] == f"設定: {here / 'config.py'}"
    assert lines[1] == f"データベース: SQLite {here / 'instances' / 'database.db'}"
    assert lines[2] == (
        f"CSV: {here / 'csv'}(users.csv → users、products.csv → products、"
        "orders.csv → orders)"
    )
    assert "    一致した列: id, name, email" in lines
    assert lines[-1] == "✅ 取り込み完了: 合計 13 行"


def test_summary_lists_tables_without_a_csv(project, runner):
    (project / "csv" / "orders.csv").unlink()
    result = runner.invoke(app, ["import-csv"])
    assert result.exit_code == 0, result.output
    assert "CSV の無いテーブル: orders" in result.stdout


def test_no_matching_csv_warns_with_the_expected_file_names(project, runner):
    for path in (project / "csv").glob("*.csv"):
        path.rename(path.with_suffix(".bak"))
    result = runner.invoke(app, ["import-csv"])
    assert result.exit_code == 0, result.output
    assert "テーブルに対応する CSV がありません" in result.stdout
    assert "(取り込むファイル名: users.csv, products.csv, orders.csv)" in result.stdout


def test_missing_csv_folder_is_explained(project, runner, monkeypatch):
    monkeypatch.setenv("MONITOR_CSV_DIR", "no_such_folder")
    result = runner.invoke(app, ["import-csv"])
    assert result.exit_code == 0, result.output
    assert f"CSV のフォルダがありません: {Path.cwd() / 'no_such_folder'}" in (
        result.stdout
    )
    assert "MONITOR_CSV_DIR で指定できます" in result.stdout


# ---------------------------------------------------------------------------
# 列の対応・重複・対応しないファイル
# ---------------------------------------------------------------------------
def test_header_variants_and_unknown_columns_are_listed(project, runner):
    (project / "csv" / "users.csv").write_text(
        "id,Name,email,memo\n1,a,a@example.com,x\n", encoding="utf-8"
    )
    result = runner.invoke(app, ["import-csv"])
    assert result.exit_code == 0, result.output
    out = result.stdout
    assert "    表記ゆれを対応付けた列: 'Name' → name" in out
    assert "    取り込まない CSV の列: memo" in out


def test_auto_numbered_primary_key_is_marked(project, runner):
    (project / "csv" / "users.csv").write_text(
        "name,email\na,a@example.com\n", encoding="utf-8"
    )
    result = runner.invoke(app, ["import-csv"])
    assert result.exit_code == 0, result.output
    assert "    CSV に無い列: id(主キー・自動採番)" in result.stdout


def test_duplicates_skipped_by_keep_are_totalled(project, runner):
    assert runner.invoke(app, ["import-csv"]).exit_code == 0
    result = runner.invoke(app, ["import-csv", "--keep"])
    assert result.exit_code == 0, result.output
    assert "users.csv: 主キーが既にある 5 行を読み飛ばしました(追記)" in result.stdout
    assert result.stdout.rstrip().endswith(
        "✅ 取り込み完了: 合計 0 行(主キーが既にある 13 行は読み飛ばし)"
    )


def test_unmatched_csv_files_get_a_suggestion(project, runner):
    (project / "csv" / "Users2.csv").write_text("id\n1\n", encoding="utf-8")
    (project / "csv" / "USERS.CSV").write_text("id\n1\n", encoding="utf-8")
    result = runner.invoke(app, ["import-csv"])
    assert result.exit_code == 0, result.output
    assert "⚠️  Users2.csv: 対応するテーブルがありません" in result.stdout
    if not (project / "csv" / "users.csv").samefile(project / "csv" / "USERS.CSV"):
        assert "テーブル 'users' のつもりならファイル名を users.csv に" in (
            result.stdout
        )


# ---------------------------------------------------------------------------
# 置換の中止と --force
# ---------------------------------------------------------------------------
def _drop_email_column(proj: Path) -> None:
    (proj / "csv" / "users.csv").write_text("id,name\n1,A\n2,B\n", encoding="utf-8")


def test_refused_replace_exits_one_and_keeps_the_rows(project, runner):
    assert runner.invoke(app, ["import-csv"]).exit_code == 0
    before = _rows(project, "SELECT * FROM users ORDER BY id")
    _drop_email_column(project)
    result = runner.invoke(app, ["import-csv"])
    assert result.exit_code == 1
    assert "  users: 0 行 (skip 0) — 置換を中止(既存の行を残しました)" in result.stdout
    assert "    CSV に無い列: email" in result.stdout
    assert "置換を中止したテーブルがあります: users" in result.stderr
    assert "--force で置き換えて" in result.stderr
    assert "✅ 取り込み完了" not in result.output
    assert _rows(project, "SELECT * FROM users ORDER BY id") == before
    # ほかのテーブルは取り込んでいる
    assert _count(project, "products") == 3


def test_force_replaces_even_when_a_column_is_missing(project, runner):
    assert runner.invoke(app, ["import-csv"]).exit_code == 0
    _drop_email_column(project)
    result = runner.invoke(app, ["import-csv", "--force"])
    assert result.exit_code == 0, result.output
    assert "CSV のヘッダにありません(空のまま取り込みます)" in result.stdout
    assert _rows(project, "SELECT id, name, email FROM users ORDER BY id") == [
        (1, "A", None),
        (2, "B", None),
    ]


def test_keep_only_warns_about_missing_columns(project, runner):
    _drop_email_column(project)
    result = runner.invoke(app, ["import-csv", "--keep"])
    assert result.exit_code == 0, result.output
    assert _count(project, "users") == 2


# ---------------------------------------------------------------------------
# --if-empty
# ---------------------------------------------------------------------------
def test_if_empty_imports_only_into_empty_tables(project, runner):
    first = runner.invoke(app, ["import-csv", "--if-empty"])
    assert first.exit_code == 0, first.output
    assert first.stdout.rstrip().endswith("✅ 取り込み完了: 合計 13 行")
    # 画面やフォームで入力した行を残す(置き換えない)
    with closing(sqlite3.connect(project / "instances" / "database.db")) as conn:
        conn.execute("DELETE FROM orders")
        conn.execute(
            "INSERT INTO orders (id, user_id, product_id, amount) VALUES (99, 1, 1, 7)"
        )
        conn.commit()
    second = runner.invoke(app, ["import-csv", "--if-empty"])
    assert second.exit_code == 0, second.output
    assert "  users: 行があるため取り込みませんでした(--if-empty)" in second.stdout
    assert _rows(project, "SELECT id, amount FROM orders") == [(99, 7.0)]


def test_if_empty_and_keep_cannot_be_combined(project, runner):
    result = runner.invoke(app, ["import-csv", "--keep", "--if-empty"])
    assert result.exit_code == 1
    assert "--keep と --if-empty は同時に指定できません" in result.stderr
    assert "Traceback" not in result.output
    assert not (project / "instances" / "database.db").exists()


def test_options_reach_run_csv_import(project, runner, monkeypatch):
    from monitor_app.cli import data as data_mod
    from monitor_app.services.importer import ImportReport

    calls: list[dict] = []

    def fake(settings, config, **kwargs):
        calls.append(kwargs)
        return ImportReport()

    monkeypatch.setattr(data_mod, "run_csv_import", fake)
    for args in (["--if-empty"], ["--force"], ["--keep", "--force"]):
        assert runner.invoke(app, ["import-csv", *args]).exit_code == 0
    assert calls == [
        {"keep": False, "only_empty": True, "force": False},
        {"keep": False, "only_empty": False, "force": True},
        {"keep": True, "only_empty": False, "force": True},
    ]


# ---------------------------------------------------------------------------
# runserver: --force と起動時の要約
# ---------------------------------------------------------------------------
@pytest.fixture
def uvicorn_calls(monkeypatch) -> list:
    calls: list = []
    monkeypatch.setattr("uvicorn.run", lambda target, **kw: calls.append((target, kw)))
    monkeypatch.setenv("MONITOR_CONFIG_PATH", "")  # runserver が書き換える値の後始末
    return calls


def test_runserver_force_replaces_a_table_with_missing_columns(
    project, runner, uvicorn_calls
):
    assert runner.invoke(app, ["import-csv"]).exit_code == 0
    _drop_email_column(project)
    assert runner.invoke(app, ["runserver", "--import-csv"]).exit_code == 0
    assert _count(project, "users") == 5  # --force なしは置換を中止して残す
    result = runner.invoke(app, ["runserver", "--import-csv", "--force"])
    assert result.exit_code == 0, result.output
    assert _count(project, "users") == 2
    assert len(uvicorn_calls) == 2


def test_runserver_logs_the_config_and_csv_files(
    project, runner, uvicorn_calls, caplog
):
    caplog.set_level(logging.INFO, logger="monitor_app.cli")
    result = runner.invoke(app, ["runserver", "--import-csv-if-empty"])
    assert result.exit_code == 0, result.output
    messages = [r.getMessage() for r in caplog.records if r.name == "monitor_app.cli"]
    here = Path.cwd()
    assert (
        f"設定: {here / 'config.py'}、CSV: {here / 'csv'}(users.csv → users、"
        "products.csv → products、orders.csv → orders)"
    ) in messages


def test_runserver_warns_when_importing_without_any_csv(
    project, runner, uvicorn_calls, caplog, monkeypatch
):
    monkeypatch.setenv("MONITOR_CSV_DIR", "empty_csv")
    (project / "empty_csv").mkdir()
    caplog.set_level(logging.INFO, logger="monitor_app.cli")
    assert runner.invoke(app, ["runserver"]).exit_code == 0
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == []  # 取り込まないなら CSV が無くても警告しない
    assert runner.invoke(app, ["runserver", "--import-csv"]).exit_code == 0
    assert any(
        "テーブルに対応する CSV がありません" in r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING
    )


def test_runserver_force_is_passed_only_to_the_replace_import(
    project, runner, uvicorn_calls, monkeypatch
):
    calls: list[dict] = []
    monkeypatch.setattr(
        server_mod, "run_csv_import", lambda settings, cfg, **kw: calls.append(kw)
    )
    assert runner.invoke(app, ["runserver", "--import-csv", "--force"]).exit_code == 0
    assert runner.invoke(app, ["runserver", "--import-csv-if-empty"]).exit_code == 0
    assert calls == [{"force": True}, {"only_empty": True}]
