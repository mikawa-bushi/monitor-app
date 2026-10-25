"""CLI のエラーの短い表示と、コマンド群(:class:`MonitorGroup`)。

- 既知の例外(設定の誤り・文字コード・使用中のポート・DB の接続や権限など)を
  1〜3 行のメッセージとヒントに変換する :func:`explain_error`(``--debug`` /
  ``MONITOR_DEBUG=1`` でトレースバック表示に戻せる)
- UTF-8 以外の出力先でも表示で落ちないようにする :func:`make_streams_safe`
- 1 回で終わるコマンドの実行中は停止のシグナルで中断できる :class:`MonitorGroup`

:mod:`monitor_app.cli._common` からも同じ名前で import できる。SQLAlchemy などの重い
モジュールは例外が起きたときだけ import する(CLI の起動を軽く保つ)。
"""

from __future__ import annotations

import codecs
import contextlib
import errno
import os
import sys
from pathlib import Path
from typing import Any

import typer
from typer.core import TyperGroup

from ..settings.loader import ConfigError
from ..settings.runtime import settings_error_message
from ._signals import StopRequested, stop_signals

__all__ = [
    "DEBUG_HINT",
    "SELF_MANAGED_SIGNAL_COMMANDS",
    "MonitorGroup",
    "debug_enabled",
    "explain_error",
    "is_address_in_use",
    "make_streams_safe",
    "remember_sqlite_path",
]


#: 既知のエラーを短く表示したときに添える案内。
DEBUG_HINT = (
    "(トレースバックを表示するには MONITOR_DEBUG=1 を設定するか、"
    "monitor-app --debug <コマンド> で再実行してください)"
)


#: 「アドレス使用中」を表す errno(Windows の WSAEADDRINUSE を含む)。
_ADDR_IN_USE_ERRNOS = frozenset(
    {errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", errno.EADDRINUSE)}
)


def _echo_error(message: str, hint: str | None = None) -> None:
    """エラーを赤字(ヒントは黄字)で標準エラーへ表示する。"""
    typer.secho(message, fg=typer.colors.RED, err=True)
    if hint:
        typer.secho(f"ヒント: {hint}", fg=typer.colors.YELLOW, err=True)


def _is_utf8(encoding: str) -> bool:
    # services.csv_encoding.is_utf8 と同じ判定。CLI の起動(--help など)で
    # services パッケージ(SQLAlchemy を読み込む)を import しないよう、ここに置く。
    try:
        return codecs.lookup(encoding).name in ("utf-8", "utf-8-sig")
    except LookupError:
        return False


def make_streams_safe(*streams: Any) -> None:
    """UTF-8 以外の出力先でも、表せない文字を ``\\uXXXX`` として書けるようにする。

    リダイレクトやパイプ先の標準出力は、Windows では cp932 などのロケール文字コードかつ
    ``errors="strict"`` になる。そのままだと処理が成功しても ✅ などの出力で
    UnicodeEncodeError になり、終了コード 1 で終わってしまう。引数を省略すると
    ``sys.stdout`` と ``sys.stderr`` を対象にする。
    """
    for stream in streams or (sys.stdout, sys.stderr):
        encoding = getattr(stream, "encoding", None)
        reconfigure = getattr(stream, "reconfigure", None)
        if not encoding or reconfigure is None or _is_utf8(encoding):
            continue
        # 出力設定を変えられなくても CLI は続ける
        with contextlib.suppress(Exception):
            reconfigure(errors="backslashreplace")


def is_address_in_use(exc: OSError) -> bool:
    """待ち受けアドレス(ポート)が使用中であることを表す OSError か。"""
    return exc.errno in _ADDR_IN_USE_ERRNOS


def debug_enabled(ctx: typer.Context | None = None) -> bool:
    """``--debug`` または環境変数 ``MONITOR_DEBUG`` でトレースバック表示が有効か。"""
    if ctx is not None and ctx.find_root().params.get("debug"):
        return True
    value = os.environ.get("MONITOR_DEBUG", "").strip().lower()
    return value not in ("", "0", "false", "no", "off")


def _db_detail(exc: Exception) -> str:
    """SQLAlchemy の例外から、DB ドライバのエラー文の 1 行目を取り出す。

    pg8000 はサーバーの応答を dict のまま持つため、そのメッセージ部分を使う。
    """
    from ..db.errors import driver_message

    return driver_message(exc)


#: SQLite のエラーに添える、DB ファイルの場所(:func:`remember_sqlite_path`)。
_SQLITE_PATH_ATTR = "_monitor_sqlite_path"


def remember_sqlite_path(exc: BaseException, path: Path | None) -> None:
    """DB のエラーに SQLite のファイルの場所を添える(エラーの案内で使う)。"""
    if path is not None and getattr(exc, _SQLITE_PATH_ATTR, None) is None:
        with contextlib.suppress(AttributeError):  # 属性を持てない例外は諦める
            setattr(exc, _SQLITE_PATH_ATTR, path)


#: SQLite の DB ファイル・フォルダに書き込めないときの案内の後半。
_SQLITE_PERMISSION_HINT = (
    "所有者と書き込みの権限を確認してください(Docker ではマウントしたフォルダの"
    "所有者のユーザーで動きます。保存先は MONITOR_DATABASE_URL で変えられます)"
)


def _is_sqlite_error(exc: BaseException) -> bool:
    """SQLite のドライバ(sqlite3)の例外か。"""
    orig = getattr(exc, "orig", None)
    return type(orig).__module__.split(".")[0] in ("sqlite3", "_sqlite3")


def _writable(path: Path) -> bool:
    return os.access(path, os.W_OK)


def _sqlite_hint(detail: str, path: Path | None) -> str:
    """SQLite のエラーの案内(``path`` は DB ファイル。分からなければ None)。"""
    readonly = "readonly database" in detail
    if readonly or "unable to open database file" in detail:
        if path is None:
            return (
                "SQLite の保存先フォルダ(既定: instances/)があり、そのフォルダと"
                "データベースファイルに書き込めるか確認してください。"
                + _SQLITE_PERMISSION_HINT
            )
        folder = path.parent
        if not folder.is_dir():
            return (
                f"SQLite の保存先フォルダ {folder} がありません(作れませんでした)。"
                "MONITOR_DATABASE_URL を確認してください"
            )
        if not _writable(folder) or (path.exists() and not _writable(path)):
            target = path if path.exists() and not _writable(path) else folder
            return (
                f"SQLite の{'データベースファイル' if target == path else '保存先フォルダ'}"
                f" {target} に書き込めません。" + _SQLITE_PERMISSION_HINT
            )
        if readonly:
            return (
                f"SQLite のデータベース {path} を書き込みできる状態で開けません"
                "(読み取り専用でマウントした場所などにあります)。"
                + _SQLITE_PERMISSION_HINT
            )
        return (
            f"SQLite のデータベース {path} を開けません(保存先のフォルダとファイル名、"
            "MONITOR_DATABASE_URL を確認してください)"
        )
    if "database is locked" in detail:
        return (
            "ほかのプロセス(起動中のサーバーなど)が書き込み中です。"
            "少し待ってから再実行してください"
        )
    return (
        "SQLite のデータベースファイル(MONITOR_DATABASE_URL)と、その保存先の"
        "空き容量・権限を確認してください"
    )


def _db_hint(
    detail: str, *, sqlite: bool | None = None, path: Path | None = None
) -> str:
    """DB のエラーの案内。``sqlite`` が None なら、エラー文から SQLite かを推し量る。"""
    if sqlite is None:
        sqlite = path is not None or any(
            text in detail
            for text in (
                "unable to open database file",
                "readonly database",
                "database is locked",
            )
        )
    if sqlite:
        return _sqlite_hint(detail, path)
    return (
        "MONITOR_DATABASE_URL の接続先(ホスト・ポート・ユーザー・パスワード)と"
        "データベースの状態を確認してください"
    )


#: DB ドライバのモジュール名(この import に失敗したら URL のドライバ指定を案内する)。
_DB_DRIVER_MODULES = frozenset(
    {
        "MySQLdb",
        "pymysql",
        "mysql",  # mysql.connector
        "mariadb",
        "psycopg2",
        "psycopg",
        "pg8000",
        "asyncpg",
        "aiomysql",
        "aiosqlite",
        "pyodbc",
        "oracledb",
        "cx_Oracle",
    }
)


_DB_URL_EXAMPLES = (
    "sqlite:///instances/database.db、"
    "postgresql+psycopg://ユーザー:パスワード@ホスト:5432/DB名、"
    "mysql+pymysql://ユーザー:パスワード@ホスト:3306/DB名"
)


_DB_URL_HINT = (
    f"MONITOR_DATABASE_URL は {_DB_URL_EXAMPLES} の形で指定してください"
    "(postgres:// で始まる接続文字列は postgresql+psycopg:// に書き換えます。"
    "パスワードの @ : / などの記号は @ → %40 のように URL エンコードします)"
)


#: psycopg(monitor-app[postgres])が無いときの接続 URL の案内(同梱の pg8000 を使う)。
_DB_URL_HINT_PG8000 = (
    "MONITOR_DATABASE_URL は sqlite:///instances/database.db、"
    "postgresql+pg8000://ユーザー:パスワード@ホスト:5432/DB名、"
    "mysql+pymysql://ユーザー:パスワード@ホスト:3306/DB名 の形で指定してください"
    "(postgres:// で始まる接続文字列は postgresql+pg8000://(同梱のドライバ)に"
    "書き換えます。postgresql+psycopg:// を使うには "
    'pip install "monitor-app[postgres]" が必要です。'
    "パスワードの @ : / などの記号は @ → %40 のように URL エンコードします)"
)


def _db_url_hint() -> str:
    """接続 URL の書き方の案内。PostgreSQL の例は、使えるドライバに合わせる。"""
    import importlib.util

    if importlib.util.find_spec("psycopg") is not None:
        return _DB_URL_HINT
    return _DB_URL_HINT_PG8000


_DB_DRIVER_HINT = (
    "同梱のドライバを URL に書いてください: MySQL は mysql+pymysql://、"
    "PostgreSQL は postgresql+pg8000:// か postgresql+psycopg://"
    '(pip install "monitor-app[postgres]" が必要)'
)


def _is_db_driver_module(name: str | None) -> bool:
    return bool(name) and name.split(".")[0] in _DB_DRIVER_MODULES


#: 実行時設定の値の誤りに添える案内。
_SETTINGS_HINT = (
    "環境変数か .env の MONITOR_* の値を直してください"
    "(設定の一覧と説明: .env.example)"
)


def explain_error(exc: BaseException) -> tuple[str, str | None] | None:
    """既知の例外を (メッセージ, ヒント) に変換する。未知の例外なら None。"""
    if isinstance(exc, ConfigError):
        return str(exc), None
    settings_message = settings_error_message(exc)
    if settings_message is not None:
        # MONITOR_PORT=abc など: pydantic の長いエラーの代わりに変数名と指定できる値
        return settings_message, _SETTINGS_HINT
    if isinstance(exc, UnicodeDecodeError):
        return (
            f"文字コード {exc.encoding} として読めないファイルがあります({exc.reason})",
            "Excel で保存した CSV なら .env に MONITOR_CSV_ENCODING=cp932 を"
            "設定してください",
        )
    if isinstance(exc, OSError) and is_address_in_use(exc):
        return (
            f"ポートは使用中です: {exc}",
            "既に起動中ではありませんか? --port で変更できます",
        )
    from ..exceptions import CsvEncodingError, MonitorAppError

    if isinstance(exc, CsvEncodingError):
        return exc.message, exc.hint
    if isinstance(exc, MonitorAppError):
        return exc.message, None
    if isinstance(exc, ModuleNotFoundError) and _is_db_driver_module(exc.name):
        # 例: mysql:// は同梱していない mysqlclient(MySQLdb)を使う。
        return (
            f"データベースのドライバ {exc.name} がインストールされていません",
            _DB_DRIVER_HINT,
        )
    # SQLAlchemy は例外が起きたときだけ import する(CLI の起動を軽く保つ)。
    from sqlalchemy.exc import ArgumentError, DBAPIError, NoSuchModuleError

    from ..db import errors as db_errors

    if isinstance(exc, NoSuchModuleError):
        # 例: postgres://(Heroku などの接続文字列)は SQLAlchemy の名前ではない。
        return (
            f"MONITOR_DATABASE_URL のデータベースの種類を読み取れません: {exc}",
            _db_url_hint(),
        )
    if isinstance(exc, ArgumentError) and "URL" in str(exc):
        # SQLAlchemy 2.0 の文言は URL(パスワードを含む)をそのまま含むため表示しない。
        return "MONITOR_DATABASE_URL の形式が正しくありません", _db_url_hint()
    if not isinstance(exc, DBAPIError):
        return None
    kind = db_errors.classify(exc)
    detail = _db_detail(exc)
    if kind == db_errors.CONFLICT:
        return (
            f"データベースの制約に違反しました: {detail}",
            "主キーの重複(CSV の中の同じ主キーなど)や外部キーの参照先の欠落がないか"
            "確認してください(--keep の追記では、主キーが既にある行は読み飛ばします)",
        )
    if kind == db_errors.INVALID:
        return (
            f"データベースが値を受け付けませんでした: {detail}",
            "文字列の長さ(str 列は 255 文字まで)や数値・日付の範囲を確認してください",
        )
    if kind == db_errors.SCHEMA:
        return (
            f"データベースの表定義が config.py と一致しません: {detail}",
            "monitor-app check で差分を確認してください"
            "(作成済みの表は列を足しても自動では変わりません)",
        )
    # 接続できない・ロック中・書き込めないなど(pg8000 の接続失敗は InterfaceError)。
    path = getattr(exc, _SQLITE_PATH_ATTR, None)
    if path is not None or _is_sqlite_error(exc):
        sqlite: bool | None = True
    else:  # ドライバの例外が分からなければ、エラー文から推し量る
        sqlite = None if getattr(exc, "orig", None) is None else False
    return f"データベースエラー: {detail}", _db_hint(detail, sqlite=sqlite, path=path)


#: 停止のシグナルを自分で扱うコマンド(uvicorn を動かす)。:class:`MonitorGroup` は
#: これら以外のコマンドの実行中に :func:`stop_signals` を使う。
SELF_MANAGED_SIGNAL_COMMANDS = frozenset({"runserver", "serve"})


#: 実行中のコマンド名を入れる ``ctx.meta`` のキー。
_COMMAND_KEY = "monitor_app.command"


class MonitorGroup(TyperGroup):
    """monitor-app のコマンド群。出力の文字コード対策と既知エラーの短い表示を加える。

    1 回で終わるコマンド(import-csv・check・migrate・sync-sources など)の実行中は
    SIGTERM を :class:`StopRequested` にする(:func:`stop_signals`)。コンテナでは
    monitor-app が PID 1 になり、ハンドラの無い SIGTERM はカーネルに無視されるため、
    ``docker stop`` が猶予時間いっぱい待って強制終了(SIGKILL)することになる。中断した
    ら短く知らせて終了コード 143 で終える(取り込み中のトランザクションは取り消し)。
    """

    def main(self, *args: Any, **kwargs: Any) -> Any:
        make_streams_safe()
        return super().main(*args, **kwargs)

    def resolve_command(
        self, ctx: typer.Context, args: list[str]
    ) -> tuple[str | None, Any, list[str]]:
        cmd_name, cmd, rest = super().resolve_command(ctx, args)
        if cmd_name is not None and cmd_name not in SELF_MANAGED_SIGNAL_COMMANDS:
            # コマンドの実行が終わってコンテキストを閉じるとき、元のハンドラに戻す
            ctx.with_resource(stop_signals())
            ctx.meta[_COMMAND_KEY] = cmd_name
        return cmd_name, cmd, rest

    def invoke(self, ctx: typer.Context) -> Any:
        try:
            return super().invoke(ctx)
        except (typer.Exit, typer.Abort):
            raise
        except StopRequested:
            name = ctx.meta.get(_COMMAND_KEY) or "処理"
            typer.secho(
                f"停止の指示(SIGTERM)を受けたため、{name} を中止しました", err=True
            )
            raise typer.Exit(143) from None
        except Exception as exc:
            explained = None if debug_enabled(ctx) else explain_error(exc)
            if explained is None:
                raise
            _echo_error(*explained)
            typer.echo(DEBUG_HINT, err=True)
            raise typer.Exit(1) from None
