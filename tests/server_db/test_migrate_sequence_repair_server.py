"""``monitor-app migrate`` が v2.3.1 で取り込んだ PostgreSQL の遅れた連番を進める(D5-01)。

実行方法は conftest.py を参照(``MONITOR_TEST_PG_URL``)。
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from typer.testing import CliRunner

from monitor_app import MonitorConfig, TableDef
from monitor_app.cli import app
from monitor_app.db.engine import Database
from monitor_app.db.registry import TableRegistry

pytestmark = pytest.mark.server_db

CONFIG_PY = """\
from monitor_app import MonitorConfig, TableDef

config = MonitorConfig(
    tables={"users": TableDef(columns={"id": "int", "name": "str"}, primary_key="id")}
)
"""


@pytest.fixture
def pg_url(server_url):
    if not server_url.startswith("postgresql"):
        pytest.skip("PostgreSQL の連番の確認")
    return server_url


def _sql(url: str, statement: str):
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            result = conn.execute(text(statement))
            return result.scalar() if result.returns_rows else None
    finally:
        engine.dispose()


def test_migrate_advances_a_lagging_sequence(pg_url, tmp_path, monkeypatch):
    config = MonitorConfig(
        tables={
            "users": TableDef(columns={"id": "int", "name": "str"}, primary_key="id")
        }
    )
    db = Database(pg_url)
    try:
        # v2.3.1 と同じ表を作り、id を指定して入れる(連番は進まない)
        TableRegistry(config).metadata.create_all(db.engine)
    finally:
        db.dispose()
    _sql(pg_url, "INSERT INTO users (id, name) VALUES (1,'a'),(2,'b'),(3,'c')")

    (tmp_path / "config.py").write_text(CONFIG_PY, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MONITOR_DATABASE_URL", pg_url)
    monkeypatch.setenv("MONITOR_DB_WAIT_TIMEOUT", "0")
    result = CliRunner().invoke(app, ["migrate"])
    assert result.exit_code == 0, result.output
    assert "テーブル 'users' の連番を表の最大値に合わせました(0 → 3)" in result.stdout
    last = _sql(
        pg_url,
        "SELECT pg_sequence_last_value("
        "CAST(pg_get_serial_sequence('users', 'id') AS regclass))",
    )
    assert last == 3
    # 2 回目は何もしない(遅れていない)
    again = CliRunner().invoke(app, ["migrate"])
    assert again.exit_code == 0, again.output
    assert "連番" not in again.stdout
