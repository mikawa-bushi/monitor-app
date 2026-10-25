"""既知の不具合のトリップワイヤ(監査で再現済みのもの)。

各テストは「直ったあとの正しい挙動」を表明し、``xfail(strict=True)`` を付けてある。
reason の先頭は監査の指摘 ID。不具合を直した作業パッケージは、該当テストの xfail
マーカーを外すこと(strict のため、直ったのにマーカーが残っていると XPASS で失敗する)。
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from urllib.parse import unquote

import pytest
from contract import helpers, snapshot
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from monitor_app import AlertRule, MonitorConfig, TableDef, ViewDef
from monitor_app.cli import app as cli_app
from monitor_app.db.engine import Database
from monitor_app.db.registry import TableRegistry
from monitor_app.main import create_app
from monitor_app.services.importer import CsvImporter
from monitor_app.services.ingest_watcher import IngestWatcher
from monitor_app.services.notifiers.base import ConsoleNotifier
from monitor_app.settings.runtime import AppSettings


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """開発者の MONITOR_* と作業ディレクトリの .env を持ち込まない。"""
    helpers.isolate_from_host(monkeypatch, tmp_path)


@pytest.fixture
def scaffold(tmp_path, monkeypatch) -> Path:
    """``startproject`` で作った雛形のプロジェクト(作業ディレクトリもそこへ移す)。"""
    project = helpers.start_project(tmp_path)
    monkeypatch.chdir(project)
    return project


def _settings(database_url: str = "sqlite://") -> AppSettings:
    return AppSettings(_env_file=None, database_url=database_url)


def test_dx03_check_succeeds_when_stdout_is_cp932(scaffold):
    """日本語版 Windows でリダイレクト/パイプした標準出力(cp932)でも成功を正しく返す。

    DX-03 は修正済み(CLI 起動時に UTF-8 以外の出力先を ``backslashreplace`` にする)。
    再発防止のため通常のテストとして残す。
    """
    env = helpers.child_env(PYTHONIOENCODING="cp932", PYTHONUTF8="0")
    proc = helpers.run_cli("check", cwd=scaffold, env=env)
    assert proc.returncode == 0, proc.stderr.decode("cp932", "replace")


def test_dx02_missing_parent_directories_of_the_sqlite_file_are_created(tmp_path):
    db_path = tmp_path / "missing_dir" / "sub" / "db.sqlite"
    config = MonitorConfig(
        tables={"events": TableDef(columns={"id": "int", "name": "str"})}
    )
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()
    (csv_dir / "events.csv").write_text("id,name\n1,a\n2,b\n", encoding="utf-8")

    db = Database(helpers.sqlite_url(db_path))
    report = CsvImporter(config, TableRegistry(config), db, csv_dir).import_all()

    assert report.total_inserted == 2
    assert db_path.exists()


def test_perf09_paginated_reads_do_not_flap_alert_notifications(monkeypatch):
    levels: list[str] = []
    monkeypatch.setattr(
        ConsoleNotifier, "send", lambda self, subject, body, level: levels.append(level)
    )
    config = MonitorConfig(
        tables={"t": TableDef(columns={"id": "int", "temp": "float"})},
        views={"v": ViewDef(query="SELECT id, temp FROM t ORDER BY id")},
        alerts=[
            AlertRule(
                view="v",
                column="temp",
                op=">",
                value=80,
                level="critical",
                notify=["console"],
            )
        ],
    )
    # lifespan を起動しない(AlertScheduler に裏で評価させず、GET の影響だけを見る)
    client = TestClient(create_app(config, _settings()))
    client.post("/api/ingest/t", json=[{"id": 1, "temp": 20}, {"id": 2, "temp": 95}])

    client.get("/api/views/v")  # 全件: 2 行目が違反 → 発火
    client.get("/api/views/v", params={"limit": 1})  # 先頭 1 行(違反なし)だけのページ
    client.get("/api/views/v")  # 全件: 状態は変わっていない

    deadline = time.monotonic() + 2.0  # 通知が非同期に送られる実装でも拾えるように
    while not levels and time.monotonic() < deadline:
        time.sleep(0.02)
    assert levels.count("critical") == 1, levels  # 発火の通知は 1 回だけ
    assert [level for level in levels if level != "critical"] == []  # 復帰の通知なし


def test_fe06_csv_export_of_a_japanese_named_view():
    config = MonitorConfig(
        tables={"t": TableDef(columns=["id", "name"])},
        views={"生産実績": ViewDef(query="SELECT id, name FROM t")},
    )
    client = TestClient(create_app(config, _settings()), raise_server_exceptions=False)
    client.post("/api/tables/t", json={"name": "山田"})

    res = client.get("/api/views/生産実績/export")

    assert res.status_code == 200
    disposition = res.headers.get("content-disposition", "")
    # RFC 6266 / RFC 5987 の filename*(UTF-8 をパーセント符号化)で名前を渡す
    match = re.search(r"filename\*\s*=\s*utf-8''([^;\s]+)", disposition, re.IGNORECASE)
    assert match, disposition
    assert unquote(match.group(1)) == "生産実績.csv"


def test_dx01_import_csv_keep_can_be_rerun_without_duplicating_rows(scaffold):
    db_path = scaffold / "instances" / "database.db"
    tables = ("users", "products", "orders")
    runner = CliRunner()
    first = runner.invoke(cli_app, ["import-csv", "--keep"])
    assert first.exit_code == 0, first.output
    before = {
        t: helpers.query(db_path, f"SELECT COUNT(*) FROM {t}")[0][0] for t in tables
    }

    second = runner.invoke(cli_app, ["import-csv", "--keep"])

    assert second.exit_code == 0, second.output
    after = {
        t: helpers.query(db_path, f"SELECT COUNT(*) FROM {t}")[0][0] for t in tables
    }
    assert after == before


def _append(path: Path, text: str) -> None:
    """CSV へ追記し、mtime を確実に進める(粗いタイムスタンプの FS でも変化を検出させる)。"""
    before = path.stat().st_mtime_ns
    with path.open("a", encoding="utf-8", newline="") as fh:
        fh.write(text)
    mtime = max(path.stat().st_mtime_ns, before + 1_000_000_000)
    os.utime(path, ns=(mtime, mtime))


def test_perf06_append_watcher_waits_for_a_complete_last_line(tmp_path):
    config = MonitorConfig(
        tables={
            "readings": TableDef(columns={"id": "int", "line": "str", "temp": "float"})
        }
    )
    db = Database(helpers.sqlite_url(tmp_path / "app.db"))
    registry = TableRegistry(config)
    registry.create_all(db)
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()
    csv_path = csv_dir / "readings.csv"
    csv_path.write_text("id,line,temp\n1,第1ライン,25.5\n", encoding="utf-8")
    importer = CsvImporter(config, registry, db, csv_dir, encoding="utf-8")
    watcher = IngestWatcher(importer, interval=1.0, keep=True)
    watcher.poll_once()

    _append(csv_path, "2,第2ライン,98")  # 書き手が改行の手前でフラッシュした
    watcher.poll_once()
    _append(csv_path, ".7\n")  # 残りが届いて行が完成した
    watcher.poll_once()

    rows = dict(helpers.query(tmp_path / "app.db", "SELECT id, temp FROM readings"))
    assert rows.get(2) == 98.7


def test_deploy13_schema_drift_is_reported_in_the_json_error_envelope(tmp_path):
    """稼働後に config.py へ列を足したときの応答も JSON(エラーエンベロープ)である。

    指摘には「check でずれを報告する」案もあるが、ここでは API の応答で表明する。
    API の応答形は利用者が観測できる契約で、ずれの検出を check・起動時・加算的な
    自動マイグレーションのどこで実装しても成り立つ(check は現状 DB を読まないため、
    その出力を固定すると特定の設計を前提にしてしまう)。自動マイグレーションで列が
    足されて 200 を返すのも正しい挙動として認める。
    """
    url = helpers.sqlite_url(tmp_path / "app.db")
    create_app(
        MonitorConfig(tables={"users": TableDef(columns=["id", "name"])}),
        _settings(url),
    )
    drifted = MonitorConfig(tables={"users": TableDef(columns=["id", "name", "dept"])})
    client = TestClient(
        create_app(drifted, _settings(url)), raise_server_exceptions=False
    )

    res = client.get("/api/tables/users")

    assert snapshot.mime_type(res.headers.get("content-type")) == "application/json", (
        res.status_code,
        res.text[:200],
    )
    body = res.json()
    if res.status_code >= 400:
        assert isinstance(body["detail"]["code"], str)
        assert body["detail"]["message"]
    else:
        assert "dept" in body["columns"]


def test_crit03_replace_import_keeps_rows_when_a_declared_column_is_missing(scaffold):
    db_path = scaffold / "instances" / "database.db"
    runner = CliRunner()
    assert runner.invoke(cli_app, ["import-csv"]).exit_code == 0
    before = helpers.query(db_path, "SELECT id, name, email FROM users ORDER BY id")

    # Excel で再保存したら email 列が落ちた CSV(宣言済みの非主キー列が丸ごと無い)
    (scaffold / "csv" / "users.csv").write_text(
        "id,name\n1,John Doe\n2,Jane Smith\n", encoding="utf-8"
    )
    # 終了コードは問わない(取り込みを拒否して exit 1 でも、警告して exit 0 でもよい)
    runner.invoke(cli_app, ["import-csv"])

    after = helpers.query(db_path, "SELECT id, name, email FROM users ORDER BY id")
    assert after == before
