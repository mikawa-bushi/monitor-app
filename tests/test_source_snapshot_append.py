"""ファイル全体を毎回読むソースの追記(D3-08)と、カーソルの競合(D4-12)。

- ``kind="json"``(既定の追記)は変わるたびに全レコードを入れ直していたため、2 回目
  から主キーの重複で失敗し続け、新しいレコードが入らなかった。主キーのあるレコードは
  既にある行を読み飛ばす。主キーの無いレコードは ``check`` が警告する。
- カーソル(どこまで読んだか)は書き込みのトランザクションの中で確かめる。cron の
  ``sync-sources`` とフォルダ監視が同じソースを同時に読んでも二重に取り込まない。
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing

import pytest
from sqlalchemy import text

from monitor_app import MonitorConfig, TableDef
from monitor_app.checks import run_checks
from monitor_app.db.engine import Database
from monitor_app.db.ingest_state import IngestStateStore
from monitor_app.db.registry import TableRegistry
from monitor_app.services.connectors import build_connectors
from monitor_app.settings.runtime import AppSettings


def _setup(tmp_path, sources: dict, columns=None):
    config = MonitorConfig(
        tables={"s": TableDef(columns=columns or {"id": "int", "v": "int"})},
        sources=sources,
    )
    db = Database(f"sqlite:///{(tmp_path / 'app.db').as_posix()}")
    registry = TableRegistry(config)
    registry.create_all(db)
    state = IngestStateStore(db)
    state.create()
    return config, db, registry, state


def _rows(db) -> list[tuple]:
    with db.connect() as conn:
        return [tuple(r) for r in conn.execute(text("SELECT id, v FROM s ORDER BY id"))]


def _rewrite(path, data) -> None:
    time.sleep(0.01)
    path.write_text(json.dumps(data))


def test_json_source_in_append_mode_picks_up_new_records(tmp_path):
    src = tmp_path / "status.json"
    src.write_text(json.dumps([{"id": 1, "v": 1}, {"id": 2, "v": 2}]))
    _cfg, db, registry, state = _setup(
        tmp_path, {"st": {"kind": "json", "path": str(src), "table": "s"}}
    )
    try:
        conn = build_connectors(_cfg, registry, db, state)["st"]
        assert conn.poll_once() == 2
        _rewrite(src, [{"id": 1, "v": 1}, {"id": 2, "v": 2}, {"id": 3, "v": 3}])
        assert conn.poll_once() == 1  # 新しいレコードだけ(数えるのも新しい分)
        _rewrite(src, [{"id": 3, "v": 3}, {"id": 4, "v": 4}])
        assert conn.poll_once() == 1
        assert _rows(db) == [(1, 1), (2, 2), (3, 3), (4, 4)]
    finally:
        db.dispose()


def test_sqlite_query_without_watermark_in_append_mode(tmp_path):
    source_db = tmp_path / "src.db"
    with closing(sqlite3.connect(source_db)) as c, c:
        c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v INTEGER)")
        c.execute("INSERT INTO t VALUES (1, 10), (2, 20)")
    _cfg, db, registry, state = _setup(
        tmp_path,
        {
            "q": {
                "kind": "sqlite",
                "path": str(source_db),
                "table": "s",
                "query": "SELECT id, v FROM t",
            }
        },
    )
    try:
        conn = build_connectors(_cfg, registry, db, state)["q"]
        assert conn.poll_once() == 2
        with closing(sqlite3.connect(source_db)) as c, c:
            c.execute("INSERT INTO t VALUES (3, 30)")
        assert conn.poll_once() == 1
        assert _rows(db) == [(1, 10), (2, 20), (3, 30)]
    finally:
        db.dispose()


def test_a_cursor_moved_by_another_process_skips_the_batch(tmp_path):
    """別のプロセス(cron の sync-sources)が先に同じ行を取り込んだら書き込まない。"""
    src = tmp_path / "log.jsonl"
    src.write_text('{"id": 1, "v": 1}\n')
    cfg, db, registry, state = _setup(
        tmp_path,
        {"j": {"kind": "jsonl", "path": str(src), "table": "s"}},
        columns={"_row_id": "int", "id": "int", "v": "int"},
    )
    try:
        watcher_side = build_connectors(cfg, registry, db, state)["j"]
        cron_side = build_connectors(cfg, registry, db, state)["j"]
        real_get = state.get
        calls = []

        def racing_get(source, **kwargs):
            # 監視側がカーソルを読んだ直後に、cron 側が同じ行を取り込み終える
            value = real_get(source, **kwargs)
            if not kwargs and not calls:
                calls.append(1)
                assert cron_side.poll_once() == 1
            return value

        state.get = racing_get
        assert watcher_side.poll_once() == 0
        with db.connect() as conn:
            assert conn.execute(text("SELECT COUNT(*) FROM s")).scalar() == 1
    finally:
        db.dispose()


def test_check_warns_about_snapshot_sources_without_a_primary_key(tmp_path):
    src = tmp_path / "status.json"
    src.write_text(json.dumps([{"name": "a", "v": 1}]))
    config = MonitorConfig(
        tables={"s": TableDef(columns={"id": "int", "name": "str", "v": "int"})},
        sources={
            "by_file": {"kind": "json", "path": str(src), "table": "s"},
            "by_mapping": {
                "kind": "json",
                "path": str(src),
                "table": "s",
                "mapping": {"name": "name", "v": "v"},
            },
            "replaced": {
                "kind": "json",
                "path": str(src),
                "table": "s",
                "mode": "replace",
            },
        },
    )
    settings = AppSettings(
        _env_file=None,
        csv_dir=str(tmp_path / "csv"),
        database_url=f"sqlite:///{tmp_path / 'x.db'}",
    )
    static = [i for i in run_checks(config, settings) if i.code == "source-reinsert"]
    assert [i.message.split("'")[1] for i in static] == ["by_mapping"]
    with_data = run_checks(config, settings, data=True)
    names = [i.message.split("'")[1] for i in with_data if i.code == "source-reinsert"]
    assert sorted(names) == ["by_file", "by_mapping"]
    assert all(i.level == "warning" for i in with_data if i.code == "source-reinsert")


@pytest.mark.parametrize("mode", ["append", "replace"])
def test_sync_counts_are_the_rows_actually_written(tmp_path, mode):
    src = tmp_path / "status.json"
    src.write_text(json.dumps([{"id": 1, "v": 1}]))
    _cfg, db, registry, state = _setup(
        tmp_path, {"st": {"kind": "json", "path": str(src), "table": "s", "mode": mode}}
    )
    try:
        conn = build_connectors(_cfg, registry, db, state)["st"]
        assert conn.poll_once() == 1
        _rewrite(src, [{"id": 1, "v": 5}, {"id": 2, "v": 2}])
        assert conn.poll_once() == (1 if mode == "append" else 2)
        expected = [(1, 1), (2, 2)] if mode == "append" else [(1, 5), (2, 2)]
        assert _rows(db) == expected
    finally:
        db.dispose()
