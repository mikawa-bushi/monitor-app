"""実際の PostgreSQL / MySQL に接続するテストの設定(マーカー ``server_db``)。

接続先は環境変数で渡す(未設定ならそのデータベースのテストを飛ばす)::

    MONITOR_TEST_PG_URL=postgresql://monitor:monitor@127.0.0.1:5432/monitor
    MONITOR_TEST_MYSQL_URL=mysql+pymysql://monitor:monitor@127.0.0.1:3306/monitor

PostgreSQL は同じ URL を pg8000 と psycopg の両方のドライバで試す(psycopg が
インストールされていなければ psycopg の分だけ飛ばす)。各テストの前後で接続先の
データベースの全テーブルを削除するため、**テスト専用のデータベースを指定すること**。

マーカーは tests/e2e/conftest.py と同じくこのディレクトリで登録する(pyproject.toml と
tests/conftest.py を変更しないため)。
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import MetaData, create_engine
from sqlalchemy.engine import make_url

#: テストの実行前に読む(テストが MONITOR_* を消しても接続先を失わない)。
PG_URL = os.environ.get("MONITOR_TEST_PG_URL", "").strip()
MYSQL_URL = os.environ.get("MONITOR_TEST_MYSQL_URL", "").strip()

DRIVERS = ("postgresql+pg8000", "postgresql+psycopg", "mysql+pymysql")


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "server_db: 実際の PostgreSQL / MySQL に接続するテスト。"
        "MONITOR_TEST_PG_URL / MONITOR_TEST_MYSQL_URL が無ければ飛ばす",
    )


def url_for(driver: str) -> str:
    """``driver`` 用の接続 URL(環境変数が無ければテストを飛ばす)。"""
    if driver.startswith("postgresql"):
        base, name = PG_URL, "MONITOR_TEST_PG_URL"
    else:
        base, name = MYSQL_URL, "MONITOR_TEST_MYSQL_URL"
    if not base:
        pytest.skip(f"{name} が未設定のため {driver} のテストを飛ばします")
    if driver == "postgresql+psycopg":
        pytest.importorskip("psycopg")
    return make_url(base).set(drivername=driver).render_as_string(hide_password=False)


def drop_all_tables(url: str) -> None:
    """接続先の全テーブルを(外部キーの依存順に)削除する。"""
    engine = create_engine(url)
    try:
        metadata = MetaData()
        metadata.reflect(engine)
        metadata.drop_all(engine)
    finally:
        engine.dispose()


@pytest.fixture(params=DRIVERS)
def server_url(request) -> str:
    """空にしたテスト用データベースの URL(ドライバごとにパラメータ化)。"""
    url = url_for(request.param)
    drop_all_tables(url)
    yield url
    drop_all_tables(url)
