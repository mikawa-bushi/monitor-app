"""PostgreSQL の遅れた連番の修正(D5-01)のうち、サーバーなしで確かめられる部分。

実サーバーでの確認は tests/server_db/test_pg_sequence_repair.py。
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from types import SimpleNamespace

from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import OperationalError

from monitor_app import MonitorConfig, TableDef
from monitor_app.db import registry as registry_module
from monitor_app.db.bulk import sequence_fix_sql, sequence_lag
from monitor_app.db.engine import Database
from monitor_app.db.registry import TableRegistry


def _config() -> MonitorConfig:
    return MonitorConfig(
        tables={
            "users": TableDef(columns={"id": "int", "name": "str"}, primary_key="id"),
            "codes": TableDef(columns={"code": "str"}, primary_key="code"),
        }
    )


def _fake_pg() -> SimpleNamespace:
    @contextmanager
    def connect():
        yield SimpleNamespace(dialect=postgresql.dialect())

    return SimpleNamespace(
        dialect="postgresql",
        engine=SimpleNamespace(dialect=postgresql.dialect()),
        connect=connect,
    )


def test_other_databases_are_left_alone(tmp_path):
    db = Database(f"sqlite:///{(tmp_path / 'a.db').as_posix()}")
    registry = TableRegistry(_config())
    try:
        registry.create_all(db)
        assert registry.repair_sequences(db) == {}
        with db.connect() as conn:
            assert sequence_lag(conn, registry.get("users")) is None
    finally:
        db.dispose()


def test_only_integer_keys_are_checked_once_per_registry(monkeypatch):
    calls = []

    def fake_repair(conn, table, pk):
        calls.append((table.name, pk))
        return (0, 3) if table.name == "users" else None

    monkeypatch.setattr(registry_module, "repair_sequence", fake_repair)
    registry = TableRegistry(_config())
    db = _fake_pg()
    monkeypatch.setattr(registry.metadata, "create_all", lambda engine: None)
    registry.create_all(db)
    registry.create_all(db)  # 2 回目以降は確かめない(取り込みのたびに呼ばれる)
    assert calls == [("users", "id")]
    assert registry.repair_sequences(db) == {"users": (0, 3)}


def test_a_failed_repair_warns_with_the_fix_and_does_not_stop(monkeypatch, caplog):
    def denied(conn, table, pk):
        raise OperationalError("SELECT setval", {}, Exception("permission denied"))

    monkeypatch.setattr(registry_module, "repair_sequence", denied)
    monkeypatch.setattr(
        logging.getLogger("monitor_app"), "propagate", True, raising=False
    )
    registry = TableRegistry(_config())
    with caplog.at_level(logging.WARNING, logger="monitor_app.db"):
        assert registry.repair_sequences(_fake_pg()) == {}
    [record] = [r for r in caplog.records if "連番" in r.message]
    assert "permission denied" in record.message
    assert "SELECT setval(pg_get_serial_sequence('users', 'id')" in record.message


def test_fix_sql_quotes_names():
    config = MonitorConfig(
        tables={"Line's": TableDef(columns={"No": "int"}, primary_key="No")}
    )
    table = TableRegistry(config).get("Line's")
    sql = sequence_fix_sql(postgresql.dialect(), table, "No")
    assert sql == (
        "SELECT setval(pg_get_serial_sequence('\"Line''s\"', 'No'), "
        '(SELECT MAX("No") FROM "Line\'s"))'
    )
