"""``demo``: 準備なしで、物流センターの在庫画面のデモを起動する。

一時フォルダにデモのプロジェクト(:func:`monitor_app.demo.write_project`)を書き、
通常の CSV 取り込み → ``create_app`` → 入出庫を発生させるタスクを足して起動する。
DB も一時フォルダの中(終了時に削除)で、作業フォルダには何も書かない。

作業フォルダの .env は読まない(プロジェクトの DB・通知先・バックアップの設定をデモに
持ち込まない)。環境変数の MONITOR_HOST / MONITOR_PORT / MONITOR_API_KEY などは使う
(Docker の既定の MONITOR_HOST=0.0.0.0 で、コンテナの外から開ける)。通知(メール・
Webhook)・バックアップ・フォルダ監視は、環境変数の設定にかかわらず使わない。
"""

from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import typer

from .. import logging_conf
from . import server as _server
from ._common import sqlite_url
from .data import run_csv_import

if TYPE_CHECKING:
    from ..settings.runtime import AppSettings

#: 一時フォルダの接頭辞。
TEMP_PREFIX = "monitor-app-demo-"
#: 起動時に件数を表示するテーブル(表示名)。
_TABLE_LABELS = {
    "items": "品目",
    "locations": "棚",
    "lots": "ロット",
    "movements": "入出庫",
}


def demo_settings(workdir: Path, host: str, port: int) -> AppSettings:
    """デモの実行時設定。環境変数は読むが .env は読まず、デモの外に影響するものは上書きする。"""
    from ..settings.runtime import AppSettings

    return AppSettings(
        _env_file=None,
        database_url=sqlite_url(workdir / "demo.db"),
        csv_dir=workdir / "csv",
        ingest_watch=False,
        webhook_url=None,
        smtp_host=None,
        alert_email_to=None,
        backup_dir=None,
        host=host,
        port=port,
    )


def demo_command(
    host: str | None = typer.Option(
        None, help="ホスト(既定: MONITOR_HOST または 127.0.0.1)"
    ),
    port: int | None = typer.Option(
        None, help="ポート(既定: MONITOR_PORT または 9990)"
    ),
    open_browser: bool = typer.Option(False, "--open", help="起動したらブラウザで開く"),
) -> None:
    """物流センターの在庫画面のデモを起動する(準備不要。データは架空で、終了すると消える)。"""
    from ..demo import SOURCE, WarehouseSimulator, write_project
    from ..main import create_app
    from ..settings.loader import load_config
    from ..settings.runtime import AppSettings

    env = AppSettings(_env_file=None)
    logging_conf.configure_logging(env.log_level, log_format=env.log_format)
    host = host or env.host
    port = port or env.port
    _server.check_port(port)
    _server.ensure_port_available(host, port)

    with _server.serving_session() as session:
        workdir = Path(
            session.enter_context(
                tempfile.TemporaryDirectory(
                    prefix=TEMP_PREFIX, ignore_cleanup_errors=True
                )
            )
        )
        write_project(workdir, now=datetime.now())
        # 一時フォルダのコピーを読む(__pycache__ をパッケージの中に作らない)。
        config = load_config(workdir / "config.py")
        settings = demo_settings(workdir, host, port)
        report = run_csv_import(settings, config, only_empty=True)
        app = create_app(config, settings)
        session.callback(app.state.services.close)
        simulator = WarehouseSimulator(app.state.services)
        app.state.services.background_tasks.append(simulator)

        counts = "・".join(
            f"{_TABLE_LABELS.get(r.table, r.table)} {r.inserted:,}"
            for r in report.results
        )
        typer.secho(
            "📦 デモ: 物流センターの在庫画面(データは架空。終了すると消えます)",
            bold=True,
        )
        typer.echo(f"  データ: {counts} 行(今日までの 7 日分)")
        typer.echo(
            f"  入出庫を {simulator.interval:g} 秒ごとに発生させます"
            "(画面は自動で更新)"
        )
        typer.echo(f"  この画面の設定: {SOURCE / 'config.py'}")
        typer.echo(
            "  自分のプロジェクトにする: "
            "monitor-app startproject my-warehouse --template warehouse"
        )
        session.serve(app, settings, host, port, open_browser=open_browser)
