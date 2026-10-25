"""db/bulk.py(主キー衝突の扱いつきの一括 INSERT・PostgreSQL の連番)のテスト。

方言ごとの経路のうち PostgreSQL / MySQL は tests/server_db で実サーバーを使って
確かめる。ここでは SQLite での動作と、UPSERT 構文の無い DB 向けの代替経路を見る。
"""

from __future__ import annotations

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, select, text
from sqlalchemy.dialects import postgresql

import monitor_app.db.bulk as bulk
from monitor_app import MonitorConfig, TableDef
from monitor_app.db.bulk import (
    advance_sequences,
    existing_keys,
    insert_rows,
    primary_key_name,
)
from monitor_app.db.engine import Database
from monitor_app.db.registry import TableRegistry


@pytest.fixture
def env():
    config = MonitorConfig(
        tables={
            "t": TableDef(
                columns={"id": "int", "name": "str", "v": "float"}, primary_key="id"
            ),
            "s": TableDef(columns={"code": "str", "name": "str"}, primary_key="code"),
        }
    )
    db = Database("sqlite://")
    registry = TableRegistry(config)
    registry.create_all(db)
    yield db, registry
    db.dispose()


def _rows(db, table: str) -> list[tuple]:
    with db.connect() as conn:
        return [tuple(r) for r in conn.execute(text(f"SELECT * FROM {table}"))]


def _insert(db, registry, rows, **kwargs) -> int:
    with db.connect() as conn:
        return insert_rows(conn, registry.get("t"), rows, **kwargs)


def test_error_mode_inserts_in_chunks(env, monkeypatch):
    db, registry = env
    calls: list[int] = []
    real_execute = bulk._Writer.execute

    def spy(self, stmt, run):
        calls.append(len(run))
        return real_execute(self, stmt, run)

    monkeypatch.setattr(bulk._Writer, "execute", spy)
    rows = [{"id": i, "name": f"r{i}"} for i in range(1, 8)]
    assert _insert(db, registry, rows, chunk=3) == 7
    assert calls == [3, 3, 1]
    assert len(_rows(db, "t")) == 7


def test_error_mode_raises_on_duplicate_keys(env):
    db, registry = env
    _insert(db, registry, [{"id": 1}])
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        _insert(db, registry, [{"id": 2}, {"id": 1}])
    assert _rows(db, "t") == [(1, None, None)]  # 呼び出し側のトランザクションごと取消


def test_rows_with_different_columns_keep_every_value(env):
    """executemany は 1 行目の列で文を作るため、列の組ごとに分けて実行する。"""
    db, registry = env
    rows = [
        {"id": 1, "name": "a"},
        {"id": 2, "name": "b", "v": 2.5},  # 1 行目に無い列
        {"id": 3, "v": 3.5},  # 1 行目にある列が無い
        {"id": 4, "name": "d"},
    ]
    assert _insert(db, registry, rows) == 4
    assert _rows(db, "t") == [
        (1, "a", None),
        (2, "b", 2.5),
        (3, None, 3.5),
        (4, "d", None),
    ]


def test_iterators_are_accepted(env):
    db, registry = env
    rows = ({"id": i} for i in range(1, 2501))
    assert _insert(db, registry, rows, chunk=1000) == 2500


def test_null_primary_keys_are_assigned_by_the_database(env):
    db, registry = env
    rows = [{"id": 5, "name": "x"}, {"id": None, "name": "auto"}, {"name": "auto2"}]
    assert _insert(db, registry, rows) == 3
    assert _rows(db, "t") == [(5, "x", None), (6, "auto", None), (7, "auto2", None)]


def test_ignore_skips_existing_and_repeated_keys(env):
    db, registry = env
    _insert(db, registry, [{"id": i, "name": "old"} for i in (1, 2, 3)])
    rows = [
        {"id": 2, "name": "dup"},
        {"id": 4, "name": "new"},
        {"id": 4, "name": "dup-in-batch"},  # 別のチャンクでも重複として読み飛ばす
        {"id": 5, "name": "new"},
        {"id": None, "name": "auto"},  # 主キーなしは採番して入れる
    ]
    assert _insert(db, registry, rows, on_conflict="ignore", chunk=2) == 3
    assert _rows(db, "t") == [
        (1, "old", None),
        (2, "old", None),  # 既存の行を残す
        (3, "old", None),
        (4, "new", None),  # バッチ内では先の行を残す
        (5, "new", None),
        (6, "auto", None),
    ]


def test_ignore_of_an_already_imported_batch_inserts_nothing(env):
    db, registry = env
    batch = [{"id": i, "v": float(i)} for i in range(1, 11)]
    _insert(db, registry, batch)
    assert _insert(db, registry, batch, on_conflict="ignore") == 0
    assert _insert(db, registry, [*batch, {"id": 11}], on_conflict="ignore") == 1
    assert len(_rows(db, "t")) == 11


def test_update_overwrites_existing_rows_and_counts_new_ones(env):
    db, registry = env
    _insert(db, registry, [{"id": 1, "name": "a", "v": 1.0}, {"id": 2, "name": "b"}])
    rows = [
        {"id": 1, "v": 10.0},  # name は送っていないので変えない
        {"id": 3, "name": "c"},
        {"id": 3, "name": "c2"},  # バッチ内では最後の値
        {"id": None, "name": "auto"},
        {"id": 2},  # 主キーだけの行は何も変えない
    ]
    assert _insert(db, registry, rows, on_conflict="update") == 2  # id 3 と採番の行
    assert _rows(db, "t") == [
        (1, "a", 10.0),
        (2, "b", None),
        (3, "c2", None),
        (4, "auto", None),
    ]


def test_update_merges_repeated_keys_in_order(env):
    """同じ主キーの行が 1 バッチに複数あれば、前から順に UPSERT したのと同じ結果。

    最後の行だけを使うと、前の行にだけある列(ここでは v)の値が失われる。
    """
    db, registry = env
    _insert(db, registry, [{"id": 1, "name": "old", "v": 0.5}])
    rows = [
        {"id": 5, "v": 1.0},
        {"id": 1, "v": 2.0},
        {"id": 5, "name": "x"},
        {"id": 1, "name": "new"},
        {"id": 5, "v": 3.0},
    ]
    assert _insert(db, registry, rows, on_conflict="update") == 1  # id 5 だけが新規
    assert sorted(_rows(db, "t")) == [(1, "new", 2.0), (5, "x", 3.0)]


class _MySQLRecordingConn:
    def __init__(self) -> None:
        from sqlalchemy.dialects import mysql

        self.dialect = mysql.dialect()
        self.executed: list = []

    def execute(self, stmt, params=None):
        self.executed.append(str(stmt.compile(dialect=self.dialect)))
        return _EmptyResult()


class _EmptyResult:
    def scalars(self):
        return []


def test_mysql_ignore_and_key_only_upserts_never_change_the_existing_row():
    """MySQL の「読み飛ばし」は ``pk = 表.pk``(何も変えない)にする。

    ``pk = VALUES(pk)`` では、大文字小文字を区別しない照合順序で ``'abc'`` が既存の
    ``'ABC'`` と衝突したとき、既存の行の主キーが書き換わる(実サーバーでの確認は
    tests/server_db/test_bulk_server.py)。
    """
    table = Table(
        "s",
        MetaData(),
        Column("code", String(10), primary_key=True),
        Column("name", String(10)),
    )
    conn = _MySQLRecordingConn()
    insert_rows(conn, table, [{"code": "abc", "name": "n"}], on_conflict="ignore")
    insert_rows(conn, table, [{"code": "abc"}], on_conflict="update")
    inserts = [sql for sql in conn.executed if sql.startswith("INSERT")]
    assert len(inserts) == 2
    for sql in inserts:
        assert sql.endswith("ON DUPLICATE KEY UPDATE code = s.code"), sql


def test_conflict_modes_need_a_single_primary_key():
    table = Table("nopk", MetaData(), Column("a", Integer), Column("b", Integer))
    with pytest.raises(ValueError, match="主キー"):
        primary_key_name(table)
    db = Database("sqlite://")
    try:
        table.create(db.engine)
        with db.connect() as conn:
            # error モードは主キーが無くても入る(従来どおり)
            assert insert_rows(conn, table, [{"a": 1, "b": 2}]) == 1
            with pytest.raises(ValueError):
                insert_rows(conn, table, [{"a": 1}], on_conflict="ignore")
    finally:
        db.dispose()


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [({"on_conflict": "replace"}, "on_conflict"), ({"chunk": 0}, "chunk")],
)
def test_invalid_arguments(env, kwargs, match):
    db, registry = env
    with pytest.raises(ValueError, match=match):
        _insert(db, registry, [{"id": 1}], **kwargs)


def test_string_primary_keys(env):
    db, registry = env
    with db.connect() as conn:
        table = registry.get("s")
        insert_rows(conn, table, [{"code": "A", "name": "a"}])
        assert (
            insert_rows(
                conn,
                table,
                [{"code": "A", "name": "x"}, {"code": "B", "name": "b"}],
                on_conflict="ignore",
            )
            == 1
        )
        assert (
            insert_rows(conn, table, [{"code": "A", "name": "z"}], on_conflict="update")
            == 0
        )
    assert _rows(db, "s") == [("A", "z"), ("B", "b")]


def test_fallback_without_upsert_syntax(env, monkeypatch):
    """UPSERT 構文の無い DB(SQLite 3.24 未満など)では UPDATE と INSERT に分ける。"""
    db, registry = env
    monkeypatch.setattr(bulk._Writer, "supports_upsert", lambda self: False)
    _insert(db, registry, [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}])
    assert _insert(db, registry, [{"id": 2}, {"id": 3}], on_conflict="ignore") == 1
    rows = [{"id": 1, "name": "A", "v": 1.5}, {"id": 4, "name": "d"}, {"id": 2}]
    assert _insert(db, registry, rows, on_conflict="update") == 1
    assert _rows(db, "t") == [
        (1, "A", 1.5),
        (2, "b", None),
        (3, None, None),
        (4, "d", None),
    ]


def test_old_sqlite_versions_do_not_use_upsert(env, monkeypatch):
    db, registry = env
    with db.connect() as conn:
        writer = bulk._Writer(conn, registry.get("t"), "id")
        assert writer.supports_upsert()
        monkeypatch.setattr(conn.dialect, "server_version_info", (3, 22, 0))
        assert not writer.supports_upsert()


def test_existing_keys_looks_up_in_small_batches(env, monkeypatch):
    db, registry = env
    monkeypatch.setattr(bulk, "_LOOKUP_CHUNK", 3)
    _insert(db, registry, [{"id": i} for i in range(1, 11)])
    with db.connect() as conn:
        found = existing_keys(conn, registry.get("t"), "id", [None, 2, 4, 4, 11, 9, 7])
    assert found == {2, 4, 7, 9}


def test_advance_sequences_is_a_no_op_outside_postgresql(env):
    db, registry = env
    with db.connect() as conn:
        advance_sequences(conn, registry.get("t"))  # 例外にならない(何もしない)
        assert conn.execute(select(registry.get("t"))).all() == []


class _RecordingConn:
    def __init__(self) -> None:
        self.dialect = postgresql.dialect()
        self.executed: list = []

    def execute(self, stmt, params=None):
        self.executed.append((str(stmt), params))


def test_advance_sequences_sql_for_postgresql():
    table = Table(
        "Orders", MetaData(), Column("Id", Integer, primary_key=True), schema="app"
    )
    conn = _RecordingConn()
    advance_sequences(conn, table)
    ((sql, params),) = conn.executed
    # 表の最大値と払い出し済みの値の大きい方へ進める(戻さない)。下限は 1。
    assert sql == (
        "SELECT setval(CAST(x.seq AS regclass), GREATEST(x.top, 1), x.top >= 1) "
        "FROM (SELECT s.seq, GREATEST(COALESCE(m.v, 0), COALESCE("
        "pg_sequence_last_value(CAST(s.seq AS regclass)), 0)) AS top "
        "FROM (SELECT pg_get_serial_sequence(:table, :pk) AS seq) AS s, "
        '(SELECT MAX("Id") AS v FROM app."Orders") AS m '
        "WHERE s.seq IS NOT NULL) AS x"
    )
    # 表名は識別子として引用して渡す(大文字・スキーマ付きでも正しい連番を引く)
    assert params == {"table": 'app."Orders"', "pk": "Id"}


def test_advance_sequences_skips_non_integer_keys():
    table = Table("s", MetaData(), Column("code", String(10), primary_key=True))
    conn = _RecordingConn()
    advance_sequences(conn, table)
    assert conn.executed == []


def test_mixed_batches_advance_the_sequence_before_assigning_ids():
    """PostgreSQL では、明示した id の後に採番の行を入れる前に連番を追従させる。"""
    table = Table(
        "t", MetaData(), Column("id", Integer, primary_key=True), Column("x", Integer)
    )
    conn = _RecordingConn()
    insert_rows(conn, table, [{"id": 5, "x": 1}, {"x": 2}, {"id": 9, "x": 3}])
    statements = [sql.split()[0] for sql, _params in conn.executed]
    assert statements == ["INSERT", "SELECT", "INSERT", "INSERT", "SELECT"]
