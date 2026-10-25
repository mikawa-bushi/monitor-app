"""保持期間(``TableDef.retention``)による古い行の定期削除(CRIT-02)。"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from monitor_app import MonitorConfig, Retention, TableDef, ViewDef
from monitor_app.container import build_services
from monitor_app.db.bulk import insert_rows
from monitor_app.db.engine import Database
from monitor_app.db.registry import TableRegistry
from monitor_app.main import create_app
from monitor_app.services.retention import RetentionTask, build_retention_task
from monitor_app.settings.runtime import AppSettings


@pytest.fixture(autouse=True)
def _propagate_monitor_logs(monkeypatch):
    """CLI のテストが configure_logging で monitor_app を propagate=False にしていても、
    caplog(root のハンドラ)で monitor_app.* のログを拾えるようにする。"""
    monkeypatch.setattr(logging.getLogger("monitor_app"), "propagate", True)


NOW = datetime(2026, 10, 21, 12, 0, 0)


def _env(tables: dict) -> tuple[MonitorConfig, TableRegistry, Database]:
    config = MonitorConfig(tables=tables)
    db = Database("sqlite://")
    registry = TableRegistry(config)
    registry.create_all(db)
    return config, registry, db


def _fill(db, registry, table: str, rows: list[dict]) -> None:
    with db.connect() as conn:
        insert_rows(conn, registry.get(table), rows)


def _ids(db, table: str) -> list[int]:
    with db.connect() as conn:
        return conn.execute(text(f"SELECT id FROM {table} ORDER BY id")).scalars().all()


def _task(config, registry, db, **kwargs) -> RetentionTask:
    return RetentionTask(config, registry, db, 600, clock=lambda: NOW, **kwargs)


def test_keep_days_on_a_datetime_column():
    config, registry, db = _env(
        {
            "m": TableDef(
                columns={"id": "int", "ts": "datetime", "v": "float"},
                retention=Retention(keep_days=2, column="ts"),
            )
        }
    )
    hours = [1, 47, 48, 49, 100]
    _fill(
        db,
        registry,
        "m",
        [{"id": h, "ts": NOW - timedelta(hours=h)} for h in hours] + [{"id": 999}],
    )
    assert _task(config, registry, db).purge_once() == {"m": 2}
    assert _ids(db, "m") == [1, 47, 48, 999]  # ちょうど 48 時間前と NULL は残す


def test_keep_days_on_a_date_column():
    config, registry, db = _env(
        {
            "d": TableDef(
                columns={"id": "int", "day": "date"},
                retention=Retention(keep_days=1, column="day"),
            )
        }
    )
    days = {1: date(2026, 10, 21), 2: date(2026, 10, 20), 3: date(2026, 10, 19)}
    _fill(db, registry, "d", [{"id": i, "day": d} for i, d in days.items()])
    # 現在 − 1 日 = 10/20 12:00。10/20 は丸ごとは古くないので残す
    assert _task(config, registry, db).purge_once() == {"d": 1}
    assert _ids(db, "d") == [1, 2]


@pytest.mark.parametrize(
    "fmt",
    ["%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"],
)
def test_keep_days_on_iso_text_columns(fmt):
    config, registry, db = _env(
        {
            "s": TableDef(
                columns={"id": "int", "ts": "str"},
                retention=Retention(keep_days=1, column="ts"),
            )
        }
    )
    hours = [1, 23, 25, 30]
    _fill(
        db,
        registry,
        "s",
        [{"id": h, "ts": (NOW - timedelta(hours=h)).strftime(fmt)} for h in hours],
    )
    assert _task(config, registry, db).purge_once() == {"s": 2}
    assert _ids(db, "s") == [1, 23]


def test_keep_days_on_iso_date_text():
    config, registry, db = _env(
        {
            "s": TableDef(
                columns={"id": "int", "day": "str"},
                retention=Retention(keep_days=3, column="day"),
            )
        }
    )
    rows = [{"id": i, "day": f"2026-10-{d:02d}"} for i, d in enumerate([21, 18, 17], 1)]
    _fill(db, registry, "s", rows)
    assert _task(config, registry, db).purge_once() == {"s": 1}
    assert _ids(db, "s") == [1, 2]


def test_non_iso_text_is_skipped_with_one_warning(caplog):
    config, registry, db = _env(
        {
            "s": TableDef(
                columns={"id": "int", "ts": "str"},
                retention=Retention(keep_days=1, column="ts"),
            )
        }
    )
    _fill(db, registry, "s", [{"id": 1, "ts": "2026/10/01 8:00"}])
    task = _task(config, registry, db)
    with caplog.at_level(logging.WARNING, logger="monitor_app.retention"):
        assert task.purge_once() == {"s": 0}
        assert task.purge_once() == {"s": 0}
    warnings = [r for r in caplog.records if "ISO 形式" in r.message]
    assert len(warnings) == 1
    assert "datetime" in warnings[0].message
    assert _ids(db, "s") == [1]


def test_keep_days_on_a_numeric_column_is_skipped(caplog):
    config, registry, db = _env(
        {
            "n": TableDef(
                columns={"id": "int", "epoch": "int"},
                retention=Retention(keep_days=1, column="epoch"),
            )
        }
    )
    _fill(db, registry, "n", [{"id": 1, "epoch": 0}])
    with caplog.at_level(logging.WARNING, logger="monitor_app.retention"):
        assert _task(config, registry, db).purge_once() == {"n": 0}
    assert "日付・日時・文字列の列にだけ" in caplog.text


def test_keep_days_on_an_empty_text_column_does_nothing():
    config, registry, db = _env(
        {
            "s": TableDef(
                columns={"id": "int", "ts": "str"},
                retention=Retention(keep_days=1, column="ts"),
            )
        }
    )
    assert _task(config, registry, db).purge_once() == {"s": 0}


def test_keep_rows_by_primary_key():
    config, registry, db = _env(
        {"k": TableDef(columns={"id": "int"}, retention=Retention(keep_rows=3))}
    )
    _fill(db, registry, "k", [{"id": i} for i in range(1, 11)])
    assert _task(config, registry, db).purge_once() == {"k": 7}
    assert _ids(db, "k") == [8, 9, 10]
    assert _task(config, registry, db).purge_once() == {"k": 0}  # 2 回目は何もしない


def test_keep_rows_by_column_keeps_ties_and_nulls():
    config, registry, db = _env(
        {
            "k": TableDef(
                columns={"id": "int", "seq": "int"},
                retention=Retention(keep_rows=2, column="seq"),
            )
        }
    )
    rows = [
        {"id": 1, "seq": 10},
        {"id": 2, "seq": 20},
        {"id": 3, "seq": 20},  # 2 番目に新しい値が同点 → どちらも残す
        {"id": 4, "seq": 30},
        {"id": 5, "seq": None},  # NULL は消さない
    ]
    _fill(db, registry, "k", rows)
    assert _task(config, registry, db).purge_once() == {"k": 1}
    assert _ids(db, "k") == [2, 3, 4, 5]


def test_fewer_rows_than_keep_rows_deletes_nothing():
    config, registry, db = _env(
        {"k": TableDef(columns={"id": "int"}, retention=Retention(keep_rows=50))}
    )
    _fill(db, registry, "k", [{"id": i} for i in range(1, 6)])
    assert _task(config, registry, db).purge_once() == {"k": 0}


def test_both_rules_apply():
    config, registry, db = _env(
        {
            "b": TableDef(
                columns={"id": "int", "ts": "datetime"},
                retention=Retention(keep_days=1, keep_rows=2, column="ts"),
            )
        }
    )
    rows = [{"id": h, "ts": NOW - timedelta(hours=h)} for h in (1, 2, 3, 30)]
    _fill(db, registry, "b", rows)
    # keep_days で 30 時間前の行、keep_rows(ts の新しい順に 2 行)で 3 時間前の行
    assert _task(config, registry, db).purge_once() == {"b": 2}
    assert _ids(db, "b") == [1, 2]


def test_deletes_are_batched(monkeypatch):
    config, registry, db = _env(
        {"k": TableDef(columns={"id": "int"}, retention=Retention(keep_rows=1))}
    )
    _fill(db, registry, "k", [{"id": i} for i in range(1, 24)])
    task = _task(config, registry, db, batch_size=5)
    transactions: list[int] = []
    real_connect = db.connect

    def counting_connect():
        transactions.append(1)
        return real_connect()

    monkeypatch.setattr(db, "connect", counting_connect)
    assert task.purge_once() == {"k": 22}
    assert len(transactions) == 5  # 5+5+5+5+2 行(1 回 1 トランザクション)
    assert _ids(db, "k") == [23]


def test_tables_without_a_single_primary_key_are_deleted_at_once():
    from sqlalchemy import Column, Integer, MetaData, Table

    config, registry, db = _env(
        {"k": TableDef(columns={"id": "int"}, retention=Retention(keep_rows=1))}
    )
    table = Table("nopk", MetaData(), Column("n", Integer))
    task = _task(config, registry, db, batch_size=2)
    table.create(db.engine)
    with db.connect() as conn:
        conn.execute(table.insert(), [{"n": i} for i in range(10)])
    assert task._delete_where(table, table.c.n < 7) == 7


def test_on_change_is_called_only_when_rows_were_deleted():
    config, registry, db = _env(
        {"k": TableDef(columns={"id": "int"}, retention=Retention(keep_rows=1))}
    )
    calls: list[int] = []
    task = _task(config, registry, db, on_change=lambda: calls.append(1))
    task.purge_once()
    assert calls == []
    _fill(db, registry, "k", [{"id": 1}, {"id": 2}])
    task.purge_once()
    assert calls == [1]


def test_one_failing_table_does_not_stop_the_others(monkeypatch, caplog):
    config, registry, db = _env(
        {
            "a": TableDef(columns={"id": "int"}, retention=Retention(keep_rows=1)),
            "b": TableDef(columns={"id": "int"}, retention=Retention(keep_rows=1)),
        }
    )
    _fill(db, registry, "b", [{"id": 1}, {"id": 2}])
    with db.connect() as conn:
        conn.execute(text("DROP TABLE a"))  # 表が消えている(ロック中などの代わり)
    with caplog.at_level(logging.WARNING, logger="monitor_app.retention"):
        assert _task(config, registry, db).purge_once() == {"b": 1}
    assert "保持期間の適用に失敗しました 'a'" in caplog.text


def test_tick_purges_and_logs(caplog):
    config, registry, db = _env(
        {"k": TableDef(columns={"id": "int"}, retention=Retention(keep_rows=1))}
    )
    _fill(db, registry, "k", [{"id": 1}, {"id": 2}])
    with caplog.at_level(logging.INFO, logger="monitor_app.retention"):
        _task(config, registry, db).tick()
    assert "k から 1 行を削除しました" in caplog.text


def _settings(minutes: float = 10.0):
    return SimpleNamespace(retention_interval_minutes=minutes)


def test_build_retention_task_conditions():
    with_rule = {"k": TableDef(columns={"id": "int"}, retention=Retention(keep_rows=1))}
    config, registry, db = _env(with_rule)
    task = build_retention_task(config, registry, db, _settings(2))
    assert isinstance(task, RetentionTask) and task.interval == 120
    assert build_retention_task(config, registry, db, _settings(0)) is None
    plain, plain_registry, plain_db = _env({"t": TableDef(columns={"id": "int"})})
    assert build_retention_task(plain, plain_registry, plain_db, _settings()) is None


def test_build_services_wires_retention_to_the_view_cache(tmp_path):
    config = MonitorConfig(
        tables={"k": TableDef(columns={"id": "int"}, retention=Retention(keep_rows=2))},
        views={"v": ViewDef(query="SELECT id FROM k ORDER BY id")},
    )
    settings = AppSettings(
        _env_file=None, database_url=f"sqlite:///{(tmp_path / 'a.db').as_posix()}"
    )
    services = build_services(config, settings)
    try:
        (task,) = services.background_tasks
        assert isinstance(task, RetentionTask)
        assert task.interval == 600  # 既定 10 分
        assert task.on_change == services.view_cache.clear
    finally:
        services.close()

    app = create_app(config, settings)
    client = TestClient(app)
    client.post("/api/ingest/k", json=[{"id": i} for i in range(1, 6)])
    assert len(client.get("/api/views/v").json()["data"]) == 5  # キャッシュに載る
    retention = next(
        t for t in app.state.services.background_tasks if isinstance(t, RetentionTask)
    )
    retention.purge_once()
    assert [r["id"] for r in client.get("/api/views/v").json()["data"]] == [4, 5]
