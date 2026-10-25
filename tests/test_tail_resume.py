"""追記監視の読み取り位置を DB に保存し、再起動後も続きから読む(D3-03)。

以前は起動のたびに CSV の末尾を読み取り位置にしたため、``runserver
--import-csv-if-empty``(Docker・systemd の既定の起動)と追記監視の組み合わせでは、
停止中に追記された行が失われていた。
"""

from __future__ import annotations

import json
import logging
import os

import pytest
from sqlalchemy import text

from monitor_app import MonitorConfig, TableDef
from monitor_app.db.engine import Database
from monitor_app.db.ingest_state import IngestStateStore
from monitor_app.db.registry import TableRegistry
from monitor_app.services.importer import CsvImporter
from monitor_app.services.ingest_watcher import IngestWatcher


@pytest.fixture(autouse=True)
def _propagate(monkeypatch):
    monkeypatch.setattr(logging.getLogger("monitor_app"), "propagate", True)


@pytest.fixture
def env(tmp_path):
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()
    config = MonitorConfig(tables={"m": TableDef(columns={"id": "int", "v": "int"})})
    db = Database(f"sqlite:///{(tmp_path / 'app.db').as_posix()}")
    registry = TableRegistry(config)
    yield config, registry, db, csv_dir
    db.dispose()


def _ids(db) -> list[int]:
    with db.connect() as conn:
        return [r[0] for r in conn.execute(text("SELECT id FROM m ORDER BY id"))]


def _append(path, data: str) -> None:
    with path.open("a") as fh:
        fh.write(data)
    st = path.stat()
    later = st.st_mtime_ns + 2_000_000_000  # mtime の分解能の粗いファイルシステム対策
    os.utime(path, ns=(later, later))


def _start_server(env, *, if_empty: bool = True) -> IngestWatcher:
    """``runserver --import-csv-if-empty`` + ``MONITOR_INGEST_MODE=append`` の起動。"""
    config, registry, db, csv_dir = env
    importer = CsvImporter(config, registry, db, csv_dir)
    if if_empty:
        importer.import_all(mode="if-empty")
    watcher = IngestWatcher(importer, 1.0, keep=True)
    watcher.setup()
    return watcher


def test_rows_appended_while_stopped_are_imported_after_restart(env):
    path = env[3] / "m.csv"
    path.write_text("id,v\n1,1\n2,2\n")
    first = _start_server(env)
    _append(path, "3,3\n")
    first.poll_once()
    # 停止中もロガーは追記を続ける
    _append(path, "4,4\n5,5\n")
    second = _start_server(env)
    assert _ids(env[2]) == [1, 2, 3, 4, 5]  # 起動時に続きを取り込む
    _append(path, "6,6\n")
    second.poll_once()
    assert _ids(env[2]) == [1, 2, 3, 4, 5, 6]


def test_the_saved_position_is_written_with_the_rows(env):
    path = env[3] / "m.csv"
    path.write_text("id,v\n1,1\n")
    watcher = _start_server(env)
    _append(path, "2,2\n")
    watcher.poll_once()
    raw = IngestStateStore(env[2]).get("csv:m")
    saved = json.loads(raw)
    assert saved["o"] == path.stat().st_size and saved["r"] == 2
    assert len(raw) < 255  # _ingest_state.cursor は VARCHAR(255)


def test_a_file_replaced_while_stopped_is_read_from_the_start(env, caplog):
    path = env[3] / "m.csv"
    path.write_text("id,v\n1,1\n2,2\n")
    first = _start_server(env)
    _append(path, "3,3\n")
    first.poll_once()
    # 停止中にローテーション(新しいファイル)
    path.write_text("id,v\n10,10\n11,11\n")
    _start_server(env)
    assert _ids(env[2]) == [1, 2, 3, 10, 11]
    assert any("前回の停止の後にファイルが" in m for m in caplog.messages)


def test_a_full_import_forgets_the_saved_position(env):
    """``import-csv`` などで行を入れたテーブルは、保存した位置を消す(古い位置から
    読み直して、取り込んだばかりの行を二重に入れない)。"""
    config, registry, db, csv_dir = env
    path = csv_dir / "m.csv"
    path.write_text("id,v\n1,1\n")
    watcher = _start_server(env)
    _append(path, "2,2\n")
    watcher.poll_once()
    _append(path, "3,3\n4,4\n")
    CsvImporter(config, registry, db, csv_dir).import_all(keep=True)
    assert IngestStateStore(db).get("csv:m") is None
    _start_server(env)
    assert _ids(db) == [1, 2, 3, 4]


def test_import_existing_resumes_from_the_saved_position(env):
    """``serve --append`` を同じ DB で起動し直しても、停止中の追記を取り込む。"""
    config, registry, db, csv_dir = env
    path = csv_dir / "m.csv"
    path.write_text("id,v\n1,1\n")

    def serve_append() -> IngestWatcher:
        importer = CsvImporter(config, registry, db, csv_dir)
        watcher = IngestWatcher(importer, 1.0, keep=True)
        watcher.import_existing()
        watcher.setup()
        return watcher

    serve_append()
    _append(path, "2,2\n")
    serve_append()
    assert _ids(db) == [1, 2]


def test_a_broken_saved_position_is_ignored(env):
    config, registry, db, csv_dir = env
    path = csv_dir / "m.csv"
    path.write_text("id,v\n1,1\n")
    registry.create_all(db)
    store = IngestStateStore(db)
    store.create()
    store.set("csv:m", '{"o": "x"}')
    importer = CsvImporter(config, registry, db, csv_dir)
    importer.import_all()
    store.set("csv:m", '{"o": "x"}')
    state, resumed = importer.start_tail("m", path)
    assert not resumed and state.offset == path.stat().st_size
    assert json.loads(store.get("csv:m"))["o"] == state.offset


def test_without_a_saved_position_the_watcher_starts_at_the_end_as_before(env):
    """保存した位置が無い(2.4 で初めて起動した・状態を消した)ときは従来どおり、
    起動時の CSV の末尾から読む(その時点の行は取り込み済みとみなす)。"""
    _config, _registry, db, csv_dir = env
    path = csv_dir / "m.csv"
    path.write_text("id,v\n1,1\n")
    _start_server(env)
    IngestStateStore(db).delete("csv:m")
    _append(path, "2,2\n")
    watcher = _start_server(env)
    assert _ids(db) == [1]
    assert json.loads(IngestStateStore(db).get("csv:m"))["r"] == 2
    _append(path, "3,3\n")
    watcher.poll_once()
    assert _ids(db) == [1, 3]
