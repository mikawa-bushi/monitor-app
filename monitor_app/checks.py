"""設定・データ・実行環境の点検(``monitor-app check`` が使う)。

:func:`run_checks` は検証済みの :class:`MonitorConfig` と実行時設定を受け取り、
起動前に分かる問題を :class:`Issue` のリストで返す。例外は送出しない。

- **SQL の空実行** … 各ビュー・KPI の SQL を、テーブル定義から作ったインメモリの
  SQLite で ``SELECT * FROM (<SQL>) AS _sub LIMIT 0`` として実行する。テーブル名・
  列名の誤りが分かり、結果の列名とアラート・グラフ・セルスタイル・見出しの列を
  照合できる(列名を誤ったアラートは黙って発火しない)。DB が SQLite でなければ、
  SQL の方言の違いがあり得るため SQL の問題は警告にとどめる。
- **データ**(``data=True``)… CSV の有無・見出しと列の対応・文字コード・列名から
  型を推論した列のデータが数値か(#DX-08)・宣言した型に変換できない値・主キーの
  重複・PostgreSQL / MySQL の列に入らない値・読めない行。取り込み(CsvImporter)と
  同じ処理で調べる(点検が OK と言った CSV が取り込みで行を落とさない)。
- **セキュリティ** … API キーなしで LAN に公開している・未知の ``MONITOR_*``
  (``.env`` の綴りの誤りで認証が無効のままになる — #CRIT-04)。
- **通知** … アラートの通知先(notify)のチャネルが未知・未設定(通知が届かない)。
"""

from __future__ import annotations

import difflib
import importlib.resources
import os
import re
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from .security import is_loopback_host
from .settings.declarative import MonitorConfig
from .settings.runtime import AppSettings, find_unknown_env_keys

if TYPE_CHECKING:
    from sqlalchemy import Connection


Level = Literal["error", "warning", "info"]

#: 表示用の重要度の名前と並び順。
LEVEL_LABELS: dict[str, str] = {"error": "エラー", "warning": "警告", "info": "情報"}
_LEVEL_ORDER = {"error": 0, "warning": 1, "info": 2}
#: 認証に関わる設定。綴りを誤ると認証が無効のまま起動するため、エラーとして扱う。
_AUTH_ENV_KEYS = {
    "MONITOR_API_KEY": "api_key",
    "MONITOR_PROTECT_READS": "protect_reads",
    "MONITOR_ADMIN_KEY": "admin_key",
}
#: 一覧として表示する列名などの最大数。
_MAX_LISTED = 12

_NO_SUCH_TABLE_RE = re.compile(r"no such table: (?:main\.)?([^\s,]+)", re.IGNORECASE)
_NO_SUCH_COLUMN_RE = re.compile(r"no such column: ([^\s,]+)", re.IGNORECASE)
#: SQLAlchemy の text() が SQL の ``:名前`` をパラメータとみなしたときのエラー。
_BIND_PARAM_RE = re.compile(r"A value is required for bind parameter '([^']*)'")
#: SQLAlchemy のエラー文の末尾に付く説明ページの案内(利用者には不要)。
_BACKGROUND_RE = re.compile(r"\s*\(Background on this error at: [^)]*\)")


@dataclass(frozen=True)
class Issue:
    """点検で見つかった 1 件の問題。"""

    #: error(直すまで正しく動かない)/ warning(動くが意図どおりでない恐れ)/ info。
    level: Level
    #: 機械可読な種別(例: ``sql-error``・``alert-column``・``env-unknown-key``)。
    code: str
    message: str
    #: 直し方の案内(任意)。
    hint: str | None = None


# ---------------------------------------------------------------------------
# 表示
# ---------------------------------------------------------------------------
def sort_issues(issues: list[Issue]) -> list[Issue]:
    """重要度の順(エラー → 警告 → 情報)に並べる(同じ重要度では元の順)。"""
    return sorted(issues, key=lambda issue: _LEVEL_ORDER.get(issue.level, 9))


def summarize(issues: list[Issue]) -> str:
    """件数の要約(例: ``エラー 1 件・警告 2 件・情報 0 件``)。"""
    counts = {level: 0 for level in LEVEL_LABELS}
    for issue in issues:
        counts[issue.level] = counts.get(issue.level, 0) + 1
    return "・".join(f"{LEVEL_LABELS[level]} {counts[level]} 件" for level in counts)


def has_errors(issues: list[Issue]) -> bool:
    return any(issue.level == "error" for issue in issues)


def _listing(names: list[str]) -> str:
    shown = ", ".join(names[:_MAX_LISTED])
    return shown + (" …" if len(names) > _MAX_LISTED else "")


def _closest(name: str, candidates: list[str]) -> str | None:
    lowered = {c.lower(): c for c in candidates}
    if name.lower() in lowered:
        return lowered[name.lower()]
    found = difflib.get_close_matches(name, candidates, n=1, cutoff=0.6)
    return found[0] if found else None


def _did_you_mean(name: str, candidates: list[str]) -> str | None:
    match = _closest(name, candidates)
    return f"'{match}' の誤りではありませんか?" if match and match != name else None


# ---------------------------------------------------------------------------
# 実行時設定・セキュリティ
# ---------------------------------------------------------------------------
def _encoding_issues(settings: AppSettings) -> list[Issue]:
    from .services import csv_encoding

    issues: list[Issue] = []
    auto = csv_encoding.is_auto(settings.csv_encoding)
    names = [] if auto else [settings.csv_encoding]
    for env, values in (
        ("MONITOR_CSV_ENCODING", names),
        (
            "MONITOR_CSV_ENCODING_FALLBACK",
            csv_encoding.split_encodings(settings.csv_encoding_fallback),
        ),
    ):
        for name in values:
            if not csv_encoding.is_known(name):
                issues.append(
                    Issue(
                        "error",
                        "csv-encoding-name",
                        f"{env} の '{name}' は文字コード名として使えません",
                        "utf-8-sig・cp932・euc-jp などを指定してください",
                    )
                )
    return issues


def _settings_issues(
    settings: AppSettings, env_files: Sequence[str | os.PathLike] | None = None
) -> list[Issue]:
    issues: list[Issue] = []
    from sqlalchemy.exc import ArgumentError

    from .db.engine import parse_url

    try:
        parse_url(settings.database_url)
    except ArgumentError:
        issues.append(
            Issue(
                "error",
                "database-url",
                "MONITOR_DATABASE_URL を接続先として解釈できません",
                "例: sqlite:///instances/database.db"
                " / postgresql+psycopg://user:pass@host:5432/db"
                "(パスワードの @ : / などの記号は @ → %40 のように URL エンコードします)",
            )
        )
    if not 0 <= settings.port <= 65535:
        issues.append(
            Issue(
                "error",
                "port",
                f"MONITOR_PORT={settings.port} はポート番号として使えません",
                "0〜65535 の範囲で指定してください(既定は 9990)",
            )
        )
    issues.extend(_encoding_issues(settings))

    if not settings.api_key:
        if not is_loopback_host(settings.host):
            issues.append(
                Issue(
                    "warning",
                    "insecure-bind",
                    f"MONITOR_HOST={settings.host} で公開していますが API キーが未設定です。"
                    "同じネットワークの誰でもデータを追加・変更・削除できます",
                    ".env に MONITOR_API_KEY=<長いランダムな文字列> を設定してください"
                    "(閲覧も保護するなら MONITOR_PROTECT_READS=true)",
                )
            )
        if settings.protect_reads:
            issues.append(
                Issue(
                    "warning",
                    "protect-reads-without-key",
                    "MONITOR_PROTECT_READS=true ですが MONITOR_API_KEY が未設定のため、"
                    "閲覧は保護されません",
                    ".env に MONITOR_API_KEY を設定してください",
                )
            )
    # 説明用の値(change-me など)・短すぎる・ASCII 以外のキー(#D6-17・#D6-07)
    from .security import configured_key_problems

    for problem in configured_key_problems(settings.api_key, settings.admin_key):
        issues.append(Issue("warning", problem.code, problem.message, problem.hint))

    for found in find_unknown_env_keys(env_files=env_files):
        field = _AUTH_ENV_KEYS.get(found.suggestion or "")
        # 認証の設定の綴りを誤ったまま、その設定が実際に効いていなければエラー
        level: Level = (
            "error" if field is not None and not getattr(settings, field) else "warning"
        )
        # 「環境変数の MONITOR_X」・「/path/.env の MONITOR_X」(ファイルのパスの後だけ空ける)
        place = found.source if found.source == "環境変数" else f"{found.source} "
        message = f"{place}の {found.key} は monitor-app の設定ではありません"
        if field is not None and level == "error":
            message += "(このままでは認証が有効になりません)"
        hint = (
            f"{found.suggestion} の誤りではありませんか?"
            if found.suggestion
            else "設定の一覧は .env.example を参照してください"
        )
        issues.append(Issue(level, "env-unknown-key", message, hint))
    return issues


def _notify_issues(cfg: MonitorConfig, settings: AppSettings) -> list[Issue]:
    """アラートの通知先(notify)のうち、通知が届かない(または危険な)チャネル。

    サーバーの起動時に記録する警告と同じ判定(未知のチャネル・未設定の接続先・
    暗号化しない SMTP の認証)。チャネルごとに 1 件。
    """
    from .services.notifiers import build_notifiers, channel_problem

    notifiers = build_notifiers(settings)
    issues: list[Issue] = []
    seen: set[str] = set()
    for i, rule in enumerate(cfg.alerts):
        for name in rule.notify:
            if name in seen:
                continue
            seen.add(name)
            problem = channel_problem(name, settings, notifiers)
            if problem:
                issues.append(
                    Issue(
                        "warning",
                        "alert-channel",
                        f"alerts[{i}] の通知先 '{name}': {problem}",
                        "通知先の設定は .env.example の「アラート通知」を参照してください",
                    )
                )
    return issues


def _reinsert_issue(name: str, pk: str) -> Issue:
    return Issue(
        "warning",
        "source-reinsert",
        f"ソース '{name}' は変わるたびにファイル全体を読み直すため、追記(mode='append')"
        f"では毎回すべての行を追加し直します(レコードに主キー '{pk}' が無く、"
        "取り込み済みの行を見分けられません)",
        f"mode='replace' にするか、レコードの主キー '{pk}' を取り込んでください"
        "(mapping で対応付けられます)",
    )


def _source_issues(cfg: MonitorConfig) -> list[Issue]:
    """ファイル全体を毎回読むソース(json・watermark の無い query)を、主キー無しで
    追記するもの(変わるたびに全行を入れ直して重複する — D3-08)。"""
    issues: list[Issue] = []
    for name, source in cfg.sources.items():
        snapshot = source.kind == "json" or (
            source.kind == "sqlite"
            and source.query is not None
            and source.watermark_column is None
        )
        tdef = cfg.tables.get(source.table)
        if source.mode != "append" or not snapshot or tdef is None:
            continue
        if source.mapping and tdef.primary_key not in source.mapping:
            issues.append(_reinsert_issue(name, tdef.primary_key))
    return issues


def _static_issues(cfg: MonitorConfig) -> list[Issue]:
    from .web import static_asset_path

    static = Path(str(importlib.resources.files("monitor_app") / "web" / "static"))
    shown = static_asset_path(cfg.favicon_path)  # 画面が実際に使うパス
    try:
        target = (static / shown).resolve()
        ok = target.is_file() and target.is_relative_to(static.resolve())
    except (OSError, ValueError):
        ok = False
    if ok and shown != cfg.favicon_path:
        # v1 の既定値 'favicon.ico' など: 同梱の img/ のファイルで表示する
        return [
            Issue(
                "info",
                "favicon-fallback",
                f"favicon_path '{cfg.favicon_path}' のファイルは無いため、"
                f"同梱の '{shown}' を表示します",
                f"FAVICON_PATH(v2 では favicon_path)を '{shown}' にしてください",
            )
        ]
    if ok:
        return []
    hint = "favicon_path は同梱の静的ファイル(例: img/favicon.ico)を指定してください"
    return [
        Issue(
            "warning",
            "favicon-missing",
            f"favicon_path '{cfg.favicon_path}' のファイルがありません"
            "(ブラウザのタブのアイコンが表示されません)",
            hint,
        )
    ]


# ---------------------------------------------------------------------------
# SQL の空実行
# ---------------------------------------------------------------------------
def _db_message(exc: Exception) -> str:
    source = getattr(exc, "orig", None) or exc
    lines = _BACKGROUND_RE.sub("", str(source)).strip().splitlines()
    return lines[0] if lines else type(source).__name__


def _schema_database(cfg: MonitorConfig):
    """テーブル定義(と内部テーブル)だけを持つインメモリの SQLite を作る。"""
    from .db.engine import Database
    from .db.ingest_state import IngestStateStore
    from .db.registry import TableRegistry
    from .services.audit_service import AuditService

    db = Database("sqlite://")
    try:
        TableRegistry(cfg).create_all(db)
        IngestStateStore(db).create()
        AuditService(db, True)  # 監査ログの表(_audit)を参照するビューのため
    except Exception:
        db.dispose()
        raise
    return db


def _real_sqlite_engine(settings: AppSettings):
    """設定の DB が既存の SQLite ファイルなら、読み取り専用で開くエンジンを返す。"""
    import sqlite3

    from sqlalchemy import create_engine
    from sqlalchemy.exc import ArgumentError

    from .db.engine import parse_url

    try:
        url = parse_url(settings.database_url)
    except ArgumentError:
        return None
    database = url.database
    if url.get_backend_name() != "sqlite" or not database:
        return None
    if database == ":memory:" or database.startswith("file:"):
        return None
    path = Path(database).expanduser()
    if not path.is_file():
        return None
    uri = path.resolve().as_uri() + "?mode=ro"
    return create_engine("sqlite://", creator=lambda: sqlite3.connect(uri, uri=True))


@dataclass
class _DryRun:
    #: 包まずに実行したときの結果の列名(画面の表示・アラート・グラフが使う列名)。
    #: 求められなければ包んだときの列名、それも無ければ None。
    columns: list[str] | None
    error: str | None = None
    #: True ならクエリ単体は正しいが、副問い合わせで包むと失敗する。
    wrap_only: bool = False
    #: 副問い合わせ ``(...) LIMIT`` で包んだときの列名。SQLite では包むと列名が
    #: 変わることがある(``SELECT name`` → 宣言の ``Name`` ではなく ``name``、
    #: 重複した列名 → ``id:1``)。行数を絞る取得(ページ送り・max_rows)はこちら。
    wrapped: list[str] | None = None


#: 空のインメモリ DB で SQL を実行する時間の上限(秒)。表を使わない再帰 CTE の集計
#: のように、データが無くても終わらない SQL で点検が止まらないようにする。
_DRY_RUN_TIMEOUT = 2.0


@contextmanager
def _time_limit(conn: Connection, seconds: float) -> Iterator[None]:
    """SQLite の実行を ``seconds`` 秒で打ち切る(超えると "interrupted" のエラー)。"""
    raw = conn.connection.dbapi_connection
    deadline = time.monotonic() + seconds
    raw.set_progress_handler(lambda: int(time.monotonic() > deadline), 10_000)
    try:
        yield
    finally:
        raw.set_progress_handler(None, 0)


def _dry_run(
    conn: Connection, query: str, *, empty_db: bool = True, wrap: bool = True
) -> _DryRun:
    """SQL を空実行する。

    ``empty_db`` は接続先が空のインメモリ DB であること(空なら包まずにも実行し、
    実行時と同じ列名を得る)。``wrap=False`` は包まずにだけ実行する SQL(KPI)。
    どの実行も ``_DRY_RUN_TIMEOUT`` 秒で打ち切り、時間切れは誤りとしない。
    """
    from sqlalchemy import text
    from sqlalchemy.exc import SQLAlchemyError

    def attempt(sql: str) -> tuple[list[str] | None, str | None]:
        """(結果の列名, エラー)。時間切れなら (None, None)。"""
        try:
            with _time_limit(conn, _DRY_RUN_TIMEOUT):
                # ドライバは最初の 1 行までしか進めない(結果は読まずに閉じる)
                result = conn.execute(text(sql))
                columns = list(result.keys())
                result.close()
        except SQLAlchemyError as exc:
            conn.rollback()
            message = _db_message(exc)
            return None, None if message == "interrupted" else message
        return columns, None

    wrapped, wrap_error = (None, None)
    if wrap:
        wrapped, wrap_error = attempt(f"SELECT * FROM ({query}) AS _sub LIMIT 0")
    bare, bare_error = (None, None)
    if empty_db:  # 実データのある DB では重い集計などを実行しない
        bare, bare_error = attempt(query)
    if bare_error is not None and (wrap_error is not None or not wrap):
        return _DryRun(None, bare_error)  # 文として誤り
    if wrap_error is None and (bare is not None or wrapped is not None):
        return _DryRun(bare if bare is not None else wrapped, wrapped=wrapped)
    if bare is None:
        try:  # 包まずに、文として正しいかを確かめる(EXPLAIN は実行しない)
            conn.execute(text(f"EXPLAIN {query}")).close()
        except SQLAlchemyError as exc:
            conn.rollback()
            return _DryRun(None, _db_message(exc))
        if wrap_error is None:
            return _DryRun(None)  # 正しいが列名は分からない(時間切れ・KPI)
    return _DryRun(bare, wrap_error, wrap_only=True)


def _sql_hint(error: str, cfg: MonitorConfig) -> str | None:
    match = _NO_SUCH_TABLE_RE.search(error)
    if match:
        name = match.group(1).strip("\"'`[]")
        tables = list(cfg.tables)
        return _did_you_mean(name, tables) or (
            f"テーブル '{name}' は config.py の tables にありません"
            f"(定義済み: {_listing(tables) or 'なし'})"
        )
    match = _NO_SUCH_COLUMN_RE.search(error)
    if match:
        name = match.group(1).split(".")[-1].strip("\"'`[]")
        columns = sorted({c for t in cfg.tables.values() for c in t.column_names})
        return _did_you_mean(name, columns)
    match = _BIND_PARAM_RE.search(error)
    if match:
        return (
            f"SQL の中の ':{match.group(1)}' はパラメータとして扱われます。文字としての"
            " ':' は \\: と書いてください(config.py では r\"...\" の文字列にすると"
            "書きやすい)"
        )
    return None


class _SqlChecker:
    """ビュー・KPI の SQL を空実行し、結果の列と設定を照合する。"""

    def __init__(self, cfg: MonitorConfig, settings: AppSettings) -> None:
        from sqlalchemy.exc import ArgumentError

        from .db.engine import parse_url

        self.cfg = cfg
        self.settings = settings
        try:
            backend = parse_url(settings.database_url).get_backend_name()
        except ArgumentError:
            backend = "sqlite"
        self.sqlite = backend == "sqlite"
        self.backend = backend
        self.issues: list[Issue] = []
        #: ビュー名 → 画面・アラートが使う結果の列名(実行できたビューだけ)。
        self.view_columns: dict[str, list[str]] = {}
        self._real = None

    def _level(self) -> Level:
        return "error" if self.sqlite else "warning"

    def _dialect_note(self) -> str:
        return (
            ""
            if self.sqlite
            else f"(SQLite で試したため、{self.backend} では問題ない可能性があります)"
        )

    def _retry_on_real_db(
        self, query: str, error: str, *, wrap: bool
    ) -> _DryRun | None:
        """設定に無いテーブルを使う SQL を、実際の SQLite ファイルで試し直す。"""
        match = _NO_SUCH_TABLE_RE.search(error)
        if not match or match.group(1).strip("\"'`[]") in self.cfg.tables:
            return None
        if self._real is None:
            self._real = _real_sqlite_engine(self.settings) or False
        if not self._real:
            return None
        try:
            with self._real.connect() as conn:
                return _dry_run(conn, query, empty_db=False, wrap=wrap)
        except Exception:  # noqa: BLE001 - 開けなければ元の結果で報告する
            return None

    def run_query(
        self, conn: Connection, kind: str, name: str, query: str, *, wrap: bool = True
    ) -> _DryRun | None:
        """1 本の SQL を空実行する。実行できれば結果(列名)、できなければ None。

        ``wrap=False`` は副問い合わせで包まずに実行される SQL(KPI)。
        """
        label = f"{kind} '{name}'"
        run = _dry_run(conn, query, wrap=wrap)
        if run.error and not run.wrap_only:
            retry = self._retry_on_real_db(query, run.error, wrap=wrap)
            if retry is not None and (retry.error is None or retry.wrap_only):
                self.issues.append(
                    Issue(
                        "info",
                        "sql-external-table",
                        f"{label} は config.py に無いテーブルを使っています"
                        "(DB にあるため動作します)",
                    )
                )
                run = retry
        if run.wrap_only:
            self.issues.append(
                Issue(
                    "warning",
                    "sql-limit-wrap",
                    f"{label} の SQL は、行数を絞る副問い合わせ (...) LIMIT で包むと"
                    f"失敗します: {run.error}",
                    "末尾の -- コメントを消すか /* */ に書き換えてください"
                    "(ページ送りなど行数を指定した取得で失敗します)",
                )
            )
            return run
        if run.error is None:
            return run
        self.issues.append(
            Issue(
                self._level(),
                "sql-error",
                f"{label} の SQL を実行できません: {run.error}{self._dialect_note()}",
                _sql_hint(run.error or "", self.cfg),
            )
        )
        return None

    def check(self) -> list[Issue]:
        from sqlalchemy.exc import SQLAlchemyError

        try:
            db = _schema_database(self.cfg)
        except SQLAlchemyError as exc:
            return [
                Issue(
                    "error",
                    "schema",
                    f"テーブル定義からデータベースを作れません: {_db_message(exc)}",
                    "テーブル名・列名の重複(大文字・小文字の違いだけの名前を含む)と、"
                    "foreign_keys の参照先の列を確認してください",
                )
            ]
        try:
            with db.engine.connect() as conn:
                view_runs = {
                    name: self.run_query(conn, "ビュー", name, view.query)
                    for name, view in self.cfg.views.items()
                }
                for name, card in self.cfg.kpis.items():
                    # KPI は包まずに実行される(行数を絞る取得は無い)
                    self.run_query(conn, "KPI", name, card.query, wrap=False)
        finally:
            db.dispose()
            if self._real:
                self._real.dispose()
        self.view_columns = {
            name: run.columns
            for name, run in view_runs.items()
            if run is not None and run.columns is not None
        }
        self._check_columns(view_runs)
        self._check_kpi_formats()
        return self.issues

    def _check_columns(self, view_runs: dict[str, _DryRun | None]) -> None:
        """アラート・グラフ・セルスタイル・見出しの列を、実行時の結果の列と照合する。

        列名は画面・アラートが使う(包まずに実行した)結果の列名で照合する。max_rows の
        ビューは行数を絞る取得になるため、包んだときの列名も認める。SQLite 以外の DB
        では列名の大文字・小文字の扱いが異なり得る(PostgreSQL は引用符の無い別名を
        小文字にする)ため、警告にとどめる。
        """
        known: dict[str, tuple[list[str], set[str]]] = {}
        for name, view in self.cfg.views.items():
            run = view_runs.get(name)
            if run is None or run.columns is None:
                continue
            accepted = set(run.columns)
            if view.max_rows is not None:
                accepted |= set(run.wrapped or ())
            known[name] = (run.columns, accepted)
        note = self._dialect_note()
        for name, view in self.cfg.views.items():
            if name not in known:
                continue
            columns, accepted = known[name]
            listed = _listing(columns)
            if view.chart is not None:
                for axis, col in [("x", view.chart.x)] + [
                    ("y", y) for y in view.chart.y_columns
                ]:
                    if col not in accepted:
                        self.issues.append(
                            Issue(
                                self._level(),
                                "chart-column",
                                f"ビュー '{name}' の chart.{axis} '{col}' は"
                                f"クエリ結果の列にありません(列: {listed}){note}",
                                _did_you_mean(col, columns),
                            )
                        )
            for kind, keys in (("styles", view.styles), ("labels", view.labels)):
                for col in keys:
                    if col not in accepted:
                        self.issues.append(
                            Issue(
                                "warning",
                                f"{kind[:-1]}-column",
                                f"ビュー '{name}' の {kind} の '{col}' はクエリ結果の"
                                f"列にないため効きません(列: {listed}){note}",
                                _did_you_mean(col, columns),
                            )
                        )
        for i, rule in enumerate(self.cfg.alerts):
            if rule.view not in known:
                continue
            columns, accepted = known[rule.view]
            if rule.column not in accepted:
                self.issues.append(
                    Issue(
                        self._level(),
                        "alert-column",
                        f"alerts[{i}] の列 '{rule.column}' はビュー '{rule.view}' の"
                        f"結果にありません。このアラートは発火しません"
                        f"(列: {_listing(columns)}){note}",
                        _did_you_mean(rule.column, columns),
                    )
                )

    def _check_kpi_formats(self) -> None:
        for name, card in self.cfg.kpis.items():
            try:
                card.format.format(1.5)
            except (ValueError, KeyError, IndexError, TypeError):
                self.issues.append(
                    Issue(
                        "warning",
                        "kpi-format",
                        f"KPI '{name}' の format '{card.format}' で値を整形できません"
                        "(値がそのまま表示されます)",
                        "例: '{:.0f}'(整数)・'{:.1f}'(小数 1 桁)・'{:,.0f}'(桁区切り)",
                    )
                )


# ---------------------------------------------------------------------------
# データ(CSV)
#
# 見出しと列の対応・文字コード・値の変換は、取り込み(CsvImporter)と同じ処理で調べる
# (CsvImporter.inspect_file)。点検だけの別の判定を持たないため、点検が OK と言った
# CSV が取り込みで行を落とすことがない(D7-01, D7-02)。
# ---------------------------------------------------------------------------
def _is_utf8_with_non_ascii(path: Path) -> bool:
    """ファイルが ASCII 以外を含む UTF-8 か(cp932 などとして読むと文字化けする)。"""
    from .settings.infer import FULL_SCAN_MAX_BYTES

    try:
        with path.open("rb") as fh:
            raw = fh.read(FULL_SCAN_MAX_BYTES)
    except OSError:
        return False
    if raw.isascii():
        return False
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        # 途中までしか読んでいないとき、末尾で切れた文字だけなら UTF-8 とみなす
        truncated = len(raw) == FULL_SCAN_MAX_BYTES
        return truncated and exc.start >= len(raw) - 3
    return True


def _file_identity(path: Path) -> tuple[int, int] | None:
    """同じファイルかを比べるための識別子(無ければ None)。"""
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_dev, stat.st_ino


def _visible(name: str) -> str:
    """見えない文字(BOM・制御文字)を \\uXXXX で表した列名(表示用)。"""
    return "".join(ch if ch.isprintable() else f"\\u{ord(ch):04x}" for ch in name)


def _line_numbers(lines: list[int]) -> str:
    shown = ", ".join(str(n) for n in lines[:_MAX_LISTED])
    if len(lines) > _MAX_LISTED:
        shown += f" ほか {len(lines) - _MAX_LISTED} 行"
    return shown + " 行目"


class _DataChecker:
    def __init__(self, cfg: MonitorConfig, settings: AppSettings) -> None:
        self.cfg = cfg
        self.settings = settings
        self.csv_dir = Path(settings.csv_dir)
        self.issues: list[Issue] = []

    def add(self, level: Level, code: str, message: str, hint: str | None = None):
        self.issues.append(Issue(level, code, message, hint))

    def check(self) -> list[Issue]:
        self._check_sources()
        source_tables = {s.table for s in self.cfg.sources.values()}
        if not self.csv_dir.is_dir():
            if set(self.cfg.tables) - source_tables:
                self.add(
                    "warning",
                    "csv-dir-missing",
                    f"CSV のフォルダ {self.csv_dir} がありません(取り込みは行われません)",
                    "MONITOR_CSV_DIR で CSV のフォルダを指定してください",
                )
            return self.issues
        importer = self._importer()
        for table, tdef in self.cfg.tables.items():
            path = self.csv_dir / f"{table}.csv"
            if not path.is_file():
                if table not in source_tables:
                    self.add(
                        "info",
                        "csv-missing",
                        f"{path} がないため、テーブル '{table}' には CSV を取り込みません",
                    )
                continue
            if importer is not None:
                self._check_csv(importer, table, tdef, path)
        self._check_unmatched()
        return self.issues

    def _importer(self):
        """取り込みと同じ設定の CsvImporter(文字コード名の誤りがあれば None)。

        文字コード名の誤りは _encoding_issues が報告する(CSV ごとには報告しない)。
        """
        from .db.registry import TableRegistry
        from .exceptions import CsvEncodingError
        from .services.importer import CsvImporter

        importer = CsvImporter(
            self.cfg,
            TableRegistry(self.cfg),
            None,  # 点検は DB に書き込まない
            self.csv_dir,
            self.settings.csv_encoding,
            encoding_fallback=self.settings.csv_encoding_fallback,
        )
        try:
            importer.encoding_candidates()
        except CsvEncodingError:
            return None
        return importer

    def _check_sources(self) -> None:
        for name, source in self.cfg.sources.items():
            path = Path(source.path).expanduser()
            if not path.exists():
                self.add(
                    "info",
                    "source-missing",
                    f"ソース '{name}' のファイル {source.path} がありません"
                    "(作られるまで取り込みは行われません)",
                )
                continue
            if source.kind == "json" and not source.mapping:
                self._check_json_keys(name, source, path)

    def _check_json_keys(self, name: str, source, path: Path) -> None:
        """追記の json ソースのレコードに主キーがあるか(無いと毎回入れ直す — D3-08)。"""
        import json

        from .services.rows import dot_get
        from .settings.infer import FULL_SCAN_MAX_BYTES

        if source.mode != "append" or source.table not in self.cfg.tables:
            return
        try:
            if path.stat().st_size > FULL_SCAN_MAX_BYTES:
                return
            data = json.loads(path.read_text(encoding="utf-8"))
            if source.record_path:
                data = dot_get(data, source.record_path)
        except (OSError, ValueError, KeyError):
            return  # 読めないファイルは取り込みが警告する
        records = [data] if isinstance(data, dict) else data
        if not isinstance(records, list):
            return
        pk = self.cfg.tables[source.table].primary_key
        sample = [r for r in records[:100] if isinstance(r, dict)]
        if sample and not any(r.get(pk) is not None for r in sample):
            self.issues.append(_reinsert_issue(name, pk))

    def _check_csv(self, importer, table, tdef, path: Path) -> None:
        from .exceptions import CsvEncodingError
        from .services import csv_encoding
        from .settings.infer import (
            FULL_SCAN_MAX_BYTES,
            SAMPLE_ROWS,
            header_looks_like_data,
            header_problem,
        )

        auto = csv_encoding.is_auto(self.settings.csv_encoding)
        try:
            with path.open("rb") as fh:
                head = fh.read(4)
            if head.startswith((b"\xff\xfe", b"\xfe\xff")):
                self.add(
                    "error",
                    "csv-unreadable",
                    f"{path.name}: UTF-16 のファイルは読み込めません"
                    "(Excel では「CSV UTF-8(コンマ区切り)」で保存してください)",
                )
                return
            limit = SAMPLE_ROWS if path.stat().st_size > FULL_SCAN_MAX_BYTES else None
            check = importer.inspect_file(
                table, path, limit=limit, database_url=self.settings.database_url
            )
        except CsvEncodingError as exc:
            hint = exc.hint
            if not auto:
                hint = (
                    f"MONITOR_CSV_ENCODING={self.settings.csv_encoding} を見直すか、"
                    "auto(自動判別)にしてください"
                )
            self.add("error", "csv-unreadable", exc.message, hint)
            return
        except OSError as exc:
            self.add("error", "csv-unreadable", f"{path.name}: 読み込めません({exc})")
            return
        plan = check.plan
        header = plan.header if plan is not None else None
        problem = header_problem(path.name, header)
        if problem is not None or plan is None:
            self.add(
                "error", "csv-unreadable", problem or f"{path.name}: 空のファイルです"
            )
            return
        encoding = check.encoding or ""
        if auto:
            if check.fallback:
                self.add(
                    "info",
                    "csv-encoding",
                    f"{path.name} は UTF-8 ではないため {encoding} として読み込みます",
                )
        elif not csv_encoding.is_utf8(encoding) and _is_utf8_with_non_ascii(path):
            self.add(
                "warning",
                "csv-encoding",
                f"{path.name} は UTF-8 ですが、MONITOR_CSV_ENCODING={encoding} の"
                "ため文字化けして取り込まれます",
                "MONITOR_CSV_ENCODING を auto(自動判別)にしてください",
            )
        if header_looks_like_data(cell.strip() for cell in header):
            self.add(
                "info",
                "csv-note",
                f"{path.name}: 1 行目が見出しではなくデータのようです"
                "(1 行目に列名を書いてください)",
            )
        if check.sampled:
            self.add(
                "info",
                "csv-note",
                f"{path.name}: ファイルが大きいため、先頭 {limit:,} 行のデータだけを"
                "点検しました",
            )
        self._check_header(table, tdef, path, plan)
        self._check_values(table, tdef, path, check)

    def _check_header(self, table, tdef, path: Path, plan) -> None:
        """見出しと宣言した列の対応(取り込みと同じ HeaderPlan)を報告する。"""
        declared = tdef.column_names
        for cell, col in plan.renamed.items():
            self.add(
                "info",
                "csv-header-variant",
                f"{path.name} の見出し '{_visible(cell)}' を列 '{col}' として取り込みます"
                "(大文字・小文字・空白・全角半角・BOM の違い)",
            )
        counts: dict[str, int] = {}
        for cell in plan.header:
            col = cell if cell in declared else plan.renamed.get(cell)
            if col is not None:
                counts[col] = counts.get(col, 0) + 1
        for col, count in counts.items():
            if count > 1:
                self.add(
                    "info",
                    "csv-note",
                    f"{path.name}: 列 '{col}' に当たる見出しが {count} 個あります"
                    "(後ろの列の値を取り込みます)",
                )
        unknown = [cell for cell in plan.unknown if cell.strip()]
        empty = len(plan.unknown) - len(unknown)
        if empty:
            self.add(
                "info",
                "csv-note",
                f"{path.name}: 見出しが空の列({empty} 列)は取り込みません",
            )
        for col in plan.missing:
            if col == tdef.primary_key and tdef.column_type(col) == "int":
                self.add(
                    "info",
                    "csv-header-missing",
                    f"{path.name} に主キーの列 '{col}' がありません"
                    "(取り込み時に自動採番します)",
                )
            elif col == tdef.primary_key:
                self.add(
                    "warning",
                    "csv-header-missing",
                    f"{path.name} に主キーの列 '{col}' がありません(整数ではない主キーは"
                    "自動採番されないため、取り込みに失敗するか主キーが空になります)",
                )
            else:
                self.add(
                    "warning",
                    "csv-header-missing",
                    f"{path.name} に列 '{col}' がありません"
                    "(置き換えの取り込みは中止され、既存の行を残します)",
                    _did_you_mean(col, unknown)
                    or f"CSV の見出し: {_listing([_visible(h) for h in plan.header])}",
                )
        for cell in unknown:
            self.add(
                "info",
                "csv-header-unknown",
                f"{path.name} の列 '{_visible(cell)}' は config.py のテーブル '{table}' に"
                "ないため取り込みません",
                _did_you_mean(cell.strip(), declared),
            )

    def _check_values(self, table, tdef, path: Path, check) -> None:
        """値の変換・DB の制限・主キーの重複・読めない行(取り込みと同じ判定)。"""
        from .services import csv_encoding

        name = path.name
        for col in tdef.column_names:
            kind = check.numeric.get(col)
            if kind is not None:
                self.add(
                    "warning",
                    "csv-numeric-as-str",
                    f"{table}.{col} は列名から str と推論されますが、データは数値です"
                    "(並べ替え・最大値・WHERE の比較が文字列として行われます)",
                    f'columns に {{"{col}": "{kind}"}} のように型を明示してください',
                )
                continue
            logical = tdef.column_type(col)
            if col in check.failures:
                line, value = check.failures[col]
                self.add(
                    "warning",
                    "csv-type-mismatch",
                    f"{name} の列 '{col}' に {logical} へ変換できない値があり、"
                    f"その行は取り込まれません(例: {line} 行目 '{value}')",
                    "データに合わせて columns の型を見直すか、CSV の値を直してください",
                )
            elif col in check.leading_zeros:
                line, value = check.leading_zeros[col]
                self.add(
                    "warning",
                    "csv-leading-zeros",
                    f"{name} の列 '{col}' は {logical} のため、取り込むと先頭の 0 が"
                    f"失われます(例: {line} 行目 '{value}')",
                    "番号・コードとして扱う列なら columns で str にしてください",
                )
            if col in check.too_large:
                line, reason = check.too_large[col]
                self.add(
                    "warning",
                    "csv-db-limit",
                    f"{name} {line} 行目: {reason}。この値の行は取り込まれません",
                    "SQLite では入りますが PostgreSQL / MySQL の列には入りません。"
                    "値を短く・小さくするか、列の型を見直してください",
                )
        if check.duplicate_key is not None:
            value, first, second = check.duplicate_key
            self.add(
                "error",
                "csv-duplicate-key",
                f"{name} の {second} 行目の主キー {tdef.primary_key}={value!r} が"
                f" {first} 行目と重複しています(置き換えの取り込みは失敗し、"
                "何も変わりません)",
                "主キーの値が重複しない CSV にしてください"
                "(追記 import-csv --keep では後の行を読み飛ばします)",
            )
        if check.undecodable:
            self.add(
                "warning",
                "csv-undecodable",
                f"{name} の {_line_numbers(check.undecodable)}は文字コード"
                f" {csv_encoding.label(check.encoding or '')} として読めないバイトを含む"
                "ため取り込まれません",
                "ファイルが壊れていないか確認してください",
            )
        if check.unparsable:
            self.add(
                "warning",
                "csv-unparsable",
                f"{name} の {_line_numbers(check.unparsable)}は CSV として読めないため"
                "取り込まれません",
                "引用符の閉じ忘れや、極端に長い値がないか確認してください",
            )

    def _check_unmatched(self) -> None:
        tables = list(self.cfg.tables)
        lowered = {t.casefold(): t for t in tables}
        # 取り込みが読むファイル(大文字・小文字を区別しないファイルシステムでは
        # DATA.CSV も DATA.csv として読まれる)
        imported = {_file_identity(self.csv_dir / f"{table}.csv") for table in tables}
        imported.discard(None)
        for path in sorted(self.csv_dir.iterdir()):
            if not path.is_file() or path.suffix.lower() != ".csv":
                continue
            if _file_identity(path) in imported:
                continue
            similar = lowered.get(path.stem.casefold())
            if similar is not None:
                hint = (
                    f"テーブル '{similar}' 用なら {similar}.csv に名前を変えてください"
                )
            else:
                hint = (
                    "monitor-app serve <CSV のフォルダ> --write-config new_config.py で"
                    "この CSV 用の設定の雛形を作れます"
                )
            self.add(
                "info",
                "csv-unmatched",
                f"{path.name} に対応するテーブルが config.py にないため取り込みません",
                hint,
            )


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def run_checks(
    cfg: MonitorConfig,
    settings: AppSettings,
    *,
    data: bool = False,
    env_files: Sequence[str | os.PathLike] | None = None,
) -> list[Issue]:
    """設定と実行環境を点検し、見つかった問題を重要度の順に返す。

    ``data=True`` なら CSV のフォルダも読む(大きな CSV では時間がかかる)。
    ``env_files`` は未知の ``MONITOR_*`` を探す .env(既定: 作業フォルダの .env)。
    """
    stages = [
        ("実行時設定", lambda: _settings_issues(settings, env_files)),
        ("通知", lambda: _notify_issues(cfg, settings)),
        ("ソース", lambda: _source_issues(cfg)),
        ("静的ファイル", lambda: _static_issues(cfg)),
        ("SQL", lambda: _SqlChecker(cfg, settings).check()),
    ]
    if data:
        stages.append(("CSV", lambda: _DataChecker(cfg, settings).check()))
    return _run_stages(stages)


def _run_stages(
    stages: Iterable[tuple[str, Callable[[], Iterable[Issue]]]],
) -> list[Issue]:
    """点検を順に行い、見つかった問題を重要度の順に返す。

    途中で失敗した点検は警告にして、残りの点検を続ける。
    """
    issues: list[Issue] = []
    for name, stage in stages:
        try:
            issues.extend(stage())
        except Exception as exc:  # noqa: BLE001 - 点検の失敗で他の点検を止めない
            issues.append(
                Issue(
                    "warning",
                    "check-failed",
                    f"{name}の点検を最後まで実行できませんでした:"
                    f" {type(exc).__name__}: {exc}",
                )
            )
    return sort_issues(issues)


def check_ui_settings(
    cfg: MonitorConfig, settings: AppSettings
) -> tuple[list[Issue], dict[str, list[str]]]:
    """画面で編集した設定の点検(ビュー・KPI の SQL の空実行、列の照合、通知先)。

    ``monitor-app check`` と同じ判定。戻り値は (重要度の順の問題, ビュー名 → 結果の
    列名)。列名は画面の入力欄の候補に使う。
    """
    checker = _SqlChecker(cfg, settings)
    issues = _run_stages(
        [("SQL", checker.check), ("通知", lambda: _notify_issues(cfg, settings))]
    )
    return issues, checker.view_columns


def check_project(
    config_path: str | Path,
    settings: AppSettings,
    *,
    data: bool = False,
    env_files: Sequence[str | os.PathLike] | None = None,
) -> tuple[MonitorConfig, list[Issue]]:
    """config.py を読み込み(未知のキーの警告を含めて)、:func:`run_checks` で点検する。

    config.py 自体の誤りは :class:`~monitor_app.settings.loader.ConfigError` を送出する。
    """
    from .settings.loader import load_config_with_warnings

    cfg, warnings = load_config_with_warnings(config_path)
    issues = [Issue("warning", "config-unknown-key", message) for message in warnings]
    issues.extend(run_checks(cfg, settings, data=data, env_files=env_files))
    return cfg, sort_issues(issues)
