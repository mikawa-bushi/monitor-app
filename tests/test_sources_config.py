"""SourceDef・IngestStateStore・integrations.merge のテスト。"""

import pytest
from pydantic import ValidationError

from monitor_app import MonitorConfig, TableDef, ViewDef
from monitor_app.db.engine import Database
from monitor_app.db.ingest_state import IngestStateStore
from monitor_app.integrations import merge
from monitor_app.settings.declarative import AlertRule, KpiCard, SourceDef


# ---------------------------------------------------------------------------
# SourceDef バリデーション
# ---------------------------------------------------------------------------


def test_sqlite_requires_source_table_or_query():
    """sqlite で source_table も query もない → ValidationError。"""
    with pytest.raises(ValidationError):
        SourceDef(kind="sqlite", path="/tmp/test.db", table="t1")


def test_sqlite_source_table_identifier_check():
    """source_table に識別子以外の文字列 → ValidationError。"""
    with pytest.raises(ValidationError):
        SourceDef(
            kind="sqlite",
            path="/tmp/test.db",
            table="t1",
            source_table="a; DROP",
        )


def test_sqlite_watermark_column_identifier_check():
    """watermark_column に識別子以外の文字列 → ValidationError。"""
    with pytest.raises(ValidationError):
        SourceDef(
            kind="sqlite",
            path="/tmp/test.db",
            table="t1",
            source_table="valid_table",
            watermark_column="a; DROP",
        )


def test_jsonl_mode_replace_is_invalid():
    """jsonl + mode='replace' → ValidationError。"""
    with pytest.raises(ValidationError):
        SourceDef(
            kind="jsonl",
            path="/tmp/test.jsonl",
            table="t1",
            mode="replace",
        )


def test_sqlite_valid_source_table():
    """sqlite + source_table 指定 → OK。"""
    sdef = SourceDef(
        kind="sqlite",
        path="/tmp/test.db",
        table="t1",
        source_table="proc_log",
    )
    assert sdef.source_table == "proc_log"


def test_sqlite_valid_query():
    """sqlite + query 指定 → OK。"""
    sdef = SourceDef(
        kind="sqlite",
        path="/tmp/test.db",
        table="t1",
        query="SELECT * FROM proc_log WHERE id > :wm",
    )
    assert sdef.query is not None


def test_jsonl_append_mode_valid():
    """jsonl + mode='append'(デフォルト) → OK。"""
    sdef = SourceDef(kind="jsonl", path="/tmp/test.jsonl", table="t1")
    assert sdef.mode == "append"


def test_json_watermark_column_is_invalid():
    """json + watermark_column → ValidationError。"""
    with pytest.raises(ValidationError):
        SourceDef(
            kind="json",
            path="/tmp/test.json",
            table="t1",
            watermark_column="ts",
        )


# ---------------------------------------------------------------------------
# MonitorConfig.sources バリデーション
# ---------------------------------------------------------------------------


def test_monitor_config_source_table_not_in_tables():
    """sources.table が tables にない → ValidationError。"""
    with pytest.raises(ValidationError):
        MonitorConfig(
            tables={
                "t1": TableDef(columns=["id", "name"], primary_key="id"),
            },
            sources={
                "s1": SourceDef(
                    kind="jsonl",
                    path="/tmp/test.jsonl",
                    table="nonexistent",
                )
            },
        )


def test_monitor_config_source_table_valid():
    """sources.table が tables にある → OK。"""
    cfg = MonitorConfig(
        tables={
            "t1": TableDef(columns=["id", "name"], primary_key="id"),
        },
        sources={
            "s1": SourceDef(
                kind="jsonl",
                path="/tmp/test.jsonl",
                table="t1",
            )
        },
    )
    assert "s1" in cfg.sources
    assert cfg.sources["s1"].table == "t1"


# ---------------------------------------------------------------------------
# IngestStateStore
# ---------------------------------------------------------------------------


@pytest.fixture()
def ingest_store():
    """インメモリ SQLite を使う IngestStateStore。"""
    db = Database("sqlite://")
    store = IngestStateStore(db)
    store.create()
    return store


def test_ingest_store_get_before_set_returns_none(ingest_store):
    """create 後に get → None。"""
    assert ingest_store.get("source_a") is None


def test_ingest_store_set_and_get(ingest_store):
    """set → get で同じ値が返る。"""
    ingest_store.set("source_a", "cursor_value_1")
    assert ingest_store.get("source_a") == "cursor_value_1"


def test_ingest_store_overwrite(ingest_store):
    """set(上書き) → get で上書き後の値が返る。"""
    ingest_store.set("source_a", "cursor_value_1")
    ingest_store.set("source_a", "cursor_value_2")
    assert ingest_store.get("source_a") == "cursor_value_2"


def test_ingest_store_multiple_sources(ingest_store):
    """複数ソースが独立して管理される。"""
    ingest_store.set("source_a", "aaa")
    ingest_store.set("source_b", "bbb")
    assert ingest_store.get("source_a") == "aaa"
    assert ingest_store.get("source_b") == "bbb"


def test_ingest_store_create_is_idempotent():
    """create() を複数回呼んでもエラーにならない。"""
    db = Database("sqlite://")
    store = IngestStateStore(db)
    store.create()
    store.create()  # 2 回目も OK


# ---------------------------------------------------------------------------
# integrations.merge
# ---------------------------------------------------------------------------


@pytest.fixture()
def base_config():
    """マージのベースとなる MonitorConfig。"""
    return MonitorConfig(
        tables={
            "orders": TableDef(columns=["id", "amount"], primary_key="id"),
        },
        views={
            "orders_view": ViewDef(query="SELECT * FROM orders"),
        },
    )


def test_merge_adds_tables_views_sources(base_config):
    """正常合成: tables/views/sources が増え、元 config は不変。"""
    new_table = TableDef(columns=["id", "name"], primary_key="id")
    new_source = SourceDef(kind="jsonl", path="/tmp/data.jsonl", table="users")
    new_view = ViewDef(query="SELECT * FROM users")

    result = merge(
        base_config,
        tables={"users": new_table},
        views={"users_view": new_view},
        sources={"s1": new_source},
    )

    # 新しい config にマージ結果が含まれる
    assert "users" in result.tables
    assert "orders" in result.tables
    assert "users_view" in result.views
    assert "orders_view" in result.views
    assert "s1" in result.sources

    # 元の config は変更されない
    assert "users" not in base_config.tables
    assert "users_view" not in base_config.views
    assert "s1" not in base_config.sources


def test_merge_table_name_conflict_raises(base_config):
    """テーブル名衝突 → ValueError。"""
    with pytest.raises(ValueError, match="tables"):
        merge(
            base_config,
            tables={"orders": TableDef(columns=["id", "qty"], primary_key="id")},
        )


def test_merge_view_name_conflict_raises(base_config):
    """ビュー名衝突 → ValueError。"""
    with pytest.raises(ValueError, match="views"):
        merge(
            base_config,
            views={
                "orders_view": ViewDef(query="SELECT 1"),
            },
        )


def test_merge_source_name_conflict_raises(base_config):
    """ソース名衝突 → ValueError。"""
    cfg_with_source = MonitorConfig(
        tables={
            "t1": TableDef(columns=["id"], primary_key="id"),
        },
        sources={
            "s1": SourceDef(kind="jsonl", path="/tmp/a.jsonl", table="t1"),
        },
    )
    with pytest.raises(ValueError, match="sources"):
        merge(
            cfg_with_source,
            sources={
                "s1": SourceDef(kind="jsonl", path="/tmp/b.jsonl", table="t1"),
            },
        )


def test_merge_revalidates_alert_view_reference(base_config):
    """合成後の再検証: 存在しないビューを指す AlertRule → ValidationError。"""
    bad_alert = AlertRule(
        view="nonexistent_view",
        column="amount",
        op=">",
        value=100,
    )
    with pytest.raises(ValidationError):
        merge(base_config, alerts=[bad_alert])


def test_merge_alerts_are_combined(base_config):
    """アラートは元のリストと追加分が結合される。"""
    good_alert = AlertRule(
        view="orders_view",
        column="amount",
        op=">",
        value=1000,
    )
    result = merge(base_config, alerts=[good_alert])
    assert len(result.alerts) == 1
    assert result.alerts[0].view == "orders_view"
