"""データ系コマンド: ``check``(設定の点検)・``import-csv``・``sync-sources``。"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import typer

from ..settings.loader import ConfigError
from ._common import (
    CONFIG_HELP,
    fail,
    load_project,
    load_settings,
    open_database,
    plan_csv_import,
    resolve_config,
    warn_relative_paths,
)

if TYPE_CHECKING:
    from ..checks import Issue
    from ..services.importer import ImportReport, TableImportResult
    from ..settings.declarative import MonitorConfig
    from ..settings.runtime import AppSettings

logger = logging.getLogger("monitor_app.cli")

#: 取り込み結果に並べる列名の最大数(テーブルごと)。
_MAX_COLUMNS = 12


def run_csv_import(
    settings: AppSettings,
    config: MonitorConfig,
    *,
    keep: bool = False,
    only_empty: bool = False,
    force: bool = False,
) -> ImportReport:
    """csv/ フォルダの CSV を DB へ取り込む(``runserver --import-csv`` と共有)。

    ``only_empty=True`` なら、行が 1 件も無いテーブルにだけ取り込む(初回起動用)。
    画面・API・フォームから入力した行がある運用中のテーブルには手を付けないため、
    再起動のたびに CSV で上書きして入力データを失うことがない。``keep=True`` は追記
    (主キーが既にある行は読み飛ばすため、再実行しても重複しない)。``force=True`` は
    宣言した列がヘッダに無い CSV でも置換する。
    """
    from ..services.importer import CsvImporter

    with open_database(settings, config) as (db, registry):
        importer = CsvImporter(
            config,
            registry,
            db,
            settings.csv_dir,
            encoding=settings.csv_encoding,
            encoding_fallback=settings.csv_encoding_fallback,
        )
        if only_empty:
            return importer.import_all(mode="if-empty", force=force)
        report = importer.import_all(keep=keep, force=force)
        if not keep:
            # 置換はフォーム・画面・API から入力した行も消す。フォームのあるテーブルを
            # 置き換えたら、次から残す方法とあわせて知らせる(DX-01)。
            replaced = [
                r.table
                for r in report.results
                if config.tables[r.table].form is not None
                and r.inserted > 0
                and not r.refused
            ]
            if replaced:
                logger.warning(
                    "入力フォームのある %s を CSV の内容で置き換えました。フォーム・画面・"
                    "API から入力した行は残りません(残すには runserver"
                    " --import-csv-if-empty か import-csv --keep を使ってください)",
                    "、".join(replaced),
                )
        return report


#: check の重要度ごとの見出しと色。
_LEVELS = (
    ("error", "エラー", typer.colors.RED),
    ("warning", "警告", typer.colors.YELLOW),
    ("info", "情報", None),
)


def _print_issues(issues: list[Issue]) -> None:
    """問題を重要度ごとにまとめて表示する(見出し・本文・ヒント)。"""
    for level, label, color in _LEVELS:
        group = [issue for issue in issues if issue.level == level]
        if not group:
            continue
        typer.secho(f"{label}({len(group)} 件):", fg=color, bold=True)
        for issue in group:
            typer.secho(f"  - {issue.message}", fg=color)
            if issue.hint:
                typer.echo(f"    ヒント: {issue.hint}")


def check(
    config: str = typer.Option("config.py", help=CONFIG_HELP),
    data: bool = typer.Option(
        False, "--data", help="CSV の中身(見出し・型・文字コード)も点検する"
    ),
    strict: bool = typer.Option(
        False, "--strict", help="警告があっても終了コード 1 にする(CI 向け)"
    ),
    fix_schema: bool = typer.Option(
        False,
        "--fix-schema",
        help="データベースの表に足りない列を追加してから点検する(migrate と同じ)",
    ),
) -> None:
    """config.py・SQL・実行時設定・DB の表定義を点検する(起動前の lint)。

    エラーがあれば終了コード 1(--strict なら警告でも 1)。
    """
    from ..checks import check_project, has_errors, sort_issues, summarize
    from ..settings.runtime import project_env_files
    from .maintenance import (
        apply_missing_columns,
        inspect_configured_schema,
        schema_issues,
    )

    path = resolve_config(config)
    settings = load_settings(path)
    try:
        cfg, issues = check_project(
            path, settings, data=data, env_files=project_env_files(path)
        )
    except ConfigError as exc:
        fail(str(exc))
    warn_relative_paths(path, settings, cfg)

    report, unavailable = inspect_configured_schema(settings, cfg)
    if fix_schema and report is not None and report.missing_columns():
        if has_errors(issues):
            # 誤りのある設定に合わせて DB を変えない
            typer.secho(
                "エラーがあるため、足りない列の追加(--fix-schema)は行いません"
                "(エラーを直してから実行してください)",
                fg=typer.colors.YELLOW,
            )
        else:
            apply_missing_columns(settings, cfg, report)
            report, unavailable = inspect_configured_schema(settings, cfg)
    if report is not None:
        issues += schema_issues(report)
    if unavailable is not None:
        issues.append(unavailable)
    issues = sort_issues(issues)

    _print_issues(issues)
    errors = sum(issue.level == "error" for issue in issues)
    warnings = sum(issue.level == "warning" for issue in issues)
    if errors:
        fail(f"❌ 問題があります({summarize(issues)})")
    if strict and warnings:
        fail(f"❌ --strict のため、警告があると失敗にします({summarize(issues)})")
    counts = f"({summarize(issues)})" if issues else ""
    typer.secho(
        f"✅ OK — テーブル {len(cfg.tables)} 件 / ビュー {len(cfg.views)} 件{counts}",
        fg=typer.colors.GREEN,
    )


# ---------------------------------------------------------------------------
# import-csv
# ---------------------------------------------------------------------------
def _print_project_summary(
    path: Path, settings: AppSettings, cfg: MonitorConfig
) -> None:
    """取り込みの前の要約: 設定ファイル・データベース・どの CSV をどのテーブルへ入れるか。

    どのテーブルの CSV も無ければ、期待するファイル名を添えて警告する。
    """
    from ..db.engine import describe_url

    plan = plan_csv_import(settings, cfg)
    typer.echo(f"設定: {os.path.abspath(path)}")
    typer.echo(f"データベース: {describe_url(settings.database_url)}")
    typer.echo(f"CSV: {plan.describe()}")
    warning = plan.warning()
    if warning:
        typer.secho(f"⚠️  {warning}", fg=typer.colors.YELLOW)


class _SummaryOwnsTheMessages(logging.Filter):
    """取り込み結果の一覧に載る取り込みのログを出さない(D5-06)。

    ``import-csv`` は結果の一覧に同じ内容(文字コードの判別・列の対応・読み飛ばした行・
    置換の中止など)を表示するため、ログにも出すと 2 回ずつ表示される。取り込みが
    結果にも入れた内容のログ(印 ``monitor_in_result``)だけを止める。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return not getattr(record, "monitor_in_result", False)


@contextmanager
def _summary_owns_importer_logs() -> Iterator[None]:
    importer_logger = logging.getLogger("monitor_app.importer")
    quiet = _SummaryOwnsTheMessages()
    importer_logger.addFilter(quiet)
    try:
        yield
    finally:
        importer_logger.removeFilter(quiet)


def _visible(name: str) -> str:
    """見えない文字(BOM・制御文字)を \\uXXXX で表した列名(``check`` の表示と同じ)。"""
    return "".join(ch if ch.isprintable() else f"\\u{ord(ch):04x}" for ch in name)


def _columns(names: list[str]) -> str:
    return ", ".join(names[:_MAX_COLUMNS]) + (
        f" ほか {len(names) - _MAX_COLUMNS} 列" if len(names) > _MAX_COLUMNS else ""
    )


def _print_result(result: TableImportResult, cfg: MonitorConfig) -> None:
    """1 テーブルの取り込み結果(行数・列の対応・警告)。

    追記で主キーが既にあった行は ``--keep`` の目的どおりの動作のため、警告(⚠️)では
    なく、行数の欄の「既存 N 行」と説明の行で示す(D5-06)。
    """
    from ..services.importer import duplicates_message

    refused = " — 置換を中止(既存の行を残しました)" if result.refused else ""
    existing = f"、既存 {result.duplicates} 行" if result.duplicates else ""
    typer.echo(
        f"  {result.table}: {result.inserted} 行 (skip {result.skipped}{existing})"
        f"{refused}"
    )
    expected = (
        duplicates_message(f"{result.table}.csv", result.duplicates)
        if result.duplicates
        else None
    )
    if result.matched:
        typer.echo(f"    一致した列: {_columns(result.matched)}")
    if result.renamed:
        pairs = [
            f"'{_visible(cell)}' → {column}" for cell, column in result.renamed.items()
        ]
        typer.echo(f"    表記ゆれを対応付けた列: {_columns(pairs)}")
    if result.missing:
        tdef = cfg.tables[result.table]
        auto = "・自動採番" if tdef.column_type(tdef.primary_key) == "int" else ""
        missing = [
            f"{c}(主キー{auto})" if c == tdef.primary_key else c for c in result.missing
        ]
        typer.echo(f"    CSV に無い列: {_columns(missing)}")
    if result.unknown:
        unknown = [_visible(cell) for cell in result.unknown]
        typer.echo(f"    取り込まない CSV の列: {_columns(unknown)}")
    for warning in result.warnings:
        if warning == expected:
            typer.echo(f"    {warning}")
            continue
        typer.secho(f"    ⚠️  {warning}", fg=typer.colors.YELLOW)


def import_csv(
    config: str = typer.Option("config.py", help=CONFIG_HELP),
    keep: bool = typer.Option(False, "--keep", help="既存行を消さずに追記する"),
    if_empty: bool = typer.Option(
        False,
        "--if-empty",
        help=(
            "行が 1 件も無いテーブルにだけ取り込む"
            "(runserver --import-csv-if-empty と同じ。入力済みの行を残す)"
        ),
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="config.py の列が CSV の見出しに無くても置き換える(無い列は空になる)",
    ),
) -> None:
    """csv/ フォルダの CSV をデータベースへ取り込む。

    既定は置き換え(テーブルの行を CSV の内容にする)。CSV の見出しに config.py の
    列が無いテーブルは置き換えを中止して既存の行を残し、終了コード 1 にする。
    """
    if keep and if_empty:
        fail(
            "--keep と --if-empty は同時に指定できません",
            "追記するなら --keep、行の無いテーブルにだけ取り込むなら --if-empty を"
            "指定してください",
        )
    path, settings, cfg = load_project(config)
    _print_project_summary(path, settings, cfg)
    with _summary_owns_importer_logs():
        report = run_csv_import(
            settings, cfg, keep=keep, only_empty=if_empty, force=force
        )

    for r in report.results:
        _print_result(r, cfg)
    for table in report.skipped_tables:
        typer.echo(f"  {table}: 行があるため取り込みませんでした(--if-empty)")
    for unmatched in report.unmatched_files:
        typer.secho(f"  ⚠️  {unmatched.message}", fg=typer.colors.YELLOW)

    refused = [r.table for r in report.results if r.refused]
    if refused:
        fail(
            f"❌ 置換を中止したテーブルがあります: {', '.join(refused)}"
            f"(既存の行を残しました。ほかのテーブルは取り込み済み: 合計"
            f" {report.total_inserted} 行)",
            "CSV の見出しを config.py の columns に合わせるか、--force で置き換えて"
            "ください(CSV に無い列は空になります)",
        )
    duplicates = (
        f"(主キーが既にある {report.total_duplicates} 行は読み飛ばし)"
        if report.total_duplicates
        else ""
    )
    typer.secho(
        f"✅ 取り込み完了: 合計 {report.total_inserted} 行{duplicates}",
        fg=typer.colors.GREEN,
    )


def sync_sources(
    config: str = typer.Option("config.py", help=CONFIG_HELP),
) -> None:
    """全ソースを 1 回同期する(sources が定義されていない場合は何もしない)。"""
    # CSV のフォルダは使わない(config.py のフォルダとのずれは警告だけ)
    _path, settings, cfg = load_project(config, csv=False)
    if not cfg.sources:
        typer.echo("sources が定義されていません")
        return

    from ..container import build_source_connectors

    total = 0
    with open_database(settings, cfg) as (db, registry):
        registry.create_all(db)
        connectors = build_source_connectors(cfg, registry, db) or {}
        for name, connector in connectors.items():
            try:
                n = connector.poll_once()
                typer.echo(f"  {name}: {n} 行")
                total += n
            except Exception as exc:  # noqa: BLE001 - 1 ソースの失敗で他を止めない
                typer.secho(f"  {name}: エラー — {exc}", fg=typer.colors.RED)

    typer.secho(f"✅ 同期完了: 合計 {total} 行", fg=typer.colors.GREEN)
