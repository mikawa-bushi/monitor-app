"""Typer ベースの CLI。

v1 と異なり ``startproject`` はアプリ本体をコピーしない。生成するのは
``config.py`` とサンプル CSV だけで、本体はパッケージから参照する。
これにより ``pip install -U monitor-app`` だけで UI・API が更新される。
"""

from __future__ import annotations

import importlib.resources as resources
import os
import shutil
from pathlib import Path

import typer

from .logging_conf import configure_logging
from .settings.loader import ConfigError, load_config
from .settings.runtime import AppSettings

app = typer.Typer(add_completion=False, help="monitor-app コマンドラインツール")


def _scaffold_dir() -> Path:
    return Path(str(resources.files("monitor_app") / "scaffold"))


def _resolve_config(config: str) -> Path:
    path = Path(config)
    if not path.exists():
        typer.secho(f"config.py が見つかりません: {path}", fg=typer.colors.RED)
        raise typer.Exit(1)
    return path


@app.command()
def startproject(name: str = typer.Argument(..., help="作成するプロジェクト名")):
    """新規プロジェクトの雛形(config.py・サンプル CSV)を生成する。"""
    dest = Path(name).resolve()
    if dest.exists():
        typer.secho(f"⚠️  '{dest}' は既に存在します", fg=typer.colors.RED)
        raise typer.Exit(1)

    scaffold = _scaffold_dir()
    (dest / "csv").mkdir(parents=True)
    (dest / "instances").mkdir()
    (dest / "instances" / ".gitkeep").touch()

    shutil.copy(scaffold / "config.py", dest / "config.py")
    shutil.copy(scaffold / "env.example", dest / ".env.example")
    shutil.copy(scaffold / "README.md", dest / "README.md")
    for csv_file in (scaffold / "csv").glob("*.csv"):
        shutil.copy(csv_file, dest / "csv" / csv_file.name)

    typer.secho(f"✅ プロジェクトを作成しました: {dest}", fg=typer.colors.GREEN)
    typer.echo("次のステップ:")
    typer.echo(f"  cd {name}")
    typer.echo("  monitor-app runserver --import-csv")


@app.command()
def check(config: str = typer.Option("config.py", help="設定ファイルのパス")):
    """config.py を読み込んで検証だけ行う(起動前の lint)。"""
    path = _resolve_config(config)
    try:
        cfg = load_config(path)
    except ConfigError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(1)
    typer.secho(
        f"✅ OK — テーブル {len(cfg.tables)} 件 / ビュー {len(cfg.views)} 件",
        fg=typer.colors.GREEN,
    )


@app.command("import-csv")
def import_csv(
    config: str = typer.Option("config.py", help="設定ファイルのパス"),
    keep: bool = typer.Option(False, "--keep", help="既存行を消さずに追記する"),
):
    """csv/ フォルダの CSV をデータベースへ取り込む。"""
    path = _resolve_config(config)
    settings = AppSettings()
    configure_logging(settings.log_level)
    try:
        cfg = load_config(path)
    except ConfigError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(1)

    from .db.engine import Database
    from .db.registry import TableRegistry
    from .services.importer import CsvImporter

    db = Database(settings.database_url)
    registry = TableRegistry(cfg)
    importer = CsvImporter(
        cfg, registry, db, settings.csv_dir, encoding=settings.csv_encoding
    )
    report = importer.import_all(keep=keep)

    for r in report.results:
        typer.echo(f"  {r.table}: {r.inserted} 行 (skip {r.skipped})")
        for w in r.warnings:
            typer.secho(f"    ⚠️  {w}", fg=typer.colors.YELLOW)
    typer.secho(
        f"✅ 取り込み完了: 合計 {report.total_inserted} 行", fg=typer.colors.GREEN
    )


@app.command("sync-sources")
def sync_sources(
    config: str = typer.Option("config.py", help="設定ファイルのパス"),
):
    """全ソースを 1 回同期する(sources が定義されていない場合は何もしない)。"""
    path = _resolve_config(config)
    settings = AppSettings()
    configure_logging(settings.log_level)
    try:
        cfg = load_config(path)
    except ConfigError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(1)

    if not cfg.sources:
        typer.echo("sources が定義されていません")
        return

    from .db.engine import Database
    from .db.ingest_state import IngestStateStore
    from .db.registry import TableRegistry
    from .services.connectors import build_connectors

    db = Database(settings.database_url)
    registry = TableRegistry(cfg)
    registry.create_all(db)
    state = IngestStateStore(db)
    state.create()
    connectors = build_connectors(cfg, registry, db, state)

    total = 0
    for name, connector in connectors.items():
        try:
            n = connector.poll_once()
            typer.echo(f"  {name}: {n} 行")
            total += n
        except Exception as exc:  # noqa: BLE001
            typer.secho(f"  {name}: エラー — {exc}", fg=typer.colors.RED)

    typer.secho(f"✅ 同期完了: 合計 {total} 行", fg=typer.colors.GREEN)


@app.command()
def runserver(
    config: str = typer.Option("config.py", help="設定ファイルのパス"),
    host: str | None = typer.Option(
        None, help="ホスト(既定: MONITOR_HOST または 127.0.0.1)"
    ),
    port: int | None = typer.Option(
        None, help="ポート(既定: MONITOR_PORT または 9990)"
    ),
    reload: bool = typer.Option(False, "--reload", help="コード変更時に自動リロード"),
    import_csv: bool = typer.Option(
        False, "--import-csv", help="起動前に CSV を取り込む"
    ),
):
    """Web サーバー(uvicorn)を起動する。"""
    import uvicorn

    path = _resolve_config(config)
    settings = AppSettings()
    configure_logging(settings.log_level)

    if import_csv:
        from .db.engine import Database
        from .db.registry import TableRegistry
        from .services.importer import CsvImporter

        cfg = load_config(path)
        db = Database(settings.database_url)
        CsvImporter(
            cfg,
            TableRegistry(cfg),
            db,
            settings.csv_dir,
            encoding=settings.csv_encoding,
        ).import_all()

    # app_factory が読む環境変数に設定パスを渡す。
    os.environ["MONITOR_CONFIG_PATH"] = str(path)
    uvicorn.run(
        "monitor_app.main:app_factory",
        factory=True,
        host=host or settings.host,
        port=port or settings.port,
        reload=reload,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    app()
