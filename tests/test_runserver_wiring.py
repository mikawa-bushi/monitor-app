"""``runserver`` の起動まわり(uvicorn はモック): uvicorn への引数、起動時のログ、
公開範囲に応じた警告と案内。
"""

from __future__ import annotations

import json
import signal
import socket
from pathlib import Path

import pytest
from typer.testing import CliRunner

import monitor_app
from monitor_app.cli import app
from monitor_app.cli import server as server_mod
from monitor_app.cli._common import stop_signals
from monitor_app.cli.server import describe_database_url, is_loopback, lan_urls
from monitor_app.logging_conf import build_logging_config

SECRET = "s3cret-pw"


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def project(tmp_path, monkeypatch, runner) -> Path:
    # シェルの MONITOR_* は conftest の自動フィクスチャが外す
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["startproject", "proj"]).exit_code == 0
    proj = tmp_path / "proj"
    monkeypatch.chdir(proj)
    monkeypatch.setenv("MONITOR_CONFIG_PATH", "")  # runserver が書き換える値の後始末
    monkeypatch.setattr(server_mod, "_in_container", lambda: False)
    return proj


@pytest.fixture
def calls(monkeypatch) -> list:
    recorded: list = []
    monkeypatch.setattr(
        "uvicorn.run", lambda target, **kw: recorded.append((target, kw))
    )
    return recorded


def _run(runner, *args: str, env: dict[str, str] | None = None):
    result = runner.invoke(app, ["runserver", *args], env=env)
    assert result.exit_code == 0, result.output
    return result


# ---------------------------------------------------------------------------
# uvicorn.run への引数
# ---------------------------------------------------------------------------
def test_uvicorn_receives_log_config_and_access_log(project, runner, calls):
    _run(runner)
    target, kw = calls[0]
    assert target == "monitor_app.main:app_factory"
    # 既存の引数は変えない
    assert kw["factory"] is True
    assert (kw["host"], kw["port"]) == ("127.0.0.1", 9990)
    assert kw["workers"] == 1
    assert kw["timeout_graceful_shutdown"] == 5
    assert kw["log_level"] == "info"
    # 追加した引数
    assert kw["access_log"] is True
    assert kw["log_config"] == build_logging_config("INFO", uvicorn=True)
    assert "forwarded_allow_ips" not in kw  # 未設定なら uvicorn の既定に任せる


def test_settings_reach_uvicorn(project, runner, calls):
    env = {
        "MONITOR_ACCESS_LOG": "false",
        "MONITOR_FORWARDED_ALLOW_IPS": "10.0.0.0/8,127.0.0.1",
        "MONITOR_LOG_FORMAT": "json",
    }
    _run(runner, env=env)
    _target, kw = calls[0]
    assert kw["access_log"] is False
    assert kw["forwarded_allow_ips"] == "10.0.0.0/8,127.0.0.1"
    assert kw["log_config"]["formatters"]["monitor"]["()"].endswith("JsonFormatter")
    assert kw["log_config"]["loggers"]["uvicorn.access"]["handlers"] == []


def test_json_log_format_applies_to_the_startup_log(project, runner, calls):
    result = _run(runner, env={"MONITOR_LOG_FORMAT": "json"})
    records = [json.loads(line) for line in result.stderr.splitlines() if line]
    startup = [r for r in records if "を起動します" in r["message"]]
    assert startup and startup[0]["logger"] == "monitor_app.cli"


# ---------------------------------------------------------------------------
# 起動時のログ
# ---------------------------------------------------------------------------
def test_startup_line_has_version_url_and_database(project, runner, calls):
    result = _run(runner)
    db = (project / "instances" / "database.db").resolve()
    assert f"monitor-app {monitor_app.__version__} を起動します" in result.output
    assert "http://127.0.0.1:9990/" in result.output
    assert f"DB: SQLite {db}" in result.output
    # ループバックなら警告も LAN の案内も出さない
    assert "【注意】" not in result.output
    assert "ほかの PC から" not in result.output


def test_startup_line_masks_the_database_password(project, runner, calls):
    url = f"postgresql+psycopg://monitor:{SECRET}@db.local:5432/monitor"
    result = _run(runner, env={"MONITOR_DATABASE_URL": url})
    assert SECRET not in result.output
    assert "postgresql+psycopg://monitor:***@db.local:5432/monitor" in result.output


def test_public_bind_without_api_key_warns_and_shows_lan_urls(
    project, runner, calls, monkeypatch
):
    monkeypatch.setattr(socket, "gethostname", lambda: "line1-pc")
    monkeypatch.setattr(server_mod, "_primary_ip", lambda: "192.168.10.20")
    result = _run(runner, "--host", "0.0.0.0", "--port", "8123")
    assert (
        "【注意】API キー(MONITOR_API_KEY)を設定せずに 0.0.0.0 で公開" in result.output
    )
    assert "http://line1-pc:8123/ または http://192.168.10.20:8123/" in result.output
    assert "http://127.0.0.1:8123/" in result.output  # このPCから開く URL


def test_public_bind_with_api_key_only_shows_lan_urls(project, runner, calls):
    result = _run(runner, "--host", "0.0.0.0", env={"MONITOR_API_KEY": SECRET})
    assert "【注意】" not in result.output
    assert "ほかの PC からは" in result.output
    assert SECRET not in result.output


def test_container_hint_replaces_lan_urls(project, runner, calls, monkeypatch):
    monkeypatch.setattr(server_mod, "_in_container", lambda: True)
    result = _run(runner, "--host", "0.0.0.0", env={"MONITOR_API_KEY": "k"})
    assert "コンテナの外からは" in result.output
    assert "ほかの PC からは" not in result.output


def test_root_path_is_mentioned(project, runner, calls):
    result = _run(runner, env={"MONITOR_ROOT_PATH": "/monitor/"})
    assert "公開パス /monitor" in result.output


# ---------------------------------------------------------------------------
# 補助関数
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("sqlite://", "SQLite(メモリ上。終了すると消えます)"),
        ("sqlite:///:memory:", "SQLite(メモリ上。終了すると消えます)"),
        # 共有キャッシュのメモリ DB も、ファイルではなくメモリ上の DB として示す
        (
            "sqlite:///file:shared?mode=memory&uri=true",
            "SQLite(メモリ上。終了すると消えます)",
        ),
        ("sqlite:///file:data.db?uri=true", "SQLite file:data.db"),
        (
            "mysql+pymysql://root:pw@localhost:3306/app",
            "mysql+pymysql://root:***@localhost:3306/app",
        ),
        ("postgresql+pg8000://u@h/db", "postgresql+pg8000://u@h/db"),
        (
            "not a url",
            "(解釈できない接続 URL。MONITOR_DATABASE_URL を確認してください)",
        ),
    ],
)
def test_describe_database_url(url, expected):
    assert describe_database_url(url) == expected


def test_describe_relative_sqlite_path_is_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    expected = (tmp_path / "instances" / "x.db").resolve()
    assert describe_database_url("sqlite:///instances/x.db") == f"SQLite {expected}"


@pytest.mark.parametrize(
    ("host", "loopback"),
    [
        ("127.0.0.1", True),
        ("127.0.0.5", True),
        ("::1", True),
        ("[::1]", True),
        ("localhost", True),
        ("LOCALHOST", True),
        ("0.0.0.0", False),
        ("::", False),
        ("192.168.1.2", False),
        ("line1-pc", False),
    ],
)
def test_is_loopback(host, loopback):
    assert is_loopback(host) is loopback


def test_lan_urls(monkeypatch):
    monkeypatch.setattr(socket, "gethostname", lambda: "pc")
    monkeypatch.setattr(server_mod, "_primary_ip", lambda: None)
    assert lan_urls("192.168.1.2", 80) == ["http://192.168.1.2:80/"]
    assert lan_urls("fe80::1", 80) == ["http://[fe80::1]:80/"]
    assert lan_urls("::", 9990) == ["http://pc:9990/"]
    monkeypatch.setattr(server_mod, "_primary_ip", lambda: "pc")
    assert lan_urls("0.0.0.0", 9990) == ["http://pc:9990/"]  # 重複は 1 つに


def test_primary_ip_without_a_route(monkeypatch):
    class _NoRoute:
        def __init__(self, *args):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def connect(self, address):
            raise OSError("Network is unreachable")

    monkeypatch.setattr(server_mod.socket, "socket", _NoRoute)
    assert server_mod._primary_ip() is None


def test_primary_ip_is_an_address_or_none():
    address = server_mod._primary_ip()
    assert address is None or not address.startswith("127.")


@pytest.mark.parametrize(
    "url",
    [
        f"postgresql+psycopg://monitor@db/monitor?password={SECRET}&sslmode=require",
        f"mysql+pymysql://u@h/db?ssl_key=/k.pem&passwd={SECRET}",
        f"postgresql+pg8000://u:{SECRET}@h/db?token={SECRET}",
    ],
)
def test_describe_database_url_masks_secrets_in_the_query(url):
    described = describe_database_url(url)
    assert SECRET not in described
    assert "***" in described


def test_describe_database_url_keeps_harmless_query_values():
    url = "postgresql+psycopg://u@h/db?sslmode=require&connect_timeout=5"
    described = describe_database_url(url)
    assert "sslmode=require" in described and "connect_timeout=5" in described
    assert "***" not in described


# ---------------------------------------------------------------------------
# 起動の準備中の SIGTERM(docker stop で PID 1 のまま待たされない)
# ---------------------------------------------------------------------------
def _send_sigterm() -> None:
    """プロセスへ SIGTERM が届いたときと同じく、今のハンドラを呼ぶ。"""
    handler = signal.getsignal(signal.SIGTERM)
    assert callable(handler), "準備中は SIGTERM のハンドラが登録されている"
    handler(signal.SIGTERM, None)


def test_sigterm_during_the_startup_import_stops_without_serving(
    project, runner, calls, monkeypatch
):
    original = signal.getsignal(signal.SIGTERM)
    raised: list[int] = []
    monkeypatch.setattr(server_mod.signal, "raise_signal", raised.append)
    imported: list[str] = []

    def slow_import(settings, cfg, **kwargs):
        imported.append("start")
        _send_sigterm()  # 取り込みの途中で docker stop された
        imported.append("not reached")

    monkeypatch.setattr(server_mod, "run_csv_import", slow_import)
    result = runner.invoke(app, ["runserver", "--import-csv-if-empty"])

    assert result.exit_code == 0, result.output
    assert imported == ["start"]  # 取り込みはそこで打ち切る
    assert calls == []  # サーバーは起動しない
    assert "起動を中止しました" in result.output
    # 元のハンドラに戻してから受け直す(PID 1 でなければ既定の動作で終了する)
    assert raised == [signal.SIGTERM]
    assert signal.getsignal(signal.SIGTERM) == original


def test_sigterm_handler_is_restored_before_uvicorn_starts(
    project, runner, monkeypatch
):
    """uvicorn は起動時のハンドラを停止後に呼び直すため、準備用のハンドラを残さない。"""
    original = signal.getsignal(signal.SIGTERM)
    seen: list = []
    monkeypatch.setattr(
        "uvicorn.run", lambda *a, **kw: seen.append(signal.getsignal(signal.SIGTERM))
    )
    _run(runner, "--import-csv-if-empty")
    assert seen == [original]


def test_stop_signals_outside_the_main_thread_is_a_no_op():
    import threading

    errors: list[BaseException] = []

    def worker():
        try:
            with stop_signals():
                pass
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    assert errors == []


def test_sigterm_is_not_swallowed_by_broad_exception_handlers(
    project, runner, calls, monkeypatch
):
    """取り込み側の ``except Exception`` を通り抜けて、起動の中止まで届く。"""
    monkeypatch.setattr(server_mod.signal, "raise_signal", lambda sig: None)

    def import_with_a_broad_handler(settings, cfg, **kwargs):
        try:
            _send_sigterm()
        except Exception:  # noqa: BLE001 - 1 ファイルの失敗で全体を止めない、など
            pytest.fail("SIGTERM が except Exception に握りつぶされた")

    monkeypatch.setattr(server_mod, "run_csv_import", import_with_a_broad_handler)
    result = runner.invoke(app, ["runserver", "--import-csv"])
    assert result.exit_code == 0, result.output
    assert calls == []
    assert "起動を中止しました" in result.output
