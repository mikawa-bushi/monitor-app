"""フォルダ監視の取り込み失敗の警告が、いつ取り込み直すかを自然に伝える。

1 回目の失敗は次のポーリングで取り込み直すため、以前は「0 秒後以降に」と出ていた。
"""

from __future__ import annotations

import logging
import time

import pytest

from monitor_app import MonitorConfig, TableDef
from monitor_app.db.engine import Database
from monitor_app.db.registry import TableRegistry
from monitor_app.services.importer import CsvImporter
from monitor_app.services.ingest_watcher import IngestWatcher, _Failure


@pytest.fixture
def watcher(tmp_path, monkeypatch):
    monkeypatch.setattr(logging.getLogger("monitor_app"), "propagate", True)
    config = MonitorConfig(tables={"items": TableDef(columns={"id": "int"})})
    db = Database("sqlite://")
    registry = TableRegistry(config)
    registry.create_all(db)
    yield IngestWatcher(CsvImporter(config, registry, db, tmp_path), 0.5, keep=False)
    db.dispose()


@pytest.mark.parametrize(
    "wait,expected",
    [
        (0.0, "1 回目。次の確認でもう一度取り込みます"),
        (8.2, "3 回目。8 秒後以降にもう一度"),
    ],
)
def test_the_retry_time_is_worded_naturally(watcher, caplog, wait, expected):
    count = 1 if wait == 0 else 3
    failure = _Failure(None, count, time.monotonic() + wait, OSError("locked"))
    with caplog.at_level(logging.WARNING, logger="monitor_app"):
        watcher._log_failure("items", failure)
    message = caplog.records[-1].getMessage()
    assert expected in message and "0 秒後" not in message
