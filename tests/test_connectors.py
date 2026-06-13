"""コネクタ(sqlite / jsonl / json)の取り込みテスト。"""

from __future__ import annotations

import json
import sqlite3
import time

import pytest

from monitor_app.db.engine import Database
from monitor_app.db.ingest_state import IngestStateStore
from monitor_app.db.registry import TableRegistry
from monitor_app.services.connectors import (
    JsonlSourceConnector,
    JsonSourceConnector,
    SqliteSourceConnector,
    build_connectors,
)
from monitor_app.settings.declarative import MonitorConfig, SourceDef, TableDef


# ---------------------------------------------------------------------------
# ヘルパ: MonitorConfig + TableRegistry + DB を組み立てる
# ---------------------------------------------------------------------------


def make_config(sources: dict | None = None, extra_tables: dict | None = None):
    """テスト用 MonitorConfig を生成する。"""
    tables = {
        "events": TableDef(
            columns={"id": "int", "name": "str", "value": "float", "ts": "str"},
            primary_key="id",
        ),
    }
    if extra_tables:
        tables.update(extra_tables)

    return MonitorConfig(
        tables=tables,
        sources=sources or {},
    )


def make_env(tmp_path, config=None):
    """DB / Registry / IngestStateStore を生成して返す。"""
    db_path = tmp_path / "app.db"
    db = Database(f"sqlite:///{db_path}")
    if config is None:
        config = make_config()
    registry = TableRegistry(config)
    registry.create_all(db)
    state = IngestStateStore(db)
    state.create()
    return db, registry, state


# ---------------------------------------------------------------------------
# sqlite + watermark
# ---------------------------------------------------------------------------


def _make_src_db(tmp_path, rows):
    """テスト用ソース sqlite を作成して path を返す。"""
    src_path = tmp_path / "src.db"
    conn = sqlite3.connect(str(src_path))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS events (id INTEGER, name TEXT, value REAL, ts TEXT)"
    )
    conn.executemany("INSERT INTO events VALUES (?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()
    return src_path


def test_sqlite_watermark_incremental(tmp_path):
    """watermark あり: 初回全件 → 行追加 → poll で増分のみ → 変化なしで 0。"""
    rows_init = [(1, "a", 1.0, "2024-01-01"), (2, "b", 2.0, "2024-01-02")]
    src_path = _make_src_db(tmp_path, rows_init)

    sdef = SourceDef(
        kind="sqlite",
        path=str(src_path),
        table="events",
        source_table="events",
        watermark_column="ts",
        mode="append",
    )
    config = make_config(sources={"src": sdef})
    db, registry, state = make_env(tmp_path, config)

    connector = SqliteSourceConnector("src", sdef, config, registry, db, state)

    # 初回: 全件
    n = connector.poll_once()
    assert n == 2

    # 変化なし: 0
    n = connector.poll_once()
    assert n == 0

    # 行追加
    conn = sqlite3.connect(str(src_path))
    conn.execute("INSERT INTO events VALUES (3, 'c', 3.0, '2024-01-03')")
    conn.commit()
    conn.close()

    # 増分のみ
    n = connector.poll_once()
    assert n == 1

    # 変化なし: 0
    n = connector.poll_once()
    assert n == 0

    # DB 内の行数確認
    with db.connect() as conn:
        rows = conn.execute(registry.get("events").select()).fetchall()
    assert len(rows) == 3


def test_sqlite_no_watermark_replace(tmp_path):
    """watermark なし + replace: ファイル更新で洗い替え。"""
    rows_v1 = [(1, "a", 1.0, "2024-01-01")]
    src_path = _make_src_db(tmp_path, rows_v1)

    sdef = SourceDef(
        kind="sqlite",
        path=str(src_path),
        table="events",
        source_table="events",
        mode="replace",
    )
    config = make_config(sources={"src": sdef})
    db, registry, state = make_env(tmp_path, config)
    connector = SqliteSourceConnector("src", sdef, config, registry, db, state)

    n = connector.poll_once()
    assert n == 1

    # 変化なし → 0
    n = connector.poll_once()
    assert n == 0

    # ファイル更新(mtime 変化を確実にするため少し待機)
    time.sleep(0.01)
    conn = sqlite3.connect(str(src_path))
    conn.execute("DELETE FROM events")
    conn.execute("INSERT INTO events VALUES (10, 'x', 9.0, '2025-01-01')")
    conn.execute("INSERT INTO events VALUES (11, 'y', 8.0, '2025-01-02')")
    conn.commit()
    conn.close()

    # ファイルの mtime/size を確実に更新するため stat をリセット
    import os

    os.utime(str(src_path), None)

    n = connector.poll_once()
    assert n == 2  # 洗い替えで 2 行

    with db.connect() as conn:
        rows = conn.execute(registry.get("events").select()).fetchall()
    assert len(rows) == 2


def test_sqlite_path_not_exist(tmp_path):
    """ファイル未存在 → 0(例外なし)。"""
    sdef = SourceDef(
        kind="sqlite",
        path=str(tmp_path / "not_exist.db"),
        table="events",
        source_table="events",
        mode="append",
    )
    config = make_config(sources={"src": sdef})
    db, registry, state = make_env(tmp_path, config)
    connector = SqliteSourceConnector("src", sdef, config, registry, db, state)
    assert connector.poll_once() == 0


# ---------------------------------------------------------------------------
# jsonl
# ---------------------------------------------------------------------------


def test_jsonl_incremental(tmp_path):
    """2 行 → poll(2) → 1 行追記 → poll(1) → 変化なし 0 → truncate 後の再読込。"""
    jsonl_path = tmp_path / "events.jsonl"
    line1 = json.dumps({"id": 1, "name": "a", "value": 1.0, "ts": "t1"}) + "\n"
    line2 = json.dumps({"id": 2, "name": "b", "value": 2.0, "ts": "t2"}) + "\n"
    jsonl_path.write_text(line1 + line2)

    sdef = SourceDef(
        kind="jsonl",
        path=str(jsonl_path),
        table="events",
        mode="append",
    )
    config = make_config(sources={"src": sdef})
    db, registry, state = make_env(tmp_path, config)
    connector = JsonlSourceConnector("src", sdef, config, registry, db, state)

    n = connector.poll_once()
    assert n == 2

    # 変化なし
    n = connector.poll_once()
    assert n == 0

    # 1 行追記
    line3 = json.dumps({"id": 3, "name": "c", "value": 3.0, "ts": "t3"}) + "\n"
    with jsonl_path.open("a") as fh:
        fh.write(line3)

    n = connector.poll_once()
    assert n == 1

    # 変化なし
    n = connector.poll_once()
    assert n == 0

    # truncate 後に新行
    line4 = json.dumps({"id": 4, "name": "d", "value": 4.0, "ts": "t4"}) + "\n"
    jsonl_path.write_text(line4)

    n = connector.poll_once()
    assert n == 1


def test_jsonl_incomplete_line(tmp_path):
    """末尾に改行のない未完了行は消費されず、改行追記後に取り込まれる。"""
    jsonl_path = tmp_path / "events.jsonl"
    complete = json.dumps({"id": 1, "name": "a", "value": 1.0, "ts": "t1"}) + "\n"
    incomplete = json.dumps(
        {"id": 2, "name": "b", "value": 2.0, "ts": "t2"}
    )  # 改行なし

    jsonl_path.write_text(complete + incomplete)

    sdef = SourceDef(
        kind="jsonl",
        path=str(jsonl_path),
        table="events",
        mode="append",
    )
    config = make_config(sources={"src": sdef})
    db, registry, state = make_env(tmp_path, config)
    connector = JsonlSourceConnector("src", sdef, config, registry, db, state)

    # 完了行のみ取り込まれる
    n = connector.poll_once()
    assert n == 1

    # 改行を追記して完了させる
    with jsonl_path.open("a") as fh:
        fh.write("\n")

    # 未完了だった行が取り込まれる
    n = connector.poll_once()
    assert n == 1


def test_jsonl_path_not_exist(tmp_path):
    """ファイル未存在 → 0(例外なし)。"""
    sdef = SourceDef(
        kind="jsonl",
        path=str(tmp_path / "not_exist.jsonl"),
        table="events",
        mode="append",
    )
    config = make_config(sources={"src": sdef})
    db, registry, state = make_env(tmp_path, config)
    connector = JsonlSourceConnector("src", sdef, config, registry, db, state)
    assert connector.poll_once() == 0


# ---------------------------------------------------------------------------
# json + record_path + replace
# ---------------------------------------------------------------------------


def test_json_record_path_replace(tmp_path):
    """record_path + replace: 初回取り込み → ファイル再生成で行が置き換わる。"""
    json_path = tmp_path / "findings.json"
    data_v1 = {
        "meta": {"version": 1},
        "results": [
            {"id": 1, "name": "a", "value": 1.0, "ts": "t1"},
            {"id": 2, "name": "b", "value": 2.0, "ts": "t2"},
        ],
    }
    json_path.write_text(json.dumps(data_v1))

    sdef = SourceDef(
        kind="json",
        path=str(json_path),
        table="events",
        record_path="results",
        mode="replace",
    )
    config = make_config(sources={"src": sdef})
    db, registry, state = make_env(tmp_path, config)
    connector = JsonSourceConnector("src", sdef, config, registry, db, state)

    n = connector.poll_once()
    assert n == 2

    # 変化なし → 0
    n = connector.poll_once()
    assert n == 0

    # ファイル再生成
    time.sleep(0.01)
    data_v2 = {
        "meta": {"version": 2},
        "results": [
            {"id": 10, "name": "x", "value": 9.0, "ts": "t9"},
        ],
    }
    json_path.write_text(json.dumps(data_v2))

    n = connector.poll_once()
    assert n == 1  # 洗い替えで 1 行

    with db.connect() as conn:
        rows = conn.execute(registry.get("events").select()).fetchall()
    assert len(rows) == 1


def test_json_path_not_exist(tmp_path):
    """ファイル未存在 → 0(例外なし)。"""
    sdef = SourceDef(
        kind="json",
        path=str(tmp_path / "not_exist.json"),
        table="events",
        mode="replace",
    )
    config = make_config(sources={"src": sdef})
    db, registry, state = make_env(tmp_path, config)
    connector = JsonSourceConnector("src", sdef, config, registry, db, state)
    assert connector.poll_once() == 0


# ---------------------------------------------------------------------------
# mapping: ドット記法 + list インデックス + list 値の平坦化
# ---------------------------------------------------------------------------


def test_jsonl_mapping_dot_notation(tmp_path):
    """mapping のドット記法(`detected_at.0` → list インデックス)と list 値の平坦化。"""
    jsonl_path = tmp_path / "events.jsonl"
    record = {
        "event_id": 1,
        "label": "foo",
        "score": 3.14,
        "detected_at": ["2024-01-01", "extra"],
    }
    jsonl_path.write_text(json.dumps(record) + "\n")

    # mapping: dest_col → src_key(ドット記法)
    sdef = SourceDef(
        kind="jsonl",
        path=str(jsonl_path),
        table="events",
        mode="append",
        mapping={
            "id": "event_id",
            "name": "label",
            "value": "score",
            "ts": "detected_at.0",
        },
    )
    config = make_config(sources={"src": sdef})
    db, registry, state = make_env(tmp_path, config)
    connector = JsonlSourceConnector("src", sdef, config, registry, db, state)

    n = connector.poll_once()
    assert n == 1

    with db.connect() as conn:
        row = conn.execute(registry.get("events").select()).fetchone()
    assert row is not None
    assert row[0] == 1  # id
    assert row[1] == "foo"  # name
    assert abs(row[2] - 3.14) < 0.001  # value


def test_jsonl_list_value_flatten(tmp_path):
    """list 値は空白区切りで平坦化される。"""
    jsonl_path = tmp_path / "events.jsonl"
    record = {"id": 1, "name": ["hello", "world"], "value": 1.0, "ts": "t1"}
    jsonl_path.write_text(json.dumps(record) + "\n")

    sdef = SourceDef(
        kind="jsonl",
        path=str(jsonl_path),
        table="events",
        mode="append",
    )
    config = make_config(sources={"src": sdef})
    db, registry, state = make_env(tmp_path, config)
    connector = JsonlSourceConnector("src", sdef, config, registry, db, state)

    n = connector.poll_once()
    assert n == 1

    with db.connect() as conn:
        row = conn.execute(registry.get("events").select()).fetchone()
    assert row[1] == "hello world"


# ---------------------------------------------------------------------------
# 型変換失敗行のスキップ
# ---------------------------------------------------------------------------


def test_coercion_failure_skips_row(tmp_path):
    """型変換失敗行はスキップされ、他の行は取り込まれる。"""
    jsonl_path = tmp_path / "events.jsonl"
    good = json.dumps({"id": 1, "name": "ok", "value": 1.0, "ts": "t1"}) + "\n"
    bad = (
        json.dumps({"id": "not_an_int", "name": "bad", "value": 2.0, "ts": "t2"}) + "\n"
    )
    another_good = (
        json.dumps({"id": 3, "name": "also_ok", "value": 3.0, "ts": "t3"}) + "\n"
    )
    jsonl_path.write_text(good + bad + another_good)

    sdef = SourceDef(
        kind="jsonl",
        path=str(jsonl_path),
        table="events",
        mode="append",
    )
    config = make_config(sources={"src": sdef})
    db, registry, state = make_env(tmp_path, config)
    connector = JsonlSourceConnector("src", sdef, config, registry, db, state)

    # id 列は int 型。"not_an_int" は変換失敗 → 2 行入り 1 行スキップ
    n = connector.poll_once()
    assert n == 2

    with db.connect() as conn:
        rows = conn.execute(registry.get("events").select()).fetchall()
    assert len(rows) == 2
    ids = {r[0] for r in rows}
    assert 1 in ids
    assert 3 in ids


# ---------------------------------------------------------------------------
# build_connectors
# ---------------------------------------------------------------------------


def test_build_connectors(tmp_path):
    """build_connectors が kind に応じた正しいクラスを返す。"""
    sqlite_path = tmp_path / "src.db"
    sqlite_path.touch()
    jsonl_path = tmp_path / "src.jsonl"
    jsonl_path.touch()
    json_path = tmp_path / "src.json"
    json_path.touch()

    sources = {
        "s_sqlite": SourceDef(
            kind="sqlite",
            path=str(sqlite_path),
            table="events",
            source_table="events",
        ),
        "s_jsonl": SourceDef(
            kind="jsonl",
            path=str(jsonl_path),
            table="events",
            mode="append",
        ),
        "s_json": SourceDef(
            kind="json",
            path=str(json_path),
            table="events",
            mode="replace",
        ),
    }
    config = make_config(sources=sources)
    db, registry, state = make_env(tmp_path, config)

    connectors = build_connectors(config, registry, db, state)
    assert isinstance(connectors["s_sqlite"], SqliteSourceConnector)
    assert isinstance(connectors["s_jsonl"], JsonlSourceConnector)
    assert isinstance(connectors["s_json"], JsonSourceConnector)
