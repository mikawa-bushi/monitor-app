"""文字列の列の保持期間(keep_days)は、行ごとの値の形で比べる(D3-10)。

以前は 1 行だけ見た区切り(T / 空白)で全行を比べ、' ' < 'T' のため区切りの違う
新しい行まで消していた。空欄('')の行も消し、先頭の行が空欄だと保持期間がまったく
効かなかった。
"""

from __future__ import annotations

import logging
from datetime import datetime

import pytest
from sqlalchemy import insert, select

from monitor_app import MonitorConfig, Retention, TableDef
from monitor_app.db.engine import Database
from monitor_app.db.registry import TableRegistry
from monitor_app.services.retention import RetentionTask

NOW = datetime(2026, 10, 21, 12, 0, 0)  # keep_days=7 → 10/14 12:00 より古い行を消す


@pytest.fixture(autouse=True)
def _propagate_monitor_logs(monkeypatch):
    monkeypatch.setattr(logging.getLogger("monitor_app"), "propagate", True)


def _run(rows: list[dict]) -> tuple[list[int], dict]:
    config = MonitorConfig(
        tables={
            "t": TableDef(
                columns={"id": "int", "ts": "str"},
                retention=Retention(keep_days=7, column="ts"),
            )
        }
    )
    db = Database("sqlite://")
    registry = TableRegistry(config)
    registry.create_all(db)
    table = registry.get("t")
    with db.connect() as conn:
        conn.execute(insert(table), rows)
    deleted = RetentionTask(config, registry, db, 60, clock=lambda: NOW).purge_once()
    with db.readonly() as conn:
        ids = conn.execute(select(table.c.id).order_by(table.c.id)).scalars().all()
    db.dispose()
    return ids, deleted


def test_mixed_separators_never_delete_recent_rows():
    ids, deleted = _run(
        [
            {"id": 1, "ts": "2026-10-01T00:00:00"},  # 古い(T)
            {"id": 2, "ts": "2026-10-14 18:00:00"},  # 6.75 日前(空白)→ 残す
            {"id": 3, "ts": "2026-10-14T06:00:00"},  # 7.25 日前(T)→ 消す
            {"id": 4, "ts": "2026-10-14 06:00:00.5"},  # 7.25 日前(空白・小数秒)
            {"id": 5, "ts": "2026-10-14"},  # 日付だけ: 10/14 は丸ごとは古くない
            {"id": 6, "ts": "2026-10-13"},
        ]
    )
    assert ids == [2, 5]
    assert deleted == {"t": 4}


def test_blank_and_non_iso_values_are_kept_and_do_not_disable_retention(caplog):
    with caplog.at_level(logging.WARNING, logger="monitor_app.retention"):
        ids, _deleted = _run(
            [
                {"id": 1, "ts": ""},  # 先頭が空欄でも保持期間は効く
                {"id": 2, "ts": "2026-10-01 08:00:00"},
                {"id": 3, "ts": None},
                {"id": 4, "ts": "   "},
                {"id": 5, "ts": "10/01/2026 08:00"},  # ISO 形式でない値は消さない
            ]
        )
    assert ids == [1, 3, 4, 5]
    assert not [r for r in caplog.records if "ISO 形式" in r.message]


def test_non_iso_first_value_warns_but_iso_rows_are_still_purged(caplog):
    with caplog.at_level(logging.WARNING, logger="monitor_app.retention"):
        ids, _deleted = _run(
            [
                {"id": 1, "ts": "2026/10/01 8:00"},
                {"id": 2, "ts": "2026-10-01 08:00:00"},
                {"id": 3, "ts": "2026-10-20 08:00:00"},
            ]
        )
    assert ids == [1, 3]
    [warning] = [r for r in caplog.records if "ISO 形式" in r.message]
    assert "datetime" in warning.message
    assert "import-csv" in warning.message  # 既存の値をそろえる方法も示す
