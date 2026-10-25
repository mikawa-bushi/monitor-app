"""サービス組み立て・CLI での DB の扱い: 設定からの Database 作成、接続先の説明の
ログ、サーバー DB の起動待ち、接続できないときの短いエラー表示。"""

from __future__ import annotations

import logging

import pytest
from contract import helpers
from typer.testing import CliRunner

from monitor_app import MonitorConfig, TableDef
from monitor_app.cli import app as cli_app
from monitor_app.cli._common import open_database
from monitor_app.container import build_services
from monitor_app.db.engine import Database
from monitor_app.settings.runtime import AppSettings


@pytest.fixture(autouse=True)
def _propagate_monitor_logs(monkeypatch):
    """CLI のテストが configure_logging で monitor_app を propagate=False にしていても、
    caplog(root のハンドラ)で monitor_app.* のログを拾えるようにする。"""
    monkeypatch.setattr(logging.getLogger("monitor_app"), "propagate", True)


CONFIG = MonitorConfig(tables={"t": TableDef(columns=["id", "name"])})


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    helpers.isolate_from_host(monkeypatch, tmp_path)


@pytest.fixture
def waits(monkeypatch) -> list:
    calls: list = []
    monkeypatch.setattr(
        Database, "wait_until_ready", lambda self, timeout: calls.append(timeout)
    )
    return calls


@pytest.fixture
def offline_server(monkeypatch):
    """サーバー DB に接続しないよう、テーブル作成と表定義の確認を止める。"""
    monkeypatch.setattr(
        "monitor_app.db.registry.TableRegistry.create_all", lambda *a: None
    )
    monkeypatch.setattr(
        "monitor_app.container.inspect_schema",
        lambda db, registry: type("R", (), {"log": lambda self, log: None})(),
    )


def _settings(**overrides) -> AppSettings:
    return AppSettings(_env_file=None, **overrides)


def test_build_services_uses_the_sqlite_tuning_settings(tmp_path):
    settings = _settings(
        database_url=f"sqlite:///{(tmp_path / 'app.db').as_posix()}",
        sqlite_journal_mode="delete",
        sqlite_busy_timeout_ms=1500,
    )
    services = build_services(CONFIG, settings)
    try:
        assert services.db.sqlite_busy_timeout_ms == 1500
        with services.db.connect() as conn:
            assert conn.exec_driver_sql("PRAGMA journal_mode").scalar() == "delete"
    finally:
        services.close()


def test_build_services_logs_the_database_once(tmp_path, caplog, waits):
    path = tmp_path / "sub" / "app.db"
    with caplog.at_level(logging.INFO, logger="monitor_app.db"):
        services = build_services(
            CONFIG, _settings(database_url=helpers.sqlite_url(path))
        )
    services.close()
    lines = [r.message for r in caplog.records if r.message.startswith("データベース:")]
    assert lines == [f"データベース: SQLite {path}"]
    assert waits == []  # SQLite は起動待ちをしない


def test_build_services_waits_for_server_databases(caplog, waits, offline_server):
    settings = _settings(
        database_url="postgresql+pg8000://monitor:s3cret@db:5432/app",
        db_wait_timeout=7.5,
    )
    with caplog.at_level(logging.INFO, logger="monitor_app.db"):
        services = build_services(CONFIG, settings)
    services.close()
    assert waits == [7.5]
    assert "postgresql+pg8000://monitor:***@db:5432/app" in caplog.text
    assert "s3cret" not in caplog.text


def test_db_wait_can_be_disabled(waits, offline_server):
    settings = _settings(database_url="mysql+pymysql://u:p@db/app", db_wait_timeout=0)
    build_services(CONFIG, settings).close()
    assert waits == []


def test_open_database_waits_for_server_databases(waits):
    settings = _settings(database_url="postgresql+pg8000://u:p@db/app")
    with open_database(settings, CONFIG) as (db, registry):
        assert db.backend == "postgresql" and registry.has("t")
    assert waits == [15.0]  # 既定の MONITOR_DB_WAIT_TIMEOUT


def test_open_database_does_not_wait_for_sqlite(tmp_path, waits):
    settings = _settings(database_url=helpers.sqlite_url(tmp_path / "x" / "app.db"))
    with open_database(settings, CONFIG) as (db, _registry):
        assert db.sqlite_path == tmp_path / "x" / "app.db"
    assert waits == []
    assert (tmp_path / "x").is_dir()  # 保存先フォルダを作る(DX-02)


def test_unreachable_database_is_reported_briefly(tmp_path, monkeypatch):
    project = helpers.start_project(tmp_path)
    monkeypatch.chdir(project)
    monkeypatch.setenv(
        "MONITOR_DATABASE_URL", "postgresql+pg8000://u:secret@127.0.0.1:1/app"
    )
    monkeypatch.setenv("MONITOR_DB_WAIT_TIMEOUT", "0.3")
    result = CliRunner().invoke(cli_app, ["import-csv"])
    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "データベースエラー: Can't create a connection to host 127.0.0.1" in (
        result.stderr
    )
    assert "MONITOR_DATABASE_URL の接続先" in result.stderr
    assert "secret" not in result.output


@pytest.mark.parametrize(
    ("url", "message", "hint"),
    [
        # Heroku などの接続文字列(SQLAlchemy の名前は postgresql)
        (
            "postgres://u:secret@127.0.0.1:1/app",
            "MONITOR_DATABASE_URL のデータベースの種類を読み取れません",
            "postgresql+psycopg:// に書き換えます",
        ),
        (
            # SQLAlchemy 2.0 の文言は URL をそのまま含む(パスワードを表示しない)
            "not a url secret",
            "MONITOR_DATABASE_URL の形式が正しくありません",
            "mysql+pymysql://",
        ),
    ],
    ids=["postgres-scheme", "unparsable"],
)
def test_bad_database_urls_are_reported_briefly(
    tmp_path, monkeypatch, url, message, hint
):
    project = helpers.start_project(tmp_path)
    monkeypatch.chdir(project)
    monkeypatch.setenv("MONITOR_DATABASE_URL", url)
    result = CliRunner().invoke(cli_app, ["import-csv"])
    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert message in result.stderr
    assert hint in result.stderr
    assert "secret" not in result.output


def test_missing_database_driver_is_reported_briefly(tmp_path, monkeypatch):
    """mysql:// は同梱していない mysqlclient を使う。同梱のドライバを案内する。"""
    project = helpers.start_project(tmp_path)
    monkeypatch.chdir(project)
    monkeypatch.setenv("MONITOR_DATABASE_URL", "mysql://u:p@127.0.0.1:1/app")

    def no_driver(*_args, **_kwargs):
        raise ModuleNotFoundError("No module named 'MySQLdb'", name="MySQLdb")

    monkeypatch.setattr("monitor_app.db.engine.create_engine", no_driver)
    result = CliRunner().invoke(cli_app, ["import-csv"])
    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "データベースのドライバ MySQLdb がインストールされていません" in (
        result.stderr
    )
    assert "mysql+pymysql://" in result.stderr


def test_other_missing_modules_are_not_hidden():
    from monitor_app.cli._common import explain_error

    exc = ModuleNotFoundError("No module named 'foo'", name="foo")
    assert explain_error(exc) is None
    assert explain_error(ModuleNotFoundError("x")) is None  # name なし
