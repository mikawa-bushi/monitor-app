"""雛形(startproject が配るファイル)の内容: env.example・config.py・README。"""

from __future__ import annotations

import re
import warnings
from pathlib import Path

import pytest
from contract import helpers
from dotenv import dotenv_values

from monitor_app.settings import ConfigWarning
from monitor_app.settings.loader import load_config_with_warnings
from monitor_app.settings.runtime import (
    AppSettings,
    find_unknown_env_keys,
    known_env_keys,
)

SCAFFOLD = Path(__file__).parent.parent / "monitor_app" / "scaffold"
ENV_EXAMPLE = SCAFFOLD / "env.example"
_DOCUMENTED_RE = re.compile(r"^#?\s*(MONITOR_[A-Z0-9_]+)=", re.MULTILINE)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    helpers.isolate_from_host(monkeypatch, tmp_path)


def test_env_example_documents_every_setting():
    documented = set(_DOCUMENTED_RE.findall(ENV_EXAMPLE.read_text(encoding="utf-8")))
    expected = {f"MONITOR_{name.upper()}" for name in AppSettings.model_fields}
    assert sorted(expected - documented) == []


def test_env_example_mentions_only_real_settings():
    """コメントの例も含め、書いてある変数名に綴りの誤りがない。"""
    documented = set(_DOCUMENTED_RE.findall(ENV_EXAMPLE.read_text(encoding="utf-8")))
    assert sorted(documented - known_env_keys()) == []
    assert find_unknown_env_keys(environ={}, env_files=[ENV_EXAMPLE]) == []


def test_env_example_active_lines_keep_the_previous_values(tmp_path):
    active = dotenv_values(ENV_EXAMPLE)
    assert active == {
        "MONITOR_DATABASE_URL": "sqlite:///instances/database.db",
        "MONITOR_HOST": "127.0.0.1",
        "MONITOR_PORT": "9990",
    }
    (tmp_path / ".env").write_text(ENV_EXAMPLE.read_text(encoding="utf-8"), "utf-8")
    settings = AppSettings()
    assert (settings.database_url, settings.host, settings.port) == (
        "sqlite:///instances/database.db",
        "127.0.0.1",
        9990,
    )


def test_env_example_commented_values_are_valid_settings(tmp_path):
    """コメントを外した例の値がそのまま設定として読める。"""
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    values: dict[str, str] = {}
    for line in text.splitlines():
        match = re.match(r"^#\s*(MONITOR_[A-Z0-9_]+)=(.*)$", line)
        if match and match.group(1).lower()[8:] in AppSettings.model_fields:
            values.setdefault(match.group(1), match.group(2))
    assert len(values) > 25
    (tmp_path / ".env").write_text(
        "\n".join(f"{k}={v}" for k, v in values.items()) + "\n", encoding="utf-8"
    )
    AppSettings()  # ValidationError にならない


def test_scaffold_config_declares_explicit_types_with_the_same_results():
    with warnings.catch_warnings():
        warnings.simplefilter("error", ConfigWarning)
        config, found = load_config_with_warnings(SCAFFOLD / "config.py")
    assert found == []
    expected = {
        "users": {"id": "int", "name": "str", "email": "str"},
        "products": {"id": "int", "name": "str", "price": "float"},
        "orders": {
            "id": "int",
            "user_id": "int",
            "product_id": "int",
            "amount": "float",
        },
    }
    for name, columns in expected.items():
        tdef = config.tables[name]
        assert tdef.columns == columns  # 型を明示した辞書
        assert {c: tdef.column_type(c) for c in tdef.column_names} == columns
    assert list(config.views) == ["users_view", "products_view", "orders_summary"]
    assert "LIMIT" in config.views["orders_summary"].query  # 増え続ける表は件数を絞る


def test_scaffold_config_examples_cover_new_features():
    text = (SCAFFOLD / "config.py").read_text(encoding="utf-8")
    for needle in ('"datetime"', "retention=Retention(", "max_rows="):
        assert needle in text


def test_scaffold_readme_hints():
    text = (SCAFFOLD / "README.md").read_text(encoding="utf-8")
    for needle in (
        "monitor-app runserver --import-csv-if-empty",
        "MONITOR_CSV_ENCODING",
        "cp932",
        "MONITOR_BACKUP_DIR",
        "monitor-app serve",
        "--write-config",
    ):
        assert needle in text, needle


def test_startproject_ships_the_documented_env_example(tmp_path):
    project = helpers.start_project(tmp_path)
    assert (project / ".env.example").read_text(encoding="utf-8") == (
        ENV_EXAMPLE.read_text(encoding="utf-8")
    )


def test_env_example_marks_the_variables_that_only_work_in_the_shell():
    """MONITOR_DEBUG・MONITOR_CONFIG_PATH は .env からは読まれない(プロセスの環境変数のみ)。

    ``# MONITOR_DEBUG=1`` のように .env の行の形で書くと、# を外しても効かない。
    """
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    section = text.split("=== コマンド")[1]
    assert ".env に書いても効かない" in section
    for name in ("MONITOR_DEBUG", "MONITOR_CONFIG_PATH"):
        lines = [line for line in section.splitlines() if f"{name}=" in line]
        assert lines and not any(line.startswith(f"# {name}=") for line in lines)
