"""入力フォームのあるテーブルを置換で取り込むときの警告(DX-01)。

置換(``import-csv`` / ``runserver --import-csv`` / 置換モードのフォルダ監視)は、
フォーム・画面・API から入力した行も CSV の内容で置き換える。消えることと残す方法を
知らせる(追記・空のときだけの取り込みでは出さない)。
"""

from __future__ import annotations

import logging

import pytest
from contract import helpers

from monitor_app import MonitorConfig, TableDef
from monitor_app.cli.data import run_csv_import
from monitor_app.db.engine import Database
from monitor_app.db.registry import TableRegistry
from monitor_app.services.importer import CsvImporter
from monitor_app.services.ingest_watcher import IngestWatcher
from monitor_app.settings.declarative import FormDef
from monitor_app.settings.loader import load_config
from monitor_app.settings.runtime import AppSettings


@pytest.fixture(autouse=True)
def _propagate_monitor_logs(monkeypatch):
    """CLI のテストが monitor_app を propagate=False にしていても caplog で拾う。"""
    monkeypatch.setattr(logging.getLogger("monitor_app"), "propagate", True)


@pytest.fixture
def scaffold(tmp_path, monkeypatch):
    helpers.isolate_from_host(monkeypatch, tmp_path)
    project = helpers.start_project(tmp_path)
    monkeypatch.chdir(project)
    settings = AppSettings(
        _env_file=None,
        database_url=helpers.sqlite_url(project / "instances" / "database.db"),
        csv_dir=project / "csv",
    )
    return settings, load_config(str(project / "config.py"))


def _form_warnings(caplog) -> list:
    return [m for m in caplog.messages if "を CSV の内容で置き換えました" in m]


def test_replace_import_warns_about_form_tables(scaffold, caplog):
    settings, config = scaffold
    assert [n for n, t in config.tables.items() if t.form is not None] == ["orders"]
    with caplog.at_level(logging.WARNING, logger="monitor_app.cli"):
        run_csv_import(settings, config)
    (message,) = _form_warnings(caplog)
    assert message.startswith("入力フォームのある orders を CSV の内容で置き換えました")
    assert "--import-csv-if-empty" in message and "--keep" in message


def test_form_tables_that_were_not_replaced_do_not_warn(scaffold, caplog):
    """ヘッダの列が足りず置換しなかった(既存の行を残した)テーブルでは警告しない。"""
    settings, config = scaffold
    run_csv_import(settings, config)
    orders = settings.csv_dir / "orders.csv"
    header = orders.read_text(encoding="utf-8").splitlines()[0]
    orders.write_text(",".join(header.split(",")[:-1]) + "\n", encoding="utf-8")
    caplog.clear()  # 1 回目(実際に置き換えた)の警告は数えない
    with caplog.at_level(logging.WARNING, logger="monitor_app.cli"):
        report = run_csv_import(settings, config)
    assert [r.table for r in report.results if r.refused] == ["orders"]
    assert _form_warnings(caplog) == []


@pytest.mark.parametrize(
    "kwargs", [{"keep": True}, {"only_empty": True}], ids=["keep", "if-empty"]
)
def test_non_destructive_imports_do_not_warn(scaffold, caplog, kwargs):
    settings, config = scaffold
    with caplog.at_level(logging.WARNING, logger="monitor_app.cli"):
        run_csv_import(settings, config, **kwargs)
    assert _form_warnings(caplog) == []


def _watcher(tmp_path, *, keep: bool) -> IngestWatcher:
    config = MonitorConfig(
        tables={
            "log": TableDef(columns={"id": "int", "v": "str"}),
            "entry": TableDef(columns={"id": "int", "v": "str"}, form=FormDef()),
        }
    )
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()
    db = Database(helpers.sqlite_url(tmp_path / "app.db"))
    importer = CsvImporter(config, TableRegistry(config), db, csv_dir)
    return IngestWatcher(importer, 1.0, keep=keep)


def test_replace_watcher_warns_once_at_startup(tmp_path, caplog):
    watcher = _watcher(tmp_path, keep=False)
    with caplog.at_level(logging.WARNING, logger="monitor_app.ingest"):
        watcher.setup()
        watcher.poll_once()
    (message,) = [m for m in caplog.messages if "MONITOR_INGEST_MODE" in m]
    assert "入力フォームのある entry では" in message
    assert "MONITOR_INGEST_MODE=append" in message
    watcher.importer.db.dispose()


def test_append_watcher_does_not_warn(tmp_path, caplog):
    watcher = _watcher(tmp_path, keep=True)
    with caplog.at_level(logging.WARNING, logger="monitor_app.ingest"):
        watcher.setup()
    assert [m for m in caplog.messages if "MONITOR_INGEST_MODE" in m] == []
    watcher.importer.db.dispose()
