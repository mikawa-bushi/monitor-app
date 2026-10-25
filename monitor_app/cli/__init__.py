"""Typer ベースの CLI。

v1 と異なり ``startproject`` はアプリ本体をコピーしない。生成するのは
``config.py`` とサンプル CSV だけで、本体はパッケージから参照する。
これにより ``pip install -U monitor-app`` だけで UI・API が更新される。

コマンドは役割ごとのモジュールに置き、ここで登録する: ``project``(startproject)・
``zero``(serve: 設定ファイルなしの起動)・``server``(runserver)・
``data``(check / import-csv / sync-sources)・``maintenance``(migrate / backup /
restore: データベースの保守)・``deploy``(deploy: 配備用ファイルの生成)。
``--help`` の一覧はここで登録した順(始め方 → 起動 → 点検・取り込み → 保守 → 配備)。
設定の読み込みなどの共通処理は ``_common``、既知のエラーの短い表示とコマンド群
(:class:`~monitor_app.cli._errors.MonitorGroup`)は ``_errors``、停止のシグナルの扱いは
``_signals`` にまとめる。
"""

from __future__ import annotations

import typer

from .. import __version__
from ._errors import MonitorGroup
from .data import check, import_csv, sync_sources
from .deploy import deploy_command
from .maintenance import backup, migrate, restore
from .project import startproject
from .server import runserver
from .zero import serve

app = typer.Typer(
    cls=MonitorGroup,
    add_completion=False,
    help="monitor-app コマンドラインツール",
    # 例外表示にローカル変数を出さない(API キーや DB パスワードが漏れるため)。
    pretty_exceptions_show_locals=False,
)


def _print_version(value: bool) -> None:
    if value:
        typer.echo(f"monitor-app {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        callback=_print_version,
        is_eager=True,
        help="バージョンを表示して終了する",
    ),
    debug: bool = typer.Option(
        False,
        "--debug",
        help="エラー時に詳細なトレースバックを表示する(MONITOR_DEBUG=1 と同じ)",
    ),
) -> None:
    # 共通オプションの受け口。--debug の値は MonitorGroup がエラー表示の
    # 切り替えに使う(コンテキストの params 経由)。
    pass


app.command()(startproject)
app.command()(serve)
app.command()(runserver)
app.command()(check)
app.command("import-csv")(import_csv)
app.command("sync-sources")(sync_sources)
app.command()(migrate)
app.command()(backup)
app.command()(restore)
app.command("deploy")(deploy_command)

__all__ = ["app"]
