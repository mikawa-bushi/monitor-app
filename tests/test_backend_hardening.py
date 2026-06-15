"""レビュー低優先のバックエンド改修(E8 / V4 / V5 / V6)のテスト。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from sqlalchemy import insert, text

from monitor_app import MonitorConfig, KpiCard, TableDef
from monitor_app.db.engine import Database
from monitor_app.db.registry import TableRegistry
from monitor_app.exceptions import RecordNotFoundError
from monitor_app.services import importer as importer_mod
from monitor_app.services.crud_service import CrudService
from monitor_app.services.importer import insert_in_chunks
from monitor_app.services.kpi_service import KpiService


# ---------------------------------------------------------------------------
# E8: パスパラメータ主キーの型変換
# ---------------------------------------------------------------------------
def _crud(config: MonitorConfig) -> CrudService:
    return CrudService(config, registry=None, repository=None)  # _coerce_pk のみ使う


def test_coerce_pk_converts_string_to_column_type():
    cfg = MonitorConfig(
        tables={"t": TableDef(columns={"id": "int", "v": "str"}, primary_key="id")}
    )
    pk = _crud(cfg)._coerce_pk("t", "5")
    assert pk == 5 and isinstance(pk, int)


def test_coerce_pk_invalid_type_is_not_found():
    cfg = MonitorConfig(
        tables={"t": TableDef(columns={"id": "int", "v": "str"}, primary_key="id")}
    )
    with pytest.raises(RecordNotFoundError):
        _crud(cfg)._coerce_pk("t", "abc")


def test_get_record_by_numeric_string_id(client):
    # conftest の users は int 主キー。URL の文字列 id が coerce されて取得できる。
    res = client.get("/api/tables/users/1")
    assert res.status_code == 200
    assert res.json()["data"]["id"] == 1


# ---------------------------------------------------------------------------
# V4: KPI を 1 コネクションで評価し、失敗を分離する
# ---------------------------------------------------------------------------
def test_kpi_evaluate_all_isolates_failing_query():
    cfg = MonitorConfig(
        tables={"t": TableDef(columns=["id"], primary_key="id")},
        kpis={
            # 失敗する KPI を先に置く。共有コネクションが rollback で回復し、
            # 後続の正常 KPI が評価できることを確認する。
            "bad": KpiCard(title="bad", query="SELECT * FROM does_not_exist"),
            "good": KpiCard(title="good", query="SELECT 42"),
        },
    )
    db = Database("sqlite:///:memory:")
    results = {r["key"]: r for r in KpiService(cfg, db).evaluate_all()}
    assert results["bad"]["value"] is None
    assert results["good"]["value"] == 42.0


# ---------------------------------------------------------------------------
# V5: チャンク挿入
# ---------------------------------------------------------------------------
def test_insert_in_chunks_inserts_all_rows(monkeypatch):
    monkeypatch.setattr(importer_mod, "INSERT_CHUNK", 2)  # 小さくして分割を強制
    cfg = MonitorConfig(
        tables={"t": TableDef(columns={"id": "int", "v": "str"}, primary_key="id")}
    )
    registry = TableRegistry(cfg)
    db = Database("sqlite:///:memory:")
    registry.create_all(db)
    table = registry.get("t")
    rows = [{"id": i, "v": f"r{i}"} for i in range(5)]

    with db.connect() as conn:
        insert_in_chunks(conn, table, rows)
    with db.connect() as conn:
        n = conn.execute(text("SELECT COUNT(*) FROM t")).scalar()
    assert n == 5


# ---------------------------------------------------------------------------
# V6: インデックス自動生成と検証
# ---------------------------------------------------------------------------
def test_indexes_created_for_fk_and_declared(tmp_path):
    cfg = MonitorConfig(
        tables={
            "users": TableDef(columns=["id", "name"], primary_key="id"),
            "orders": TableDef(
                columns={"id": "int", "user_id": "int", "ts": "str"},
                primary_key="id",
                foreign_keys={"user_id": "users.id"},
                indexes=[["ts"]],
            ),
        }
    )
    db = Database(f"sqlite:///{tmp_path / 'app.db'}")
    TableRegistry(cfg).create_all(db)
    with db.connect() as conn:
        names = {
            r[0]
            for r in conn.execute(
                text(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='index' AND tbl_name='orders'"
                )
            )
        }
    assert "ix_orders_user_id" in names  # FK 列に自動付与
    assert "ix_orders_ts" in names  # 宣言インデックス


def test_index_on_unknown_column_rejected():
    with pytest.raises(ValidationError):
        TableDef(columns=["id", "v"], primary_key="id", indexes=[["nope"]])
