"""設定モデルのバリデーションと v1 後方互換ローダーのテスト。"""

import textwrap

import pytest
from pydantic import ValidationError

from monitor_app import MonitorConfig, TableDef, ViewDef
from monitor_app.settings.loader import ConfigError, load_config


def test_primary_key_must_be_in_columns():
    with pytest.raises(ValidationError):
        TableDef(columns=["id", "name"], primary_key="missing")


def test_foreign_key_reference_must_exist():
    with pytest.raises(ValidationError):
        MonitorConfig(
            tables={
                "orders": TableDef(
                    columns=["id", "user_id"], foreign_keys={"user_id": "ghost.id"}
                )
            }
        )


def test_view_query_must_be_select():
    with pytest.raises(ValidationError):
        ViewDef(query="DELETE FROM users")


def test_column_type_inference():
    t = TableDef(columns=["id", "user_id", "amount", "name"], primary_key="id")
    assert t.column_type("id") == "int"
    assert t.column_type("user_id") == "int"
    assert t.column_type("amount") == "float"
    assert t.column_type("name") == "str"


def test_explicit_column_types_win():
    t = TableDef(columns={"id": "int", "code": "str"}, primary_key="id")
    assert t.column_type("code") == "str"


def test_load_v2_config(tmp_path):
    cfg_file = tmp_path / "config.py"
    cfg_file.write_text(textwrap.dedent("""
            from monitor_app import MonitorConfig, TableDef
            config = MonitorConfig(
                tables={"users": TableDef(columns=["id", "name"], primary_key="id")}
            )
            """))
    cfg = load_config(cfg_file)
    assert "users" in cfg.tables


def test_load_v1_config_backward_compat(tmp_path):
    cfg_file = tmp_path / "config.py"
    cfg_file.write_text(textwrap.dedent("""
            ALLOWED_TABLES = {
                "users": {"columns": ["id", "name"], "primary_key": "id"},
            }
            VIEW_TABLES = {"users_view": {"query": "SELECT * FROM users", "title": "U"}}
            TABLE_CELL_STYLES = {
                "users_view": {"id": {"greater_than": {"value": 1, "class": "bg-x"}}}
            }
            APP_TITLE = "Legacy"
            """))
    cfg = load_config(cfg_file)
    assert cfg.app_title == "Legacy"
    assert "users" in cfg.tables
    style = (
        cfg.views["users_view"]
        .styles["id"]
        .model_dump(by_alias=True, exclude_none=True)
    )
    assert style["greater_than"]["class"] == "bg-x"


def test_load_missing_file():
    with pytest.raises(ConfigError):
        load_config("/nonexistent/config.py")
