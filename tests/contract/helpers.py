"""安全網テスト(E2E・CLI スモーク・既知の不具合)で共有する小さな道具。"""

from __future__ import annotations

import importlib
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path


def isolate_from_host(monkeypatch, workdir: Path) -> None:
    """テストを開発者の環境から切り離す(pytest のフィクスチャから呼ぶ)。

    - シェルに残った ``MONITOR_*`` を外し、作業ディレクトリを ``workdir`` にする
      (リポジトリ直下の .env などを読まない)。
    - ``configure_logging()`` を何もしない関数に置き換える。インプロセスで実行した CLI
      が dictConfig でルートロガーのハンドラを CliRunner の一時ストリームに差し替え、
      後続のテストのログ出力で "Logging error" を出すのを防ぐ。内部の名前が変わって
      いても失敗しない(その場合はこの抑止が効かないだけ)。
    """
    for key in list(os.environ):
        if key.upper().startswith("MONITOR_"):
            monkeypatch.delenv(key)
    monkeypatch.chdir(workdir)

    def _noop(*_args, **_kwargs) -> None:
        return None

    for module_name in ("monitor_app.logging_conf", "monitor_app.cli"):
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        if hasattr(module, "configure_logging"):
            monkeypatch.setattr(module, "configure_logging", _noop)


def child_env(**overrides: str) -> dict[str, str]:
    """子プロセス(``python -m monitor_app``)用の環境変数。

    開発者のシェルの ``MONITOR_*`` を持ち込まず、標準入出力は UTF-8 に固定する。
    Windows の cp932 などで ✅ の出力が落ちる既知の不具合(DX-03)の巻き添えで別の
    テストが落ちないようにするため。DX-03 自体の確認では ``overrides`` で上書きする。
    """
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("MONITOR_")}
    env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    env.update(overrides)
    return env


def run_cli(
    *args: str, cwd: Path, env: dict[str, str] | None = None, timeout: float = 60
) -> subprocess.CompletedProcess:
    """``python -m monitor_app <args>`` を実行する(出力はバイト列のまま返す)。"""
    return subprocess.run(
        [sys.executable, "-m", "monitor_app", *args],
        cwd=cwd,
        env=child_env() if env is None else env,
        capture_output=True,
        timeout=timeout,
    )


def start_project(parent: Path, name: str = "proj") -> Path:
    """``startproject`` で雛形を作る(インプロセス。CliRunner は出力を UTF-8 で扱う)。"""
    from typer.testing import CliRunner

    from monitor_app.cli import app

    target = parent / name
    result = CliRunner().invoke(app, ["startproject", str(target)])
    if result.exit_code != 0:
        raise AssertionError(f"startproject に失敗しました:\n{result.output}")
    return target


def sqlite_url(path: Path) -> str:
    """ファイルパスから SQLite の接続 URL を作る(Windows のパスも ``/`` 区切りにする)。"""
    return f"sqlite:///{path.as_posix()}"


def query(db_path: Path, sql: str) -> list[tuple]:
    """SQLite ファイルへ直接 SQL を投げる(アプリを介さずに DB の中身を確かめる)。"""
    with closing(sqlite3.connect(db_path)) as conn:
        return conn.execute(sql).fetchall()


def case_insensitive_fs(directory: Path) -> bool:
    """``directory`` のファイルシステムが大文字小文字を区別しないか(Windows・macOS の
    既定)。区別しないと ``USERS.CSV`` を書くと ``users.csv`` を上書きする。"""
    probe = directory / "case-probe.tmp"
    probe.touch()
    try:
        return (directory / "CASE-PROBE.TMP").exists()
    finally:
        probe.unlink()
