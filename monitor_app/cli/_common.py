"""CLI のコマンドが共有するヘルパー。

- 実行時設定(``.env`` / ``MONITOR_*``)の読み込みとログ設定(``--config`` で作業
  フォルダの外の config.py を指定したら、そのフォルダの .env も読む — DX-06)
- config.py の場所の解決と読み込み(誤りは短いメッセージで終了する。``--config`` には
  プロジェクトのフォルダも指定できる)
- 相対パスの基準の確認(config.py のフォルダにしか無い DB・CSV を作業フォルダで探して
  空の DB を作らない)と、CSV のフォルダのどのファイルをどのテーブルへ取り込むかの要約
- データベースとテーブル定義を開く

エラーの短い表示と :class:`MonitorGroup` は :mod:`._errors`、停止のシグナルの扱い
(:func:`stop_signals`)は :mod:`._signals` にあり、ここからも同じ名前で import できる
(従来の import 先を保つ)。SQLAlchemy などの重いモジュールは必要になった時点で
import し、``--help`` や ``check`` の起動を軽く保つ。
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

import typer

from .. import logging_conf
from ..settings.loader import ConfigError, load_config, resolve_config_path
from ..settings.runtime import AppSettings, load_app_settings, same_path

# エラーの短い表示・停止のシグナルは別のモジュールに分けた。従来どおり
# ``monitor_app.cli._common`` から import できるよう、同じ名前で公開する。
from ._errors import (  # noqa: F401
    _DB_DRIVER_HINT,
    _DB_DRIVER_MODULES,
    _DB_URL_EXAMPLES,
    _DB_URL_HINT,
    _DB_URL_HINT_PG8000,
    _SETTINGS_HINT,
    _SQLITE_PATH_ATTR,
    _SQLITE_PERMISSION_HINT,
    DEBUG_HINT,
    SELF_MANAGED_SIGNAL_COMMANDS,
    MonitorGroup,
    _db_detail,
    _db_hint,
    _db_url_hint,
    _echo_error,
    _is_db_driver_module,
    _is_sqlite_error,
    _is_utf8,
    _sqlite_hint,
    debug_enabled,
    explain_error,
    is_address_in_use,
    make_streams_safe,
    remember_sqlite_path,
)
from ._signals import StopRequested, stop_signals  # noqa: F401

if TYPE_CHECKING:
    from ..db.engine import Database
    from ..db.registry import TableRegistry
    from ..settings.declarative import MonitorConfig

logger = logging.getLogger("monitor_app.cli")

#: config.py が見つからないときの案内。
MISSING_CONFIG_HINT = (
    "monitor-app startproject <name> で雛形を作成するか、"
    "--config で config.py の場所を指定してください"
)
#: ``--config`` の説明(config.py を使うコマンドで共通)。
CONFIG_HELP = "設定ファイル(config.py)のパス。プロジェクトのフォルダも指定できる"
#: 要約に並べるファイル名・テーブル名の最大数。
_MAX_LISTED = 12


# ---------------------------------------------------------------------------
# 終了
# ---------------------------------------------------------------------------


def fail(message: str, hint: str | None = None) -> NoReturn:
    """エラーを表示し、終了コード 1 で終える。"""
    _echo_error(message, hint)
    raise typer.Exit(1)


# ---------------------------------------------------------------------------
# 設定・DB
# ---------------------------------------------------------------------------
def load_settings(config_path: Path | None = None) -> AppSettings:
    """実行時設定(``.env`` / ``MONITOR_*``)を読み込み、ログ出力を設定する。

    ``config_path`` を渡すと、config.py のフォルダの ``.env`` も読む(作業フォルダの
    .env が優先 — :func:`~monitor_app.settings.runtime.project_env_files`)。ログの
    形式は ``MONITOR_LOG_FORMAT``(text / json)に従う。

    ``configure_logging`` は呼び出し時にモジュールから引く。テストが
    ``monitor_app.logging_conf.configure_logging`` を差し替えれば CLI にも効く
    (単一モジュール時代の ``monitor_app.cli.configure_logging`` の代わり)。
    """
    settings = load_app_settings(config_path)
    logging_conf.configure_logging(settings.log_level, log_format=settings.log_format)
    return settings


def resolve_config(config: str | os.PathLike) -> Path:
    """``--config`` のパスを返す。ファイルが無ければ案内を表示して終了する。

    フォルダを指定したら、その中の ``config.py`` を使う(DX-06。規則は
    :func:`~monitor_app.settings.loader.resolve_config_path` と同じ)。
    """
    path = resolve_config_path(config)
    if not path.is_file():
        fail(f"config.py が見つかりません: {path}", MISSING_CONFIG_HINT)
    return path


def read_config(path: Path) -> MonitorConfig:
    """config.py を読み込んで検証する。誤りがあれば理由を表示して終了する。"""
    try:
        return load_config(path)
    except ConfigError as exc:
        fail(str(exc))


def load_project(
    config: str, *, csv: bool = True, check_paths: bool = True
) -> tuple[Path, AppSettings, MonitorConfig]:
    """config.py の解決 → 実行時設定とログの初期化 → config.py の読み込みを行う。

    作業フォルダが config.py のフォルダと違い、config.py のフォルダにしか無い相対パス
    (DB・CSV のフォルダなど)があれば、作業フォルダに空の DB を作る前に止める
    (:func:`check_relative_paths`)。``csv=False`` は CSV のフォルダを使わないコマンド
    (CSV のフォルダのずれは警告だけにする)。``check_paths=False`` なら確かめない
    (呼び出し側が後で :func:`check_relative_paths` を呼ぶ)。
    """
    path = resolve_config(config)
    settings = load_settings(path)
    cfg = read_config(path)
    if check_paths:
        check_relative_paths(path, settings, cfg, csv=csv)
    return path, settings, cfg


# ---------------------------------------------------------------------------
# パスの基準と CSV の対応(DX-06)
# ---------------------------------------------------------------------------
#: 同じフォルダか(無いフォルダは絶対パスの表記で比べる。.env の判定と同じ規則)。
same_dir = same_path


def _is_relative(path: str | os.PathLike, *, expand: bool = True) -> bool:
    text = os.fspath(path)
    return not os.path.isabs(os.path.expanduser(text) if expand else text)


#: 相対パスの種類(:func:`_relative_entries`)。
_DB, _CSV, _BACKUP, _SOURCE = "db", "csv", "backup", "source"


def _relative_entries(
    settings: AppSettings, cfg: MonitorConfig | None = None
) -> list[tuple[str, str]]:
    """作業フォルダを基準に解釈される、設定の中の相対パスと、その種類。"""
    from sqlalchemy.exc import ArgumentError  # SQLAlchemy は使う時点で読み込む

    from ..db.engine import parse_url

    found: list[tuple[str, str]] = []
    try:
        url = parse_url(settings.database_url)
    except ArgumentError:
        url = None
    if url is not None and url.get_backend_name() == "sqlite":
        database = url.database or ""
        if (
            database
            and database != ":memory:"
            and not database.startswith("file:")
            and _is_relative(database)
        ):
            found.append((database, _DB))
    if _is_relative(settings.csv_dir, expand=False):  # 取り込みは ~ を展開しない
        found.append((str(settings.csv_dir), _CSV))
    if settings.backup_dir is not None and _is_relative(settings.backup_dir):
        found.append((str(settings.backup_dir), _BACKUP))
    if cfg is not None:
        found.extend(
            (s.path, _SOURCE) for s in cfg.sources.values() if _is_relative(s.path)
        )
    unique: dict[str, str] = {}
    for name, kind in found:
        unique.setdefault(name, kind)
    return list(unique.items())


def relative_paths(
    settings: AppSettings, cfg: MonitorConfig | None = None
) -> list[str]:
    """作業フォルダを基準に解釈される、設定の中の相対パス(表示用の書き方のまま)。

    SQLite の DB ファイル・CSV のフォルダ・バックアップの保存先・sources のファイル。
    """
    return [name for name, _kind in _relative_entries(settings, cfg)]


def relative_path_mismatches(
    config_path: Path, settings: AppSettings, cfg: MonitorConfig | None = None
) -> list[tuple[str, str]]:
    """config.py のフォルダにはあるのに、作業フォルダには無い相対パス(と種類)。

    相対パスは従来どおり作業フォルダを基準にする。プロジェクトの外から
    ``--config <プロジェクト>`` で実行すると、プロジェクトの DB・CSV ではなく作業
    フォルダの(無い)ものを使うことになる。作業フォルダにもあるパス(0.1.x の
    ``config/config.py`` をプロジェクトのフォルダから使う形など)は問題にしない。
    """
    project_dir = Path(os.path.abspath(config_path)).parent
    cwd = Path.cwd()
    if same_dir(project_dir, cwd):
        return []
    return [
        (name, kind)
        for name, kind in _relative_entries(settings, cfg)
        if not (cwd / name).exists() and (project_dir / name).exists()
    ]


def _quote_path(path: Path) -> str:
    text = str(path)
    return f'"{text}"' if any(c.isspace() for c in text) else text


def _mismatch_message(config_path: Path, names: list[str]) -> tuple[str, str]:
    project_dir = Path(os.path.abspath(config_path)).parent
    message = (
        f"相対パス({'、'.join(names)})は今のフォルダ({Path.cwd()})を基準にしますが、"
        f"今のフォルダには無く、config.py のフォルダ({project_dir})にあります"
    )
    hint = (
        f"cd {_quote_path(project_dir)} でプロジェクトのフォルダへ移動してから実行するか、"
        ".env の MONITOR_DATABASE_URL・MONITOR_CSV_DIR などを絶対パスで指定してください"
    )
    return message, hint


def relative_path_hint(
    config_path: Path, settings: AppSettings, cfg: MonitorConfig | None = None
) -> str | None:
    """config.py のフォルダにしか無い相対パスがあるときの 1 行の案内(無ければ None)。"""
    mismatches = relative_path_mismatches(config_path, settings, cfg)
    if not mismatches:
        return None
    message, hint = _mismatch_message(config_path, [n for n, _k in mismatches])
    return f"{message}。{hint}"


def warn_relative_paths(
    config_path: Path, settings: AppSettings, cfg: MonitorConfig | None = None
) -> None:
    """:func:`relative_path_hint` の案内を警告としてログに出す(無ければ何もしない)。"""
    hint = relative_path_hint(config_path, settings, cfg)
    if hint:
        logger.warning("%s", hint)


def check_relative_paths(
    config_path: Path,
    settings: AppSettings,
    cfg: MonitorConfig | None = None,
    *,
    csv: bool = False,
) -> None:
    """config.py のフォルダにしか無い DB(``csv=True`` なら CSV のフォルダも)を使う
    設定なら、案内して終了する。それ以外のパス(バックアップの保存先・sources)は警告。

    そのまま続けると、作業フォルダに空の DB を作って使ってしまう(プロジェクトの
    データが無いかのように表示・取り込みされる)。
    """
    mismatches = relative_path_mismatches(config_path, settings, cfg)
    if not mismatches:
        return
    blocking = {_DB, _CSV} if csv else {_DB}
    names = [name for name, _kind in mismatches]
    message, hint = _mismatch_message(config_path, names)
    if any(kind in blocking for _name, kind in mismatches):
        fail(message, hint)
    logger.warning("%s。%s", message, hint)


@dataclass
class CsvPlan:
    """CSV のフォルダのどのファイルを、どのテーブルへ取り込むか(取り込みの前の要約)。"""

    #: CSV のフォルダ(絶対パス)。
    csv_dir: Path
    exists: bool
    #: CSV(``<テーブル名>.csv``)があるテーブル。
    matched: list[str] = field(default_factory=list)
    #: CSV が無いテーブル(sources から取り込むテーブルは除く)。
    missing: list[str] = field(default_factory=list)

    def describe(self) -> str:
        """``/abs/csv(users.csv → users、…。CSV の無いテーブル: orders)`` の形。"""
        if not self.exists:
            return f"{self.csv_dir}(フォルダがありません)"
        parts = [f"{table}.csv → {table}" for table in self.matched]
        text = _listing(parts, "、") if parts else "テーブルに対応する CSV なし"
        if self.matched and self.missing:
            text += f"。CSV の無いテーブル: {_listing(self.missing, ', ')}"
        return f"{self.csv_dir}({text})"

    def warning(self) -> str | None:
        """どのテーブルの CSV も無いときの警告(期待するファイル名つき)。無ければ None。"""
        if self.matched or not self.missing:
            return None
        names = _listing([f"{table}.csv" for table in self.missing], ", ")
        if not self.exists:
            return (
                f"CSV のフォルダがありません: {self.csv_dir}"
                f"(取り込むファイル名: {names}。場所は MONITOR_CSV_DIR で指定できます)"
            )
        return (
            f"CSV のフォルダ {self.csv_dir} に、テーブルに対応する CSV がありません"
            f"(取り込むファイル名: {names})"
        )


def _listing(names: list[str], sep: str) -> str:
    shown = sep.join(names[:_MAX_LISTED])
    rest = len(names) - _MAX_LISTED
    return shown + (f"{sep}ほか {rest} 件" if rest > 0 else "")


def plan_csv_import(settings: AppSettings, cfg: MonitorConfig) -> CsvPlan:
    """取り込みと同じ規則(``<CSV のフォルダ>/<テーブル名>.csv``)で CSV の対応を調べる。"""
    csv_dir = Path(os.path.abspath(settings.csv_dir))  # 取り込みと同じく ~ は展開しない
    plan = CsvPlan(csv_dir=csv_dir, exists=csv_dir.is_dir())
    source_tables = {source.table for source in cfg.sources.values()}
    for table in cfg.tables:
        if plan.exists and (csv_dir / f"{table}.csv").is_file():
            plan.matched.append(table)
        elif table not in source_tables:
            plan.missing.append(table)
    return plan


def sqlite_url(path: Path) -> str:
    """SQLite ファイルの接続 URL(``?`` を含むパスは扱えないため呼び出し前に弾く)。"""
    from sqlalchemy.engine import URL

    text = path.as_posix()
    if "#" in text or "%" in text:
        # URL として特別な意味を持つ文字は、SQLAlchemy に任せてエスケープする
        return URL.create("sqlite", database=text).render_as_string(hide_password=False)
    return f"sqlite:///{text}"


@contextmanager
def open_database(
    settings: AppSettings, config: MonitorConfig
) -> Iterator[tuple[Database, TableRegistry]]:
    """設定の DB とテーブル定義を渡す。抜けるときに DB のコネクションを閉じる。

    サーバー DB(MySQL/PostgreSQL)は ``db_wait_timeout`` 秒まで起動を待つ
    (Docker Compose で DB と同時に起動したときなど)。
    """
    from sqlalchemy.exc import DBAPIError

    from ..db.engine import Database
    from ..db.registry import TableRegistry

    db = Database.from_settings(settings)
    try:
        if db.backend != "sqlite" and settings.db_wait_timeout > 0:
            db.wait_until_ready(settings.db_wait_timeout)
        yield db, TableRegistry(config)
    except DBAPIError as exc:
        # エラーの案内(explain_error)が、書き込めないフォルダ・ファイルを示せるように
        remember_sqlite_path(exc, db.sqlite_path)
        raise
    finally:
        db.dispose()
