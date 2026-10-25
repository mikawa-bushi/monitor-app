"""PostgreSQL / MySQL の列に入らない値と、自分自身を参照する表の取り込み(D3-06, D3-07)。

- SQLite が受け付ける値(255 文字を超える文字列・32 ビットを超える整数)は、サーバー
  DB では 1 つで取り込み全体の失敗になっていた。その行だけを行番号つきの警告を残して
  読み飛ばす(制限は DB の実際の列の型から決める)。新しく作る表の、主キー・外部キー
  以外の整数列は BIGINT のため、32 ビットを超える整数もそのまま入る。
- 自分自身を参照する外部キー(親子のツリー)の表は、MySQL で 2 回目の置換が必ず失敗し、
  親より前に子の行がある CSV は PostgreSQL / MySQL で失敗していた。

実行方法は conftest.py を参照。
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, text

from monitor_app import MonitorConfig, TableDef
from monitor_app.db.engine import Database
from monitor_app.db.ingest_state import IngestStateStore
from monitor_app.db.registry import TableRegistry
from monitor_app.exceptions import ConflictError
from monitor_app.services.connectors import build_connectors
from monitor_app.services.importer import CsvImporter

pytestmark = pytest.mark.server_db

LIMITS = MonitorConfig(
    tables={"lim": TableDef(columns={"id": "int", "note": "str", "n": "int"})}
)
TREE = MonitorConfig(
    tables={
        "cat": TableDef(
            columns={"id": "int", "name": "str", "parent_id": "int"},
            foreign_keys={"parent_id": "cat.id"},
        )
    }
)


def _rows(url: str, sql: str) -> list[tuple]:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return [tuple(r) for r in conn.execute(text(sql))]
    finally:
        engine.dispose()


def _limits_csv() -> str:
    return (
        "id,note,n\n1,ok,1\n2,"
        + "x" * 300
        + ",2\n3,ok,1729500000000\n4,"
        + "あ" * 255
        + ",4\n2147483648,ok,5\n"
    )


@pytest.mark.parametrize("mode", ["replace", "append"])
def test_values_the_server_rejects_skip_only_their_rows(server_url, tmp_path, mode):
    (tmp_path / "lim.csv").write_text(_limits_csv(), encoding="utf-8")
    db = Database(server_url)
    try:
        registry = TableRegistry(LIMITS)
        registry.create_all(db)
        importer = CsvImporter(LIMITS, registry, db, tmp_path)
        result = importer.import_table("lim", tmp_path / "lim.csv", mode=mode)
    finally:
        db.dispose()
    assert (result.inserted, result.skipped) == (3, 2)
    assert result.warnings[0].startswith("lim.csv 3行目: 列 note の値 'xxxx")
    assert "300 文字" in result.warnings[0]
    # 主キー(INTEGER)の範囲を超える値。BIGINT の n の大きな値は入る
    assert result.warnings[1].startswith("lim.csv 6行目: 列 id の値 2147483648")
    assert _rows(server_url, "SELECT id, n FROM lim ORDER BY id") == [
        (1, 1),
        (3, 1729500000000),
        (4, 4),
    ]


def test_the_tailer_skips_rejected_values_too(server_url, tmp_path):
    path = tmp_path / "lim.csv"
    path.write_text("id,note,n\n", encoding="utf-8")
    db = Database(server_url)
    try:
        registry = TableRegistry(LIMITS)
        registry.create_all(db)
        importer = CsvImporter(LIMITS, registry, db, tmp_path)
        state = importer.init_tail("lim", path)
        path.write_text(_limits_csv(), encoding="utf-8")
        assert importer.tail_append("lim", path, state) == 3
    finally:
        db.dispose()
    assert _rows(server_url, "SELECT id FROM lim ORDER BY id") == [(1,), (3,), (4,)]


def test_limits_follow_the_actual_column_type(server_url, tmp_path):
    """作成済みの表の列が宣言より狭ければ、DB の実際の列の型で判断する。"""
    engine = create_engine(server_url)
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE lim (id INTEGER PRIMARY KEY, note VARCHAR(10), n BIGINT)"
            )
        )
    engine.dispose()
    (tmp_path / "lim.csv").write_text(
        "id,note,n\n1,short,1729500000000\n2,longer-than-ten,2\n"
    )
    db = Database(server_url)
    try:
        importer = CsvImporter(LIMITS, TableRegistry(LIMITS), db, tmp_path)
        result = importer.import_all().results[0]
    finally:
        db.dispose()
    assert "10 文字まで" in " ".join(result.warnings)
    # BIGINT の列には大きな整数も入る(PostgreSQL も宣言の BIGINT に変換して送る)
    assert _rows(server_url, "SELECT id, n FROM lim") == [(1, 1729500000000)]
    assert result.skipped == 1


def test_an_integer_column_created_by_an_earlier_version_still_limits(
    server_url, tmp_path
):
    """以前の版が INTEGER で作った列は、32 ビットを超える値の行だけを読み飛ばす。"""
    engine = create_engine(server_url)
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE lim (id INTEGER PRIMARY KEY, note VARCHAR(255), n INTEGER)"
            )
        )
    engine.dispose()
    (tmp_path / "lim.csv").write_text("id,note,n\n1,a,1729500000000\n2,b,2\n")
    db = Database(server_url)
    try:
        importer = CsvImporter(LIMITS, TableRegistry(LIMITS), db, tmp_path)
        result = importer.import_all().results[0]
    finally:
        db.dispose()
    assert result.warnings[0].startswith("lim.csv 2行目: 列 n の値 1729500000000")
    assert _rows(server_url, "SELECT id, n FROM lim") == [(2, 2)]


def test_sources_skip_rejected_values(server_url, tmp_path):
    src = tmp_path / "s.json"
    src.write_text(json.dumps([{"id": 1, "note": "ok"}, {"id": 2, "note": "y" * 256}]))
    config = MonitorConfig(
        tables=LIMITS.tables,
        sources={"s": {"kind": "json", "path": str(src), "table": "lim"}},
    )
    db = Database(server_url)
    try:
        registry = TableRegistry(config)
        registry.create_all(db)
        state = IngestStateStore(db)
        state.create()
        assert build_connectors(config, registry, db, state)["s"].poll_once() == 1
    finally:
        db.dispose()
    assert _rows(server_url, "SELECT id FROM lim") == [(1,)]


def test_a_self_referencing_table_can_be_replaced_again(server_url, tmp_path):
    (tmp_path / "cat.csv").write_text(
        "id,name,parent_id\n1,root,\n2,child,1\n3,grandchild,2\n"
    )
    db = Database(server_url)
    try:
        importer = CsvImporter(TREE, TableRegistry(TREE), db, tmp_path)
        importer.import_all()
        importer.import_all()  # 2 回目の置換(MySQL は 1 行ずつ外部キーを確かめる)
    finally:
        db.dispose()
    assert _rows(server_url, "SELECT id, parent_id FROM cat ORDER BY id") == [
        (1, None),
        (2, 1),
        (3, 2),
    ]


def test_children_before_their_parent_in_the_csv(server_url, tmp_path):
    (tmp_path / "cat.csv").write_text(
        "id,name,parent_id\n3,grandchild,2\n2,child,1\n1,root,\n"
    )
    db = Database(server_url)
    try:
        importer = CsvImporter(TREE, TableRegistry(TREE), db, tmp_path)
        assert importer.import_all().results[0].inserted == 3
    finally:
        db.dispose()
    assert _rows(server_url, "SELECT id, parent_id FROM cat ORDER BY id") == [
        (1, None),
        (2, 1),
        (3, 2),
    ]


def test_a_missing_parent_names_the_file_and_changes_nothing(server_url, tmp_path):
    (tmp_path / "cat.csv").write_text("id,name,parent_id\n1,root,\n")
    db = Database(server_url)
    try:
        importer = CsvImporter(TREE, TableRegistry(TREE), db, tmp_path)
        importer.import_all()
        (tmp_path / "cat.csv").write_text("id,name,parent_id\n1,root,\n2,orphan,9\n")
        with pytest.raises(ConflictError) as info:
            importer.import_all()
    finally:
        db.dispose()
    assert info.value.message.startswith("cat.csv: 外部キー制約に違反しました")
    assert "取り込みは取り消しました" in info.value.message
    assert _rows(server_url, "SELECT id FROM cat") == [(1,)]
