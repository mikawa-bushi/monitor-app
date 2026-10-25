"""db/bulk.py の実サーバーでの境界条件(PostgreSQL の連番・MySQL の照合順序)。

実行方法は conftest.py を参照(``MONITOR_TEST_PG_URL`` / ``MONITOR_TEST_MYSQL_URL``)。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select, text

from monitor_app import MonitorConfig, TableDef
from monitor_app.db.bulk import insert_rows
from monitor_app.db.engine import Database
from monitor_app.db.registry import TableRegistry

pytestmark = pytest.mark.server_db


@pytest.fixture
def env(server_url):
    config = MonitorConfig(
        tables={
            "n": TableDef(columns={"id": "int", "v": "str"}, primary_key="id"),
            "s": TableDef(columns={"code": "str", "name": "str"}, primary_key="code"),
        }
    )
    db = Database(server_url)
    registry = TableRegistry(config)
    registry.create_all(db)
    yield db, registry
    db.dispose()


def test_ids_at_or_below_zero_do_not_break_the_sequence(env):
    """表の id がすべて 0 以下でも取り込める(PostgreSQL の setval は 1 未満を拒む)。"""
    db, registry = env
    table = registry.get("n")
    with db.connect() as conn:
        assert insert_rows(conn, table, [{"id": 0, "v": "zero"}]) == 1
        assert insert_rows(conn, table, [{"id": -5, "v": "neg"}]) == 1
    with db.connect() as conn:
        conn.execute(table.insert().values(v="auto"))
        ids = conn.execute(select(table.c.id).order_by(table.c.id)).scalars().all()
    assert ids == [-5, 0, 1]


def test_advancing_never_lowers_the_sequence_below_an_id_in_use(env, server_url):
    """別のトランザクションが採番した(未確定の)id より連番を戻さない。

    戻すと、そのトランザクションの確定後に同じ id が払い出されて主キーが衝突する。
    """
    if not server_url.startswith("postgresql"):
        pytest.skip("PostgreSQL の連番の確認")
    db, registry = env
    table = registry.get("n")
    with db.connect() as conn:
        insert_rows(conn, table, [{"id": i, "v": "csv"} for i in range(1, 11)])
    other = db.engine.connect()
    try:
        trans = other.begin()
        auto_id = other.execute(
            table.insert().values(v="form").returning(table.c.id)
        ).scalar_one()
        assert auto_id == 11  # まだ確定していない
        with db.connect() as conn:  # 同時に動く、主キーを明示した取り込み
            assert insert_rows(conn, table, [{"id": 0, "v": "ingest"}]) == 1
        trans.commit()
    finally:
        other.close()
    with db.connect() as conn:
        conn.execute(table.insert().values(v="next"))  # 11 と衝突しない
        ids = conn.execute(select(table.c.id).order_by(table.c.id)).scalars().all()
    assert ids == [0, *range(1, 12), 12]


def test_ignore_and_key_only_upserts_keep_the_existing_key(env):
    """読み飛ばし・主キーだけの UPSERT で既存の行(主キーの表記を含む)を変えない。

    MySQL 8 の既定の照合順序は大文字小文字を区別しないため ``'abc'`` と ``'ABC'`` が
    衝突する。PostgreSQL では別のキーとして入る。
    """
    db, registry = env
    table = registry.get("s")
    with db.connect() as conn:
        insert_rows(conn, table, [{"code": "ABC", "name": "first"}])
    with db.connect() as conn:
        insert_rows(conn, table, [{"code": "abc", "name": "2nd"}], on_conflict="ignore")
        insert_rows(conn, table, [{"code": "abc"}], on_conflict="update")
    with db.connect() as conn:
        rows = dict(conn.execute(text("SELECT code, name FROM s")).all())
    assert rows["ABC"] == "first"
    if db.dialect in ("mysql", "mariadb"):
        assert rows == {"ABC": "first"}
    else:
        assert rows == {"ABC": "first", "abc": "2nd"}


def test_update_merges_repeated_keys_in_one_batch(env):
    db, registry = env
    table = registry.get("s")
    rows = [{"code": "k", "name": "a"}, {"code": "k"}, {"code": "k", "name": "b"}]
    with db.connect() as conn:
        assert insert_rows(conn, table, rows, on_conflict="update") == 1
        assert insert_rows(conn, table, rows[:2], on_conflict="update") == 0
    with db.connect() as conn:
        assert conn.execute(text("SELECT code, name FROM s")).all() == [("k", "a")]
