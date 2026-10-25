"""取り込みで行が変わったときの通知(``on_change``)と、ビューのキャッシュとの配線。

PostgreSQL / MySQL では DB の変更を検知できないため、取り込み(CSV・追記監視・
ソース)が行を書き込んだらビューの共有キャッシュを捨て、新しいデータをすぐ表示する。
"""

from __future__ import annotations

import json
import logging
import os

import pytest
from fastapi.testclient import TestClient

from monitor_app import MonitorConfig, SourceDef, TableDef, ViewDef
from monitor_app.container import build_services
from monitor_app.db.engine import Database
from monitor_app.db.registry import TableRegistry
from monitor_app.main import create_app
from monitor_app.services.importer import CsvImporter, TailState
from monitor_app.services.ingest_watcher import IngestWatcher
from monitor_app.settings.runtime import AppSettings


@pytest.fixture(autouse=True)
def _propagate(monkeypatch):
    monkeypatch.setattr(logging.getLogger("monitor_app"), "propagate", True)


@pytest.fixture
def env(tmp_path):
    config = MonitorConfig(tables={"t": TableDef(columns={"id": "int", "v": "str"})})
    db = Database(f"sqlite:///{(tmp_path / 'app.db').as_posix()}")
    registry = TableRegistry(config)
    registry.create_all(db)
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()
    yield config, registry, db, csv_dir
    db.dispose()


def test_importer_notifies_after_committed_changes_only(env, monkeypatch):
    config, registry, db, csv_dir = env
    calls = []
    importer = CsvImporter(
        config, registry, db, csv_dir, on_change=lambda: calls.append(1)
    )
    path = csv_dir / "t.csv"
    path.write_text("id,v\n1,a\n", encoding="utf-8")
    importer.import_all()
    assert calls == [1]
    importer.import_all(keep=True)  # すべて既存の主キー → 変化なし
    assert calls == [1]
    path.write_text("id,v\n", encoding="utf-8")
    importer.import_all()  # 有効行 0 件 → 置換しない
    assert calls == [1]
    path.write_text("id\n1\n", encoding="utf-8")
    importer.import_all()  # 列 v が無い → 置換を拒否
    assert calls == [1]
    path.write_text("id,v\n1,a\n2,b\n", encoding="utf-8")
    import monitor_app.services.importer as importer_mod

    def boom(*a, **k):
        raise RuntimeError("失敗")

    monkeypatch.setattr(importer_mod, "insert_in_chunks", boom)
    with pytest.raises(RuntimeError):
        importer.import_all()  # 取り消された書き込みでは呼ばない
    assert calls == [1]
    monkeypatch.undo()
    state = TailState()
    importer.tail_append("t", path, state)  # 1 は既存 → 2 だけ入る
    assert calls == [1, 1]


def test_a_failing_callback_does_not_fail_the_import(env, caplog):
    config, registry, db, csv_dir = env

    def broken():
        raise RuntimeError("キャッシュが壊れた")

    importer = CsvImporter(config, registry, db, csv_dir, on_change=broken)
    (csv_dir / "t.csv").write_text("id,v\n1,a\n", encoding="utf-8")
    assert importer.import_all().total_inserted == 1
    assert "取り込み後の通知(on_change)に失敗しました" in caplog.messages


def test_watcher_notifies_once_per_poll_with_changes(env):
    config, registry, db, csv_dir = env
    path = csv_dir / "t.csv"
    path.write_text("id,v\n1,a\n", encoding="utf-8")
    calls = []
    watcher = IngestWatcher(
        CsvImporter(config, registry, db, csv_dir),
        interval=1.0,
        keep=True,
        on_change=lambda: calls.append(1),
    )
    watcher.poll_once()
    assert calls == [1]
    watcher.poll_once()  # 変化なし
    assert calls == [1]


def _wire_config(tmp_path) -> MonitorConfig:
    jsonl = tmp_path / "events.jsonl"
    jsonl.write_text("", encoding="utf-8")
    return MonitorConfig(
        tables={
            "t": TableDef(columns={"id": "int", "v": "str"}),
            "e": TableDef(columns={"id": "int", "v": "str"}),
        },
        views={"tv": ViewDef(query="SELECT id, v FROM t ORDER BY id")},
        sources={"ev": SourceDef(kind="jsonl", path=str(jsonl), table="e")},
    )


def test_build_services_wires_importer_and_connectors_to_the_view_cache(tmp_path):
    settings = AppSettings(
        _env_file=None,
        database_url=f"sqlite:///{(tmp_path / 'app.db').as_posix()}",
        csv_dir=tmp_path / "csv",
        ingest_watch=True,
        csv_encoding_fallback="euc-jp,cp932",
    )
    services = build_services(_wire_config(tmp_path), settings)
    try:
        (watcher,) = services.background_tasks
        assert watcher.importer.on_change == services.view_cache.clear
        assert watcher.importer.encoding == "auto"
        assert watcher.importer.encoding_fallback == ["euc-jp", "cp932"]
        assert services.connectors["ev"].on_change == services.view_cache.clear
    finally:
        services.close()


def test_new_rows_are_visible_immediately_despite_a_long_cache_ttl(tmp_path):
    """キャッシュの TTL が長くても、取り込み後の GET は新しい行を返す。"""
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()
    (csv_dir / "t.csv").write_text("id,v\n1,a\n", encoding="utf-8")
    settings = AppSettings(
        _env_file=None,
        database_url=f"sqlite:///{(tmp_path / 'app.db').as_posix()}",
        csv_dir=csv_dir,
        ingest_watch=True,
        ingest_mode="append",
        view_cache_ttl_ms=600_000,
    )
    config = _wire_config(tmp_path)
    app = create_app(config, settings)
    client = TestClient(app)
    services = app.state.services
    (watcher,) = services.background_tasks
    watcher.setup()  # 既存の 1 行は取り込み済みとみなす
    assert client.get("/api/views/tv").json()["data"] == []  # キャッシュに載る
    with (csv_dir / "t.csv").open("a", encoding="utf-8") as fh:
        fh.write("2,b\n")
    later = (csv_dir / "t.csv").stat().st_mtime_ns + 2_000_000_000
    os.utime(csv_dir / "t.csv", ns=(later, later))
    watcher.poll_once()
    assert client.get("/api/views/tv").json()["data"] == [{"id": 2, "v": "b"}]

    jsonl = tmp_path / "events.jsonl"
    jsonl.write_text(json.dumps({"id": 1, "v": "x"}) + "\n", encoding="utf-8")
    assert services.connectors["ev"].poll_once() == 1
    services.close()
