"""CLI のエラー表示と起動前チェックのテスト。

- ``--version`` / ``--debug``(MONITOR_DEBUG)などの共通オプション
- config.py が無い・誤りがあるときの短いメッセージ(runserver は取り込み・起動前に止める)
- ポート使用中の事前確認(CSV 取り込みより前)
- 既知の例外(DB・文字コード・制約違反・ポート)を 1〜3 行へ変換する層
- UTF-8 以外へリダイレクトされた出力で絵文字が落ちないこと
"""

from __future__ import annotations

import errno
import io
import os
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError, OperationalError
from typer.testing import CliRunner

import monitor_app.cli
from monitor_app import __version__
from monitor_app.cli import app
from monitor_app.cli._common import DEBUG_HINT, MISSING_CONFIG_HINT, make_streams_safe
from monitor_app.cli.server import ensure_port_available
from monitor_app.settings.loader import ConfigError

SCAFFOLD_CONFIG = (
    Path(__file__).parent.parent / "monitor_app" / "scaffold" / "config.py"
)
PORT_IN_USE = (
    "ポート {port} は使用中です(既に起動中ではありませんか? --port で変更できます)"
)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def project(tmp_path, monkeypatch, runner) -> Path:
    """startproject で雛形を作り、その中を cwd にして返す。"""
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["startproject", "proj"]).exit_code == 0
    proj = tmp_path / "proj"
    monkeypatch.chdir(proj)
    monkeypatch.setenv("MONITOR_CONFIG_PATH", "")  # runserver が書き換える値の後始末
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
    return proj


@pytest.fixture
def uvicorn_calls(monkeypatch) -> list:
    calls: list = []
    monkeypatch.setattr("uvicorn.run", lambda target, **kw: calls.append((target, kw)))
    return calls


@pytest.fixture
def busy_port():
    """127.0.0.1 上で待ち受け中(=使用中)のポート番号。"""
    sock = socket.create_server(("127.0.0.1", 0))
    try:
        yield sock.getsockname()[1]
    finally:
        sock.close()


def _free_port() -> int:
    with socket.create_server(("127.0.0.1", 0)) as sock:
        return sock.getsockname()[1]


def _db_file(proj: Path) -> Path:
    return proj / "instances" / "database.db"


def _fail_import(monkeypatch, exc: BaseException) -> None:
    """CSV 取り込みの途中で ``exc`` が起きる状況を作る。"""

    def boom(self, **_kwargs):
        raise exc

    monkeypatch.setattr("monitor_app.services.importer.CsvImporter.import_all", boom)


def _operational_error() -> OperationalError:
    return OperationalError(
        "INSERT INTO users ...",
        {},
        sqlite3.OperationalError("unable to open database file"),
    )


# ---------------------------------------------------------------------------
# 共通オプション
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("flag", ["--version", "-V"])
def test_version_flag(runner, flag):
    result = runner.invoke(app, [flag])
    assert result.exit_code == 0, result.output
    assert result.output == f"monitor-app {__version__}\n"


def test_help_lists_global_options(runner):
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "--version" in result.output
    assert "--debug" in result.output


def test_python_m_entry_points_use_the_same_app():
    import monitor_app.__main__ as pkg_main
    import monitor_app.cli.__main__ as cli_main

    assert pkg_main.app is app
    assert cli_main.app is app
    assert monitor_app.cli.app is app


def test_python_m_monitor_app_cli_keeps_its_usage_line(tmp_path):
    # 単一モジュール時代と同じく、使い方には実際に打ったコマンドを表示する。
    env = dict(os.environ, PYTHONIOENCODING="utf-8", COLUMNS="200", NO_COLOR="1")
    result = subprocess.run(
        [sys.executable, "-m", "monitor_app.cli", "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=tmp_path,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "Usage: python -m monitor_app.cli [OPTIONS] COMMAND" in result.stdout


def test_cli_configures_logging_through_logging_conf(project, runner, monkeypatch):
    # monitor_app.logging_conf.configure_logging を差し替えれば CLI にも効く
    # (単一モジュール時代の monitor_app.cli.configure_logging の差し替えと同じ用途。
    # インプロセス実行のテストがルートロガーを CliRunner の一時出力へ向けないため)。
    calls: list = []
    monkeypatch.setattr(
        "monitor_app.logging_conf.configure_logging",
        lambda *args, **kwargs: calls.append(args),
    )
    result = runner.invoke(app, ["import-csv"])
    assert result.exit_code == 0, result.output
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# config.py が無い / 誤りがある
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "command", ["check", "import-csv", "sync-sources", "runserver"]
)
def test_missing_config_prints_hint(
    tmp_path, monkeypatch, runner, uvicorn_calls, command
):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, [command])
    assert result.exit_code == 1
    assert "config.py が見つかりません" in result.stderr
    assert MISSING_CONFIG_HINT in result.stderr
    assert result.stdout == ""  # エラーは標準エラーへ
    assert uvicorn_calls == []


def test_runserver_invalid_config_stops_before_import_and_uvicorn(
    project, runner, uvicorn_calls
):
    (project / "config.py").write_text(
        "from monitor_app import MonitorConfig\n"
        "config = MonitorConfig(refresh_mode='typo')\n",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["runserver", "--import-csv"])
    assert result.exit_code == 1
    assert "refresh_mode" in result.output  # 何が悪いかを示す
    assert "Traceback" not in result.output
    assert uvicorn_calls == []
    assert not _db_file(project).exists()  # CSV も取り込まない


# ---------------------------------------------------------------------------
# runserver: ポートの事前確認と uvicorn への引数
# ---------------------------------------------------------------------------
def test_runserver_busy_port_fails_before_import(
    project, runner, uvicorn_calls, busy_port, real_port_probe
):
    result = runner.invoke(
        app,
        ["runserver", "--import-csv", "--host", "127.0.0.1", "--port", str(busy_port)],
    )
    assert result.exit_code == 1
    assert PORT_IN_USE.format(port=busy_port) in result.output
    assert uvicorn_calls == []
    # 二重起動で稼働中のサーバーのデータを置き換えない(取り込み前に止まる)。
    assert not _db_file(project).exists()


def test_runserver_reload_skips_port_check(
    project, runner, uvicorn_calls, busy_port, real_port_probe
):
    result = runner.invoke(
        app, ["runserver", "--reload", "--host", "127.0.0.1", "--port", str(busy_port)]
    )
    assert result.exit_code == 0, result.output
    _target, kw = uvicorn_calls[0]
    assert kw["reload"] is True


def test_runserver_passes_single_worker_and_shutdown_timeout(
    project, runner, uvicorn_calls
):
    result = runner.invoke(app, ["runserver", "--port", str(_free_port())])
    assert result.exit_code == 0, result.output
    _target, kw = uvicorn_calls[0]
    assert kw["workers"] == 1
    assert kw["timeout_graceful_shutdown"] == 5
    # SSE 接続中でも停止できるよう、uvicorn より先に読み込まれている。
    assert "sse_starlette.sse" in sys.modules
    assert "WEB_CONCURRENCY" not in result.output


@pytest.mark.parametrize(
    ("value", "warned"), [("3", True), ("1", False), ("auto", False)]
)
def test_runserver_warns_about_web_concurrency(
    project, runner, uvicorn_calls, monkeypatch, value, warned
):
    monkeypatch.setenv("WEB_CONCURRENCY", value)
    result = runner.invoke(app, ["runserver", "--port", str(_free_port())])
    assert result.exit_code == 0, result.output
    assert uvicorn_calls[0][1]["workers"] == 1
    assert (
        "WEB_CONCURRENCY=3 は無視して 1 プロセスで起動します" in result.output
    ) is warned


def test_ensure_port_available_leaves_other_errors_to_uvicorn(monkeypatch):
    # 使用中・権限なし・この PC のアドレスでない、以外の失敗は止めない(例外なし)。
    # (権限・アドレスの案内は test_runserver_bind_errors.py)
    def refuse(address, *, family):
        raise OSError(errno.ENOBUFS, "No buffer space available")

    monkeypatch.setattr(socket, "create_server", refuse)
    ensure_port_available("192.0.2.1", 9990)


def test_ensure_port_available_uses_ipv6_for_ipv6_hosts(monkeypatch):
    families: list = []

    class _Sock:
        def close(self) -> None:
            families.append("closed")

    def fake(address, *, family):
        families.append(family)
        return _Sock()

    monkeypatch.setattr(socket, "create_server", fake)
    ensure_port_available("::1", 9990)
    ensure_port_available("127.0.0.1", 9990)
    assert families == [socket.AF_INET6, "closed", socket.AF_INET, "closed"]


# ---------------------------------------------------------------------------
# 既知の例外を短いメッセージへ
# ---------------------------------------------------------------------------
def test_operational_error_is_shown_in_a_few_lines(project, runner, monkeypatch):
    _fail_import(monkeypatch, _operational_error())
    result = runner.invoke(app, ["import-csv"])
    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    lines = result.stderr.strip().splitlines()
    # SQLite のファイルの場所を示す(書き込めないフォルダ・ファイルの案内は
    # test_sqlite_error_hints.py)
    db_file = _db_file(project).resolve()
    assert lines[-3:] == [
        "データベースエラー: unable to open database file",
        f"ヒント: SQLite のデータベース {db_file} を開けません(保存先のフォルダと"
        "ファイル名、MONITOR_DATABASE_URL を確認してください)",
        DEBUG_HINT,
    ]
    assert "Traceback" not in result.output


def test_operational_error_other_hints(project, runner, monkeypatch):
    _fail_import(
        monkeypatch,
        OperationalError(
            "SELECT 1", {}, sqlite3.OperationalError("database is locked")
        ),
    )
    result = runner.invoke(app, ["import-csv"])
    assert result.exit_code == 1
    assert "database is locked" in result.output
    assert "少し待ってから再実行" in result.output

    # SQLite のプロジェクトでは、サーバー DB の接続先(ホスト・パスワード)は案内しない
    _fail_import(monkeypatch, OperationalError("SELECT 1", {}, Exception("timeout")))
    result = runner.invoke(app, ["import-csv"])
    assert "MONITOR_DATABASE_URL の接続先" not in result.output
    assert "SQLite のデータベースファイル" in result.output


def test_integrity_error_hint(project, runner, monkeypatch):
    _fail_import(
        monkeypatch,
        IntegrityError(
            "INSERT", {}, sqlite3.IntegrityError("UNIQUE constraint failed: users.id")
        ),
    )
    result = runner.invoke(app, ["import-csv", "--keep"])
    assert result.exit_code == 1
    assert "UNIQUE constraint failed: users.id" in result.output
    assert "--keep" in result.output


def test_unicode_decode_error_suggests_csv_encoding(project, runner, monkeypatch):
    _fail_import(
        monkeypatch,
        UnicodeDecodeError("utf-8", b"\x82\xa0", 0, 1, "invalid start byte"),
    )
    result = runner.invoke(app, ["import-csv"])
    assert result.exit_code == 1
    assert "MONITOR_CSV_ENCODING=cp932" in result.output
    assert "Traceback" not in result.output


def test_config_error_inside_command_is_short(project, runner, monkeypatch):
    _fail_import(monkeypatch, ConfigError("設定の誤りです"))
    result = runner.invoke(app, ["import-csv"])
    assert result.exit_code == 1
    assert result.stderr.splitlines()[0] == "設定の誤りです"


def test_address_in_use_from_server_is_short(project, runner, monkeypatch):
    def run(target, **kw):
        raise OSError(errno.EADDRINUSE, "Address already in use")

    monkeypatch.setattr("uvicorn.run", run)
    result = runner.invoke(app, ["runserver", "--port", str(_free_port())])
    assert result.exit_code == 1
    assert "ポートは使用中です" in result.output
    assert "--port で変更できます" in result.output


@pytest.mark.parametrize(
    "exc",
    [OSError(errno.EACCES, "Permission denied"), RuntimeError("想定外")],
    ids=["other-oserror", "runtime-error"],
)
def test_unknown_errors_are_not_hidden(project, runner, monkeypatch, exc):
    def run(target, **kw):
        raise exc

    monkeypatch.setattr("uvicorn.run", run)
    result = runner.invoke(app, ["runserver", "--port", str(_free_port())])
    assert result.exit_code == 1
    assert result.exception is exc  # 未知のエラーはそのまま(トレースバック表示)
    assert DEBUG_HINT not in result.output


def test_debug_flag_restores_traceback(project, runner, monkeypatch):
    error = _operational_error()
    _fail_import(monkeypatch, error)
    result = runner.invoke(app, ["--debug", "import-csv"])
    assert result.exit_code == 1
    assert result.exception is error
    assert "データベースエラー" not in result.output


@pytest.mark.parametrize(("value", "raw"), [("1", True), ("true", True), ("0", False)])
def test_monitor_debug_env(project, runner, monkeypatch, value, raw):
    error = _operational_error()
    _fail_import(monkeypatch, error)
    result = runner.invoke(app, ["import-csv"], env={"MONITOR_DEBUG": value})
    assert result.exit_code == 1
    assert (result.exception is error) is raw
    assert ("データベースエラー" in result.output) is not raw


# ---------------------------------------------------------------------------
# UTF-8 以外の出力先(Windows のリダイレクト等)
# ---------------------------------------------------------------------------
def test_make_streams_safe_escapes_unencodable_characters():
    buf = io.BytesIO()
    stream = io.TextIOWrapper(buf, encoding="cp932")
    make_streams_safe(stream)
    stream.write("✅ OK 日本語")
    stream.flush()
    assert buf.getvalue() == "\\u2705 OK 日本語".encode("cp932")


def test_make_streams_safe_leaves_utf8_and_odd_streams_alone():
    utf8 = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")

    class _Broken:
        encoding = "cp1252"

        def reconfigure(self, **kw):
            raise ValueError("detached")

    make_streams_safe(utf8, io.StringIO(), _Broken(), object())  # 例外なし
    assert utf8.errors == "strict"  # UTF-8 はそのまま


def test_redirected_cp932_stdout_does_not_crash_on_emoji(tmp_path):
    # CliRunner は UTF-8 で捕捉するため、実際にリダイレクトしたプロセスで確かめる。
    env = dict(os.environ, PYTHONIOENCODING="cp932")
    env.pop("PYTHONUTF8", None)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "monitor_app",
            "check",
            "--config",
            str(SCAFFOLD_CONFIG),
        ],
        capture_output=True,
        env=env,
        cwd=tmp_path,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr.decode("cp932", "replace")
    assert b"\\u2705 OK" in result.stdout
