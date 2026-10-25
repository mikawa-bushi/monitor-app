"""テスト共通のフィクスチャ。

アプリファクトリにテスト用設定(in-memory SQLite)を注入し、サンプル CSV を
取り込んだ状態の TestClient を提供する。テストどうしが実ポートやログ設定を
通じて影響し合わないための自動フィクスチャ、開発者の環境(シェルの ``MONITOR_*``・
リポジトリ直下の .env)から切り離す自動フィクスチャもここに置く。
"""

import functools
import logging
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from monitor_app import MonitorConfig, TableDef, ViewDef
from monitor_app.db.change_token import SqliteDataVersion
from monitor_app.db.engine import Database
from monitor_app.main import create_app
from monitor_app.services.importer import CsvImporter
from monitor_app.settings.runtime import AppSettings

SCAFFOLD_CSV = Path(__file__).parent.parent / "monitor_app" / "scaffold" / "csv"
#: 開発者のシェルに残っていても外さない接頭辞(サーバー DB・ブラウザのテストの
#: 接続先など、テストを有効にするための変数)。
_KEPT_ENV_PREFIX = "MONITOR_TEST_"
#: rich の色付き出力を強制する環境変数。Typer は GitHub Actions(GITHUB_ACTIONS)でも
#: 端末扱いにするため、ヘルプの文字列が ANSI エスケープで分断される。
_FORCE_COLOR_ENV = ("GITHUB_ACTIONS", "FORCE_COLOR", "PY_COLORS", "TTY_COMPATIBLE")


@pytest.fixture(autouse=True)
def _no_host_monitor_env(monkeypatch, tmp_path_factory):
    """テストを開発者の環境から切り離す(すべてのテストに自動で効く)。

    - シェルの ``MONITOR_*``(``MONITOR_API_KEY`` や ``MONITOR_CSV_ENCODING`` など。
      配備の手順で export したまま、ということがある)を外す。``MONITOR_TEST_*`` は残す。
    - 作業フォルダを空の一時フォルダにする。リポジトリ直下の .env を AppSettings が
      読まず、既定の ``instances/`` などもリポジトリに作られない。作業フォルダを決めて
      使うテストは、これまでどおり各自で ``monkeypatch.chdir`` する(後から効く)。
    - CI(GitHub Actions)でも、CLI の出力を端末の外と同じ色なしの文字列にする。
    """
    for key in list(os.environ):
        upper = key.upper()
        if upper.startswith("MONITOR_") and not upper.startswith(_KEPT_ENV_PREFIX):
            monkeypatch.delenv(key)
    for key in _FORCE_COLOR_ENV:
        monkeypatch.delenv(key, raising=False)
    # Typer は色の強制を import 時に決める(rich_utils.FORCE_TERMINAL)
    try:
        from typer import rich_utils
    except ImportError:  # rich が無い環境では色が付かない
        pass
    else:
        monkeypatch.setattr(rich_utils, "FORCE_TERMINAL", None)
    monkeypatch.chdir(tmp_path_factory.mktemp("cwd"))


@pytest.fixture(autouse=True)
def _close_databases(monkeypatch):
    """テストの中で開いたデータベース(エンジンと変更検知の接続)を、テストの後で閉じる。

    アプリ(create_app)や Database を作って閉じないテストが多く、Python 3.13 以降の
    sqlite3 はそのコネクションが回収されるたびに「unclosed database」の ResourceWarning
    を出す(1 回の実行で 250 件以上。本物の閉じ忘れが埋もれる)。各テストで作ったものを
    覚えておき、テストの後にまとめて閉じる(閉じ済みでも害はない)。インメモリ DB
    (StaticPool の単一の接続)も、テストの後なので破棄してよい。
    """
    databases: list[Database] = []
    tokens: list[SqliteDataVersion] = []

    def tracking(cls, created: list):
        original = cls.__init__

        @functools.wraps(original)
        def init(self, *args, **kwargs):
            original(self, *args, **kwargs)
            created.append(self)

        monkeypatch.setattr(cls, "__init__", init)

    tracking(Database, databases)
    tracking(SqliteDataVersion, tokens)
    yield
    for token in reversed(tokens):
        token.close()
    for db in reversed(databases):
        # エンジンを差し替えたテスト(代役のオブジェクト)もあるため、あれば閉じる
        dispose = getattr(getattr(db, "engine", None), "dispose", None)
        if callable(dispose):
            dispose()


@pytest.fixture
def config() -> MonitorConfig:
    return MonitorConfig(
        tables={
            "users": TableDef(columns=["id", "name", "email"], primary_key="id"),
            "products": TableDef(columns=["id", "name", "price"], primary_key="id"),
            "orders": TableDef(
                columns=["id", "user_id", "product_id", "amount"],
                primary_key="id",
                foreign_keys={"user_id": "users.id", "product_id": "products.id"},
            ),
        },
        views={
            "users_view": ViewDef(query="SELECT * FROM users", title="ユーザー"),
            "orders_summary": ViewDef(
                query=(
                    "SELECT o.id, u.name AS user, o.amount FROM orders o "
                    "JOIN users u ON o.user_id = u.id"
                ),
                title="注文",
            ),
        },
    )


@pytest.fixture
def app(config):
    settings = AppSettings(database_url="sqlite:///:memory:")
    application = create_app(config, settings)
    CsvImporter(
        config, application.state.registry, application.state.db, SCAFFOLD_CSV
    ).import_all()
    return application


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


@pytest.fixture(autouse=True)
def _no_real_port_probe(request, monkeypatch):
    """``runserver`` のポート事前確認で、テストが実際のポートを掴まないようにする。

    uvicorn をモックする CLI テスト(既定ポート 9990 や 0.0.0.0 を使うもの)が、
    開発中に起動しているサーバーやファイアウォールの確認ダイアログに左右されないため。
    事前確認そのものを確かめるテストは ``real_port_probe`` フィクスチャを使う。
    子プロセスで起動するテスト(E2E)には影響しない。
    """
    if "real_port_probe" in request.fixturenames:
        return
    monkeypatch.setattr(
        "monitor_app.cli.server.ensure_port_available", lambda host, port: None
    )


@pytest.fixture
def real_port_probe() -> None:
    """``runserver`` のポート事前確認を本物のまま使う(``_no_real_port_probe`` を外す)。"""


#: ``configure_logging``(dictConfig)が設定する名前付きのロガー。
_CONFIGURED_LOGGERS = (
    "monitor_app",
    "sqlalchemy.engine",
    "uvicorn",
    "uvicorn.error",
    "uvicorn.access",
)


def _is_dead(handler: logging.Handler) -> bool:
    return getattr(getattr(handler, "stream", None), "closed", False)


@pytest.fixture(autouse=True)
def _drop_dead_log_handlers():
    """インプロセスで実行した CLI の ``configure_logging`` が変えたログ設定を、各テストの後に
    元へ戻す。

    - dictConfig はハンドラを CliRunner の一時出力に向けたまま残す。後続のテストのログ
      (lifespan で起動するバックグラウンドタスクなど)がその閉じたストリームへ書き込み、
      "--- Logging error ---" を出すのを防ぐ(ルートは閉じたハンドラだけを外す。pytest が
      テストの段階ごとにルートへ付け外しする捕捉用のハンドラには触れない)。
    - ``monitor_app`` などは propagate=False のまま残り、後続のテストの caplog がテストの
      順序によってログを拾えなくなる。名前付きのロガーは、テスト前の状態に戻す。
    """
    saved = {}
    for name in _CONFIGURED_LOGGERS:
        logger = logging.getLogger(name)
        saved[name] = (
            logger.level,
            logger.propagate,
            logger.disabled,
            list(logger.handlers),
        )
    yield
    for name, (level, propagate, disabled, handlers) in saved.items():
        logger = logging.getLogger(name)
        logger.setLevel(level)
        logger.propagate = propagate
        logger.disabled = disabled
        logger.handlers[:] = [h for h in handlers if not _is_dead(h)]
    root = logging.getLogger()
    for handler in list(root.handlers):
        if _is_dead(handler):
            root.removeHandler(handler)
