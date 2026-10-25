"""CLI・アプリ・点検で共有する小さな規則(D7-09・D7-10・D7-08)。

同じ判定を別々に書くと規則がずれる(フォルダを受け付ける場所と受け付けない場所、
空白の扱いの違う「ループバックか」など)。共有した規則がどの入口でも同じに効くことを
確かめる。
"""

from __future__ import annotations

import errno
import logging
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from monitor_app import MonitorConfig, TableDef, security
from monitor_app import checks as checks_mod
from monitor_app import deploy as deploy_mod
from monitor_app.cli import _common
from monitor_app.cli import maintenance as maintenance_mod
from monitor_app.cli import server as server_mod
from monitor_app.db import registry as registry_module
from monitor_app.db.engine import is_memory_url
from monitor_app.db.registry import TableRegistry
from monitor_app.main import app_factory
from monitor_app.settings import runtime
from monitor_app.settings.loader import load_config, resolve_config_path

CONFIG_PY = (
    "from monitor_app import MonitorConfig, TableDef\n"
    'config = MonitorConfig(tables={"t": TableDef(columns={"id": "int"})})\n'
)


def _project(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    (project / "config.py").write_text(CONFIG_PY, encoding="utf-8")
    return project


# ---------------------------------------------------------------------------
# 設定ファイルのパス(D7-09)
# ---------------------------------------------------------------------------
def test_resolve_config_path_accepts_a_folder_and_the_home_folder(
    tmp_path, monkeypatch
):
    project = _project(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert resolve_config_path(project) == project / "config.py"
    assert resolve_config_path("~/proj") == project / "config.py"
    assert resolve_config_path("~/proj/config.py") == project / "config.py"
    # 無いファイルはそのまま返す(あるかどうかは呼び出し側が確かめる)
    assert resolve_config_path(tmp_path / "nope.py") == tmp_path / "nope.py"


def test_load_config_accepts_a_project_folder(tmp_path):
    assert list(load_config(_project(tmp_path)).tables) == ["t"]


@pytest.mark.parametrize("form", ["folder", "home"])
def test_app_factory_reads_the_same_paths_as_the_cli(tmp_path, monkeypatch, form):
    """``MONITOR_CONFIG_PATH`` にもフォルダと ``~`` を書ける(``--config`` と同じ)。"""
    project = _project(tmp_path)
    (project / ".env").write_text("MONITOR_LOG_FORMAT=json\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    value = str(project) if form == "folder" else "~/proj"
    monkeypatch.setenv("MONITOR_CONFIG_PATH", value)
    monkeypatch.setenv("MONITOR_DATABASE_URL", "sqlite://")
    app = app_factory()
    assert list(app.state.config.tables) == ["t"]
    # config.py のフォルダの .env も読む(フォルダを指定したときも)
    assert app.state.settings.log_format == "json"
    assert _common.resolve_config(value) == project / "config.py"


def test_same_dir_and_the_env_file_rule_are_one_function():
    assert _common.same_dir is runtime.same_path


# ---------------------------------------------------------------------------
# ループバックの判定(D7-10)
# ---------------------------------------------------------------------------
def test_loopback_rule_is_shared_by_cli_check_and_host_guard():
    assert checks_mod.is_loopback_host is security.is_loopback_host
    assert server_mod.is_loopback is security.is_loopback_host
    for host in (" localhost ", "x.localhost", "[::1]", "127.0.0.9"):
        assert server_mod.is_loopback(host) and checks_mod.is_loopback_host(host)
    for host in ("0.0.0.0", "line1-pc", "localhost.example.com"):
        assert not server_mod.is_loopback(host)


# ---------------------------------------------------------------------------
# メモリ上の SQLite の判定(D7-10)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("url", "memory"),
    [
        ("sqlite://", True),
        ("sqlite:///:memory:", True),
        ("sqlite:///file::memory:?uri=true", True),
        ("sqlite:///file:x.db?mode=memory&uri=true", True),
        ("sqlite:///instances/database.db", False),
        ("sqlite:///file:instances/database.db?uri=true", False),
    ],
)
def test_check_migrate_and_backup_use_the_engines_memory_rule(url, memory):
    parsed = make_url(url)
    assert is_memory_url(parsed) is memory
    assert maintenance_mod._is_memory(parsed) is memory


# ---------------------------------------------------------------------------
# デプロイ用ファイルの書き出し(D7-08)
# ---------------------------------------------------------------------------
def test_write_deploy_files_reports_written_and_skipped(tmp_path):
    files = deploy_mod.plan_deploy_files(tmp_path, ["docker"])
    written, skipped = deploy_mod.write_deploy_files(files)
    assert [f.path for f in written] == [f.path for f in files] and skipped == []
    written, skipped = deploy_mod.write_deploy_files(files)
    assert written == [] and [f.path for f in skipped] == [f.path for f in files]
    written, skipped = deploy_mod.write_deploy_files(files, force=True)
    assert len(written) == len(files) and skipped == []


def test_write_deploy_files_names_the_file_that_failed(tmp_path, monkeypatch):
    def full(file):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(deploy_mod, "write_deploy_file", full)
    files = deploy_mod.plan_deploy_files(tmp_path, ["docker"])
    with pytest.raises(OSError) as info:
        deploy_mod.write_deploy_files(files)
    assert info.value.filename == str(files[0].path)


# ---------------------------------------------------------------------------
# ドライバのエラー文(D7-10)
# ---------------------------------------------------------------------------
class _Pg8000Error(Exception):
    """pg8000 はサーバーの応答を dict のまま args[0] に入れる。"""


def _fake_pg() -> SimpleNamespace:
    @contextmanager
    def connect():
        yield SimpleNamespace(dialect=postgresql.dialect())

    return SimpleNamespace(
        dialect="postgresql",
        engine=SimpleNamespace(dialect=postgresql.dialect()),
        connect=connect,
    )


def test_sequence_repair_warning_shows_only_the_servers_message(monkeypatch, caplog):
    def denied(conn, table, pk):
        orig = _Pg8000Error(
            {"S": "ERROR", "C": "42501", "M": "permission denied for sequence t_id_seq"}
        )
        raise OperationalError("SELECT setval", {}, orig)

    monkeypatch.setattr(registry_module, "repair_sequence", denied)
    monkeypatch.setattr(logging.getLogger("monitor_app"), "propagate", True)
    registry = TableRegistry(
        MonitorConfig(tables={"t": TableDef(columns={"id": "int"}, primary_key="id")})
    )
    with caplog.at_level(logging.WARNING, logger="monitor_app.db"):
        assert registry.repair_sequences(_fake_pg()) == {}
    [record] = [r for r in caplog.records if "連番" in r.message]
    assert "permission denied for sequence t_id_seq" in record.message
    assert "'S': 'ERROR'" not in record.message
