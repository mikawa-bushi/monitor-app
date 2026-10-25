"""データベースの保守のコマンド: ``migrate``(列の追加)・``backup``・``restore``。

表定義のずれ(config.py と実際の DB)の点検は ``check`` と共有する
(:func:`inspect_configured_schema`)。``check`` は DB を書き換えないよう、SQLite の
ファイルを読み取り専用で開く。
"""

from __future__ import annotations

import os
import socket
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

import typer

from ._common import (
    CONFIG_HELP,
    StopRequested,
    check_relative_paths,
    fail,
    load_project,
    load_settings,
    open_database,
    resolve_config,
    stop_signals,
)

if TYPE_CHECKING:
    from sqlalchemy.engine import URL

    from ..checks import Issue
    from ..db.engine import Database
    from ..db.schema import SchemaProblem, SchemaReport
    from ..settings.declarative import MonitorConfig
    from ..settings.runtime import AppSettings

#: バックアップの保存先の既定(MONITOR_BACKUP_DIR が未設定のとき。作業フォルダ基準)。
DEFAULT_BACKUP_DIR = Path("backups")
#: 足りない列を見つけたときの案内。
MIGRATE_HINT = "monitor-app migrate で追加できます(check --fix-schema でも可)"
#: 稼働中のサーバーを探すときの接続の待ち時間(秒)。
_PROBE_TIMEOUT = 0.5
#: check が表定義を読むとき、サーバー DB へ接続する待ちの上限(秒)。
_SCHEMA_CONNECT_TIMEOUT = 3.0


# ---------------------------------------------------------------------------
# 表定義のずれ(check・migrate が共有)
# ---------------------------------------------------------------------------
def _is_memory(url: URL) -> bool:
    """メモリ上の SQLite の URL か(:func:`~monitor_app.db.engine.is_memory_url`)。"""
    from ..db.engine import is_memory_url  # SQLAlchemy は使う時点で読み込む

    return is_memory_url(url)


def _schema_url(settings: AppSettings) -> str | None:
    """表定義を読むために開く接続 URL。DB がまだ無い・メモリ上なら None。

    ファイルの SQLite は読み取り専用(``mode=ro``)で開く。点検で DB を作ったり、
    WAL に切り替えたり、保存先のフォルダを作ったりしない。
    """
    from sqlalchemy.engine import URL
    from sqlalchemy.exc import ArgumentError

    from ..db.engine import parse_url

    try:
        url = parse_url(settings.database_url)
    except ArgumentError:
        return None  # 形式の誤りは run_checks が database-url として報告する
    if url.get_backend_name() != "sqlite":
        return settings.database_url
    database = url.database or ""
    if _is_memory(url):
        return None
    if database.startswith("file:"):  # URI 形式は書かれたとおりに、読み取り専用で
        return url.update_query_dict({"mode": "ro"}).render_as_string(
            hide_password=False
        )
    path = Path(os.path.abspath(os.path.expanduser(database)))
    if not path.is_file():
        return None
    readonly = URL.create(
        "sqlite", database=path.as_uri(), query={"mode": "ro", "uri": "true"}
    )
    return readonly.render_as_string(hide_password=False)


def _unavailable_detail(exc: Exception) -> str:
    from sqlalchemy.exc import DBAPIError

    if isinstance(exc, DBAPIError):
        from ..db.errors import driver_message

        return driver_message(exc)
    if isinstance(exc, ModuleNotFoundError):
        return f"ドライバ {exc.name} がインストールされていません"
    return f"{type(exc).__name__}: {exc}"


def inspect_configured_schema(
    settings: AppSettings, cfg: MonitorConfig
) -> tuple[SchemaReport | None, Issue | None]:
    """config.py と、設定の DB の表定義を比べる(読み取りのみ)。

    戻り値は (ずれの報告, 点検できなかったときの警告)。DB がまだ無い(初回の起動・
    取り込みで作られる)・メモリ上なら (None, None)。
    """
    from ..checks import Issue
    from ..db.engine import Database
    from ..db.registry import TableRegistry
    from ..db.schema import inspect_schema

    url = _schema_url(settings)
    if url is None:
        return None, None
    db = None
    try:
        # 点検は短く待つ(応答の無いホストで check が何十秒も止まらない)
        db = Database(url, connect_timeout=_SCHEMA_CONNECT_TIMEOUT)
        return inspect_schema(db, TableRegistry(cfg)), None
    except Exception as exc:  # noqa: BLE001 - 点検できなくても他の点検は続ける
        return None, Issue(
            "warning",
            "schema-unavailable",
            "データベースの表定義を読めないため、config.py との照合を省きました:"
            f" {_unavailable_detail(exc)}",
            "MONITOR_DATABASE_URL の接続先とデータベースの状態を確認してください",
        )
    finally:
        if db is not None:
            db.dispose()


def schema_issues(report: SchemaReport) -> list[Issue]:
    """表定義のずれを ``check`` の問題(:class:`~monitor_app.checks.Issue`)にする。"""
    from ..checks import Issue
    from ..db.schema import MISSING_COLUMN

    return [
        Issue(
            "warning" if problem.level == "warning" else "info",
            f"schema-{problem.kind.replace('_', '-')}",
            problem.message,
            MIGRATE_HINT if problem.kind == MISSING_COLUMN else None,
        )
        for problem in report.problems
    ]


def _additive_statements(report: SchemaReport) -> list[str]:
    from ..db.schema import MISSING_COLUMN

    return [
        p.fix_sql
        for p in report.problems
        if p.kind == MISSING_COLUMN and p.fix_sql is not None
    ]


def apply_missing_columns(
    settings: AppSettings, cfg: MonitorConfig, report: SchemaReport
) -> list[str]:
    """``report`` の足りない列を、実行する SQL を表示してから追加する(実行した SQL を返す)。"""
    from ..db.schema import apply_additive_migrations

    statements = _additive_statements(report)
    if not statements:
        return []
    typer.echo(f"足りない列を追加します({len(statements)} 件):")
    for sql in statements:
        typer.echo(f"  {sql}")
    with open_database(settings, cfg) as (db, registry):
        applied = apply_additive_migrations(db, registry, report)
    typer.secho(f"✅ {len(applied)} 列を追加しました", fg=typer.colors.GREEN)
    return applied


def _print_problems(
    title: str, problems: list[SchemaProblem], color: str | None
) -> None:
    if not problems:
        return
    typer.secho(f"{title}({len(problems)} 件):", fg=color, bold=True)
    for problem in problems:
        typer.secho(f"  - {problem.message}", fg=color)


# ---------------------------------------------------------------------------
# migrate
# ---------------------------------------------------------------------------
def migrate(
    config: str = typer.Option("config.py", help=CONFIG_HELP),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="実行する SQL を表示するだけで、データベースは変えない"
    ),
) -> None:
    """config.py に足した列をデータベースの表に追加する(型の変更などは手順を表示)。

    作成済みの表は起動時に変わらないため、config.py に列を足したら実行する。
    列の追加(NULL を許す列)だけを行い、列の削除・型の変更・主キーの変更は
    行わない(必要な対応を表示する)。
    """
    from ..db.engine import describe_url, parse_url
    from ..db.schema import MISSING_COLUMN, apply_additive_migrations, inspect_schema

    _path, settings, cfg = load_project(config, csv=False)
    url = parse_url(settings.database_url)  # 形式の誤りは CLI のエラー層が短く表示する
    typer.echo(f"データベース: {describe_url(settings.database_url)}")
    if _schema_url(settings) is None:
        if _is_memory(url):
            typer.echo(
                "メモリ上のデータベースは移行の対象外です(起動のたびに config.py の"
                "とおりに作られます)"
            )
        else:
            typer.secho(
                "✅ データベースはまだありません。サーバーの起動時・CSV の取り込み時に"
                " config.py のとおりに作成されるため、移行は不要です",
                fg=typer.colors.GREEN,
            )
        return

    def show(statements: list[str]) -> None:
        if statements:
            typer.echo(f"追加する列({len(statements)} 件):")
            for sql in statements:
                typer.echo(f"  {sql}")

    if dry_run:
        # 読み取りだけ(SQLite は読み取り専用で開き、WAL への切り替えもしない)
        report, problem = inspect_configured_schema(settings, cfg)
        if report is None:
            fail(problem.message if problem else "表定義を読めません")
        statements = _additive_statements(report)
        show(statements)
    else:
        with open_database(settings, cfg) as (db, registry):
            # PostgreSQL で表の最大値より遅れた id の連番(以前の版の取り込み)も進める
            # (起動時・取り込み時と同じ処理。遅れていなければ何もしない — #D5-01)
            for name, (behind, top) in registry.repair_sequences(db).items():
                typer.echo(
                    f"テーブル '{name}' の連番を表の最大値に合わせました({behind} → {top})"
                )
            report = inspect_schema(db, registry)
            statements = _additive_statements(report)
            show(statements)  # 実行する SQL を表示してから追加する
            if statements:
                apply_additive_migrations(db, registry, report)

    manual = [
        p for p in report.problems if p.level == "warning" and p.kind != MISSING_COLUMN
    ]
    _print_problems("手動での対応が必要なもの", manual, typer.colors.YELLOW)
    _print_problems("情報", [p for p in report.problems if p.level == "info"], None)
    if statements and dry_run:
        typer.secho(
            "--dry-run のため変更していません(追加するには --dry-run を外して実行)",
            fg=typer.colors.YELLOW,
        )
    elif statements:
        typer.secho(f"✅ {len(statements)} 列を追加しました", fg=typer.colors.GREEN)
    elif not manual:
        typer.secho(
            "✅ データベースの表定義は config.py と一致しています",
            fg=typer.colors.GREEN,
        )
    else:
        typer.echo("追加する列はありません")


# ---------------------------------------------------------------------------
# backup / restore
# ---------------------------------------------------------------------------
def _sqlite_file(settings: AppSettings, action: str) -> Path:
    """バックアップ・復元の対象の SQLite ファイル。SQLite のファイル以外は案内して終える。"""
    from ..db.engine import parse_url

    url = parse_url(settings.database_url)  # 形式の誤りは CLI のエラー層が短く表示する
    backend = url.get_backend_name()
    if backend != "sqlite":
        fail(
            f"{action}は SQLite のデータベースだけに対応しています"
            f"(今のデータベース: {backend})",
            "PostgreSQL は pg_dump / pg_restore、MySQL は mysqldump / mysql コマンドを"
            "使ってください",
        )
    database = url.database or ""
    if _is_memory(url):
        fail(f"メモリ上の SQLite データベースは{action}できません(終了すると消えます)")
    if database.startswith("file:"):
        fail(
            f"file: 形式の MONITOR_DATABASE_URL の{action}には対応していません",
            "sqlite:///<ファイルのパス> の形で指定してください",
        )
    return Path(os.path.abspath(os.path.expanduser(database)))


def _size_label(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB"):
        if value < 1024:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def backup(
    config: str = typer.Option("config.py", help=CONFIG_HELP),
    dest: Path | None = typer.Option(
        None,
        "--dest",
        metavar="DIR",
        help="保存先のフォルダ(既定: MONITOR_BACKUP_DIR、未設定なら backups)",
        show_default=False,
    ),
    keep: int | None = typer.Option(
        None,
        "--keep",
        metavar="N",
        help="新しい順に残す世代数(既定: MONITOR_BACKUP_KEEP。0 ですべて残す)",
        show_default=False,
    ),
) -> None:
    """SQLite のデータベースをバックアップする(サーバーの稼働中でもよい)。

    書き込み中のデータも含めて一貫したコピーを 1 ファイル
    (<DB 名>-YYYYmmdd-HHMMSS.db)に作り、古いバックアップは --keep の世代だけ残す。
    """
    import sqlite3

    from ..db.backup import (
        backup_database,
        backup_owner,
        is_corrupt_error,
        list_backups,
    )
    from ..db.engine import Database
    from ..exceptions import BackupError

    path = resolve_config(config)
    settings = load_settings(path)
    # config.py のフォルダにしか無い DB を、作業フォルダを基準に探さない(止める)
    check_relative_paths(path, settings)
    db_file = _sqlite_file(settings, "バックアップ")
    if not db_file.is_file():
        fail(
            f"データベースファイルがありません: {db_file}",
            "サーバーの起動・CSV の取り込みで作成されます。別の場所の DB なら、その"
            "プロジェクトのフォルダで実行してください",
        )
    folder = dest if dest is not None else settings.backup_dir or DEFAULT_BACKUP_DIR
    generations = settings.backup_keep if keep is None else keep
    db = Database.from_settings(settings)
    try:
        # docker stop・systemctl stop でも書きかけの一時ファイルを消して終える
        with stop_signals():
            target = backup_database(db, folder, keep=generations)
    except StopRequested:
        _stopped("バックアップ")
    except BackupError as exc:
        fail(exc.message, "少し待ってから、もう一度実行してください")
    except sqlite3.DatabaseError as exc:  # 壊れて読めない DB など
        fail(
            f"データベースを読めないため、バックアップできません: {db_file}({exc})",
            (
                "壊れている場合は monitor-app restore <バックアップファイル> --yes で"
                "復元できます"
                if is_corrupt_error(exc)
                else None
            ),
        )
    except OSError as exc:  # 保存先を作れない・書けない
        fail(
            f"バックアップを書き込めません: {Path(folder).expanduser()}"
            f"({exc.strerror or exc})",
            "書き込みできるフォルダを --dest か MONITOR_BACKUP_DIR に指定してください",
        )
    finally:
        db.dispose()
    target = target.resolve()
    # 共有のフォルダでは、このデータベースのバックアップだけを数える(#D3-16)
    count = len(list_backups(target.parent, db_file.stem, owner=backup_owner(target)))
    policy = (
        f"新しい {generations} 件を残します" if generations > 0 else "すべて残します"
    )
    typer.secho(
        f"✅ バックアップを作成しました: {target}({_size_label(target.stat().st_size)})",
        fg=typer.colors.GREEN,
    )
    typer.echo(f"  {target.parent} のバックアップ: {count} 件({policy})")


def _server_seems_running(settings: AppSettings) -> bool:
    """設定のホスト・ポートで待ち受けているプロセスがあるか(接続できるか)。"""
    host = settings.host.strip("[]")
    target = {"": "127.0.0.1", "0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)
    try:
        with socket.create_connection((target, settings.port), timeout=_PROBE_TIMEOUT):
            return True
    except OSError:
        return False


def restore(
    file: Path = typer.Argument(
        ...,
        metavar="FILE",
        help="復元するバックアップファイル(monitor-app backup で作ったもの)",
        show_default=False,
    ),
    config: str = typer.Option("config.py", help=CONFIG_HELP),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="復元を実行する(付けなければ内容を表示して止まる。サーバーは先に停止する)",
    ),
) -> None:
    """バックアップからデータベースを復元する(サーバーを停止してから実行)。

    今のデータベースの内容はバックアップの内容に置き換わる。置き換える前に、今の
    データベースをバックアップの保存先へ退避する。
    """
    from ..db.backup import validate_backup
    from ..db.engine import Database
    from ..exceptions import BackupError

    path = resolve_config(config)
    settings = load_settings(path)
    # config.py のフォルダにしか無い DB を、作業フォルダを基準に探さない(止める)
    check_relative_paths(path, settings)
    db_file = _sqlite_file(settings, "復元")
    try:
        source = validate_backup(file).resolve()
    except BackupError as exc:
        fail(exc.message)
    if db_file.is_file() and source.samefile(db_file):
        fail("復元元と復元先が同じファイルです")
    if not yes:
        typer.echo(
            f"復元すると、データベース {db_file} の内容が {source} の内容に"
            "置き換わります(今のデータは失われます)"
        )
        fail(
            "確認のため --yes を付けて実行してください",
            "サーバー(runserver)を停止してから実行してください"
            "(今のデータベースは置き換える前に退避します)",
        )
    if _server_seems_running(settings):
        typer.secho(
            f"⚠️  {settings.host}:{settings.port} で動いているサーバーがあるようです。"
            "動作中に復元すると、復元中の書き込みが失われ、画面に古い内容が残ることが"
            "あります(停止してから復元してください)",
            fg=typer.colors.YELLOW,
            err=True,
        )
    folder = settings.backup_dir or DEFAULT_BACKUP_DIR
    db = Database.from_settings(settings)
    try:
        with stop_signals():
            _save_and_restore(db, db_file, folder, source)
    except StopRequested:
        _stopped("復元")
    except BackupError as exc:
        fail(exc.message, "サーバー(runserver)を停止してから、もう一度実行してください")
    finally:
        db.dispose()
    typer.secho(f"✅ 復元しました: {source} → {db_file}", fg=typer.colors.GREEN)
    typer.echo("  サーバーを起動してください: monitor-app runserver")


def _stopped(action: str) -> NoReturn:
    """停止の指示(SIGTERM など)で中止したことを示して終える(終了コード 143)。"""
    typer.secho(f"停止の指示(SIGTERM)を受けたため、{action}を中止しました", err=True)
    raise typer.Exit(143)


def _save_and_restore(db: Database, db_file: Path, folder: Path, source: Path) -> None:
    """今の DB を退避してから(できなければ警告して続ける)、``source`` で置き換える。

    今の DB が壊れていてバックアップ API で読めないときは、壊れたファイルをそのまま
    (-wal も)退避する(修復ツールで行を取り出せることがある)。読み書きの接続で開くと
    -wal が消えることがあるため、先に読み取り専用で確かめてから写す。復元はそのまま行う。
    """
    import sqlite3

    from ..db.backup import (
        backup_database,
        is_corrupt_error,
        is_damaged,
        restore_database,
        save_damaged_copy,
    )
    from ..exceptions import BackupError

    def save_raw() -> str | None:
        try:
            return str(save_damaged_copy(db_file, folder).resolve())
        except OSError:
            return None

    if db_file.is_file() and is_damaged(db_file):
        raw = save_raw()
        typer.secho(
            "⚠️  今のデータベースは壊れていて開けません。"
            + (
                f"ファイルをそのまま退避しました: {raw}"
                if raw
                else "退避できなかったため、そのまま復元します"
            ),
            fg=typer.colors.YELLOW,
            err=True,
        )
    elif db_file.is_file():
        try:
            saved = backup_database(db, folder, keep=0)
            typer.echo(f"  今のデータベースを退避しました: {saved.resolve()}")
        except (BackupError, sqlite3.Error, OSError) as exc:
            raw = save_raw() if is_corrupt_error(exc) else None
            outcome = (
                f"壊れたファイルをそのまま退避しました: {raw}"
                if raw
                else "そのまま復元します"
            )
            typer.secho(
                f"⚠️  今のデータベースを退避できませんでした({exc})。{outcome}",
                fg=typer.colors.YELLOW,
                err=True,
            )
    restore_database(source, db)
