"""SQLAlchemy エンジンとコネクション管理。

ファイルの SQLite のチューニング(PRAGMA)と保存先フォルダの作成、サーバー DB
(PostgreSQL / MySQL)のプール設定と起動時の待機、ログに出せる接続先の説明を担う。
"""

from __future__ import annotations

import logging
import math
import os
import signal
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import Connection, create_engine, event, text
from sqlalchemy.engine import URL, Engine, make_url
from sqlalchemy.exc import ArgumentError, DBAPIError, OperationalError
from sqlalchemy.pool import StaticPool

from ..cancel import raise_if_stop_requested, stop_requested
from ..settings.runtime import SQLITE_JOURNAL_MODES, SQLITE_SYNCHRONOUS_MODES
from .errors import driver_message, is_permanent_connect_error

if TYPE_CHECKING:
    from ..settings.runtime import AppSettings

logger = logging.getLogger("monitor_app.db")

#: 読み取り専用にする SQL(方言名 → (開始時, 終了時))。URL の接頭辞ではなく
#: エンジンの方言名で選ぶため、``mariadb+pymysql://`` なども漏れなく対象になる。
#: SQLite の PRAGMA はコネクション単位なので終了時に解除する。トランザクション
#: 単位の ``SET TRANSACTION READ ONLY`` は解除不要。
_READONLY_SQL: dict[str, tuple[str, str | None]] = {
    "sqlite": ("PRAGMA query_only = ON", "PRAGMA query_only = OFF"),
    "postgresql": ("SET TRANSACTION READ ONLY", None),
    "mysql": ("SET TRANSACTION READ ONLY", None),
    "mariadb": ("SET TRANSACTION READ ONLY", None),
}

#: WAL が正しく動かないファイルシステム(Linux の /proc/self/mountinfo の型名)。
#: WAL は共有メモリ(-shm)とロックが同じホスト上で働くことを前提にするため、
#: NFS/SMB などのネットワーク共有や、Docker Desktop・WSL のホスト共有フォルダ
#: (9p / virtiofs など)では journal_mode=delete を使う(PERF-02)。
_NETWORK_FS_TYPES = frozenset(
    {
        "nfs",
        "nfs4",
        "cifs",
        "smb3",
        "smbfs",
        "9p",
        "afs",
        "ncpfs",
        "davfs",
        "virtiofs",
        "fakeowner",
        "osxfs",
        "fuse.grpcfuse",
        "fuse.sshfs",
        "fuse.rclone",
        "fuse.s3fs",
        "glusterfs",
        "fuse.glusterfs",
        "ceph",
        "fuse.ceph",
        "lustre",
        "gpfs",
    }
)
#: クエリ文字列のうち、値を伏せるキー(接続 URL をログに出すとき)。``?password=`` や
#: ``?passwd=``(PyMySQL)・``?pwd=``(ODBC)のように、URL のパスワード欄以外でも秘密を
#: 渡せるため。
_SECRET_QUERY_KEYS = ("pass", "pwd", "secret", "token", "key")
#: メモリ上の SQLite の説明(ログ・起動時の表示)。
MEMORY_DESCRIPTION = "SQLite(メモリ上。終了すると消えます)"
#: サーバー DB へ 1 回接続するときの待ちの上限(秒)の既定。応答の無いホスト(パケットを
#: 捨てるファイアウォールなど)では、ドライバの既定(無制限・OS の TCP の再送まかせ)の
#: ままだと 1 回の接続に 20 秒〜2 分かかり、起動待ち(MONITOR_DB_WAIT_TIMEOUT)や
#: リクエストを処理するスレッドがその間止まる。
DEFAULT_CONNECT_TIMEOUT = 10.0
#: ドライバ名 → 接続の待ちの上限を渡す引数名(と、整数で渡すか)。どれも接続の確立
#: だけに効く(pg8000 はソケット全体に効くため、接続後に :func:`_pg8000_no_read_timeout`
#: で外す。長いクエリを時間切れにしない)。
_CONNECT_TIMEOUT_ARGS: dict[str, tuple[str, bool]] = {
    "psycopg": ("connect_timeout", True),
    "psycopg2": ("connect_timeout", True),
    "pg8000": ("timeout", False),
    "pymysql": ("connect_timeout", True),
    "mysqldb": ("connect_timeout", True),
    "mariadbconnector": ("connect_timeout", True),
}
#: 起動待ちの残り時間(:meth:`Database.wait_until_ready` が設定する。スレッドごと)。
_wait_deadline = threading.local()
#: 起動待ちを打ち切る停止のシグナル(uvicorn が受けるものと同じ)。
_STOP_SIGNALS = tuple(
    sig
    for sig in (
        getattr(signal, name, None) for name in ("SIGINT", "SIGTERM", "SIGBREAK")
    )
    if sig is not None
)


class DatabaseWaitAborted(Exception):
    """データベースの起動待ちを、停止のシグナル(SIGTERM・SIGINT)で打ち切った。"""

    def __init__(self, signum: int) -> None:
        super().__init__(
            "停止の指示を受けたため、データベースの起動待ちを中止しました"
            f"(シグナル {signum})"
        )
        self.signum = signum


@contextmanager
def _stop_signal_watch() -> Iterator[None]:
    """このブロックの間、停止のシグナルで :class:`DatabaseWaitAborted` を送出する。

    uvicorn はアプリの読み込み中(``build_services`` の起動待ちはここで動く)に受けた
    SIGTERM を記録するだけで、読み込みが終わるまで停止の処理に移らない。起動待ちの
    間だけハンドラを重ね、元のハンドラ(uvicorn の記録など)を呼んでから待ちを打ち切る。
    元のハンドラが例外を送出する場合(CLI の起動の準備中・Ctrl+C の KeyboardInterrupt)
    はその例外で抜ける。Python のハンドラが無い(既定の動作・無視)シグナルには重ねない。
    メインスレッド以外では何もしない(シグナルのハンドラはメインスレッドでしか設定
    できない)。
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    installed: dict = {}

    def handler(signum: int, frame: Any) -> None:
        previous = installed[signum]
        previous(signum, frame)  # uvicorn の記録など(例外を送出すればそれで抜ける)
        raise DatabaseWaitAborted(signum)

    for sig in _STOP_SIGNALS:
        previous = signal.getsignal(sig)
        if callable(previous):
            installed[sig] = previous
            signal.signal(sig, handler)
    try:
        yield
    finally:
        for sig, previous in installed.items():
            signal.signal(sig, previous)


# ---------------------------------------------------------------------------
# ネットワーク共有の判定(WAL を避ける)
# ---------------------------------------------------------------------------
def _is_unc_path(path: str) -> bool:
    r"""``\\server\share\...`` / ``//server/share/...`` 形式のパスか。

    Windows の拡張パス(``\\?\C:\...``)はローカル。``\\?\UNC\server\...`` は共有。
    """
    if path.startswith(("\\\\?\\", "\\\\.\\", "//?/", "//./")):
        return path[4:].upper().startswith(("UNC\\", "UNC/"))
    for sep in ("\\", "/"):
        if path.startswith(sep * 2) and not path.startswith(sep * 3):
            return True
    return False


def _unescape_mount(field: str) -> str:
    # mountinfo は空白などを 8 進エスケープ(\040)で書く。
    return field.encode("latin-1", "backslashreplace").decode("unicode_escape")


def _linux_fs_type(path: str, mountinfo: str | None = None) -> str | None:
    """``path`` を含むマウントのファイルシステム型(Linux 以外・不明なら None)。"""
    if mountinfo is None:
        try:
            with open("/proc/self/mountinfo", encoding="utf-8") as fh:
                mountinfo = fh.read()
        except OSError:
            return None
    target = os.path.realpath(path)
    best_point, best_type = "", None
    for line in mountinfo.splitlines():
        fields = line.split()
        if "-" not in fields or len(fields) < 5:
            continue
        point = _unescape_mount(fields[4])
        sep = fields.index("-")
        if sep + 1 >= len(fields):
            continue
        prefix = point.rstrip("/") + "/"
        inside = target == point or target.startswith(prefix)
        # 同じ場所に重ねてマウントされていれば後の行(上に見えている方)を使う
        if inside and len(point) >= len(best_point):
            best_point, best_type = point, fields[sep + 1]
    return best_type


def _windows_drive_is_remote(path: str) -> bool:
    """Windows の割り当て済みネットワークドライブ(例: ``Z:\\``)上のパスか。"""
    if os.name != "nt":  # pragma: no cover - Windows 専用
        return False
    try:  # pragma: no cover - Windows 専用
        import ctypes

        drive = os.path.splitdrive(os.path.abspath(path))[0]
        if not drive:
            return False
        drive_remote = 4  # GetDriveTypeW の DRIVE_REMOTE
        return ctypes.windll.kernel32.GetDriveTypeW(drive + "\\") == drive_remote
    except Exception:  # noqa: BLE001  # pragma: no cover - 判定できなければ通常扱い
        return False


def is_network_path(path: str, *, mountinfo: str | None = None) -> bool:
    """SQLite の DB ファイルの置き場所がネットワーク共有(WAL 非対応)か。"""
    if _is_unc_path(path):
        return True
    if os.name == "nt":  # pragma: no cover - Windows 専用
        return _windows_drive_is_remote(path)
    fs_type = _linux_fs_type(os.path.dirname(os.path.abspath(path)), mountinfo)
    return fs_type in _NETWORK_FS_TYPES


# ---------------------------------------------------------------------------
# URL の解析
# ---------------------------------------------------------------------------
#: 解釈できない接続 URL のエラー文(URL はパスワードを含み得るため入れない)。
BAD_URL_MESSAGE = (
    "接続 URL(MONITOR_DATABASE_URL)を解釈できません(パスワードに @ : / などの記号を"
    "含むときは、@ → %40 のように URL エンコードしてください)"
)


def parse_url(url: str | URL) -> URL:
    """接続 URL を解釈する(:func:`sqlalchemy.engine.make_url` と同じ結果)。

    解釈できない URL は、URL の中身を含まない :class:`~sqlalchemy.exc.ArgumentError`
    にする(SQLAlchemy の例外の文言には、パスワードやその一部がそのまま入ることがある)。
    パスワードの ``@`` を URL エンコードしていない URL も、``@`` の後ろがホスト名として
    読まれ、パスワードの一部がログや接続エラーに出るため、解釈できない URL として扱う。
    """
    try:
        parsed = make_url(url)
    except (ArgumentError, ValueError):  # ポートを数値にできない、など
        raise ArgumentError(BAD_URL_MESSAGE) from None
    if "@" in (parsed.host or ""):
        raise ArgumentError(BAD_URL_MESSAGE)
    return parsed


def _is_sqlite_uri(url: URL) -> bool:
    database = url.database or ""
    return database.startswith("file:") and str(url.query.get("uri", "")).lower() in (
        "true",
        "1",
    )


def _masked_url(url: URL) -> str:
    """パスワード(とクエリ中の秘密らしき値)を伏せた接続 URL(表示用)。"""
    base = url.set(query={}).render_as_string(hide_password=True)
    parts = []
    for key, value in sorted(url.query.items()):
        for item in value if isinstance(value, tuple) else (value,):
            secret = any(s in key.lower() for s in _SECRET_QUERY_KEYS)
            parts.append(f"{key}={'***' if secret else item}")
    return f"{base}?{'&'.join(parts)}" if parts else base


def is_memory_url(url: URL) -> bool:
    """SQLite の URL がメモリ上の DB を指すか(``sqlite://``・``:memory:``・``mode=memory``)。

    メモリ上の DB は接続ごとに別の DB になるため、:class:`Database` は StaticPool で
    1 つの接続を共有する。check・migrate・backup も同じ判定を使う。
    """
    database = url.database or ""
    return not database or ":memory:" in database or url.query.get("mode") == "memory"


def describe_url(url: str) -> str:
    """接続 URL を、ログに出してよい 1 行の説明にする(:meth:`Database.describe` と同じ)。

    SQLite はファイルの絶対パス(作業ディレクトリ基準。``~`` はホームフォルダ)、
    サーバー DB はパスワードとクエリ中の秘密らしき値を ``***`` に伏せた URL。
    エンジンを作らないため、DB を開く前(起動時の表示など)にも使える。解釈できない
    URL は、中身(パスワードを含み得る)を出さずにその旨だけを返す。
    """
    try:
        parsed = parse_url(url)
    except ArgumentError:
        return "(解釈できない接続 URL。MONITOR_DATABASE_URL を確認してください)"
    if parsed.get_backend_name() != "sqlite":
        return _masked_url(parsed)
    if is_memory_url(parsed):
        return MEMORY_DESCRIPTION
    database = parsed.database or ""
    if _is_sqlite_uri(parsed):
        return f"SQLite {database}"
    return f"SQLite {Path(os.path.abspath(os.path.expanduser(database)))}"


class Database:
    """接続 URL を受け取り、エンジンとコネクションを供給する薄いラッパ。

    monitor-app は同期 SQLAlchemy を使う。FastAPI のスレッドプール上で
    十分な性能が出るうえ、非同期 DB ドライバよりデバッグが容易なため。

    ファイルの SQLite には接続ごとに PRAGMA を設定する(PERF-02): 既定の
    ``journal_mode=wal`` は読み取りと書き込みが互いを待たず、書き込みごとの fsync も
    大幅に減る。``synchronous=normal`` は WAL のときだけ使い(電源断で直近のコミットが
    失われ得るが DB は壊れない)、WAL 以外では SQLite 既定の ``full`` にする。
    インメモリ DB には何も設定しない(従来どおり StaticPool の単一コネクション)。
    """

    def __init__(
        self,
        url: str,
        echo: bool = False,
        *,
        sqlite_journal_mode: str = "wal",
        sqlite_synchronous: str = "normal",
        sqlite_busy_timeout_ms: int = 5000,
        connect_timeout: float | None = DEFAULT_CONNECT_TIMEOUT,
    ) -> None:
        self.url = url
        parsed = parse_url(url)
        #: バックエンド名(sqlite / postgresql / mysql / mariadb など)。
        self.backend: str = parsed.get_backend_name()
        #: インメモリ SQLite か(StaticPool の単一コネクションがデータベースそのもの)。
        self.is_memory: bool = False
        #: 実際に有効になった SQLite の journal_mode(最初の接続で確定する)。
        self.journal_mode: str | None = None
        #: SQLite のロック待ちの上限(ミリ秒)。バックアップ用の接続にも使う。
        self.sqlite_busy_timeout_ms = int(sqlite_busy_timeout_ms)
        self._sqlite_file: str | None = None
        #: ファイルの SQLite の PRAGMA(busy_timeout, journal_mode, synchronous)。
        self._pragmas: tuple[int, str | None, str] | None = None
        self._journal_warned = False

        connect_args: dict[str, Any] = {}
        engine_kwargs: dict[str, Any] = {}
        if self.backend == "sqlite":
            # FastAPI は複数スレッドからアクセスするため。
            connect_args["check_same_thread"] = False
            database = parsed.database or ""
            if is_memory_url(parsed):
                # インメモリ DB は接続ごとに別物になる。単一コネクションを
                # 共有する StaticPool で全アクセスから同じ DB を見えるようにする。
                engine_kwargs["poolclass"] = StaticPool
                self.is_memory = True
            else:
                if not _is_sqlite_uri(parsed):
                    if database.startswith("~"):
                        # 親フォルダの作成と実際に開く場所を一致させる(SQLite 自身は ~ を
                        # 展開しない)。
                        database = os.path.expanduser(database)
                        parsed = parsed.set(database=database)
                    self._sqlite_file = database
                    _make_parent_dir(database)
                self._install_sqlite_pragmas(
                    parsed,
                    sqlite_journal_mode,
                    sqlite_synchronous,
                    sqlite_busy_timeout_ms,
                )
        else:
            # サーバー DB: 切れたコネクション(DB の再起動・アイドル切断)をプールから
            # 渡さないよう、貸し出し時に生存確認し、1 時間で作り直す(CRIT-01)。
            engine_kwargs["pool_pre_ping"] = True
            engine_kwargs["pool_recycle"] = 3600
        self._url_obj = parsed
        self.engine: Engine = create_engine(
            parsed, echo=echo, future=True, connect_args=connect_args, **engine_kwargs
        )
        if self._pragmas is not None:
            event.listen(self.engine, "connect", self._on_sqlite_connect)
        elif self.backend in ("mysql", "mariadb"):
            event.listen(self.engine, "connect", _mysql_session_setup)
        #: サーバー DB へ 1 回接続するときの待ちの上限(秒。None はドライバの既定)。
        self.connect_timeout = connect_timeout if self.backend != "sqlite" else None
        timeout_arg = _CONNECT_TIMEOUT_ARGS.get(self.engine.dialect.driver)
        if self.connect_timeout is not None and timeout_arg is not None:
            event.listen(
                self.engine,
                "do_connect",
                _connect_timeout_setter(*timeout_arg, self.connect_timeout),
            )
            if self.engine.dialect.driver == "pg8000":
                event.listen(self.engine, "connect", _pg8000_no_read_timeout)
        if not self.is_memory:
            # 停止の指示を受けたバックグラウンド処理(取り込み・保持期間の削除・
            # アラート評価)を次の SQL で打ち切る。インメモリ DB は除く: 打ち切りで
            # 単一のコネクションが破棄されると、データベースごと消えるため。
            event.listen(self.engine, "before_cursor_execute", _abort_if_stopping)

    @classmethod
    def from_settings(cls, settings: AppSettings, *, echo: bool = False) -> Database:
        """実行時設定(``MONITOR_DATABASE_URL`` と SQLite のチューニング)から作る。"""
        return cls(
            settings.database_url,
            echo=echo,
            sqlite_journal_mode=settings.sqlite_journal_mode,
            sqlite_synchronous=settings.sqlite_synchronous,
            sqlite_busy_timeout_ms=settings.sqlite_busy_timeout_ms,
        )

    # ------------------------------------------------------------------
    # SQLite の PRAGMA
    # ------------------------------------------------------------------
    def _install_sqlite_pragmas(
        self, url: URL, journal_mode: str, synchronous: str, busy_timeout_ms: int
    ) -> None:
        journal_mode = journal_mode.strip().lower()
        synchronous = synchronous.strip().lower()
        if journal_mode not in SQLITE_JOURNAL_MODES:
            raise ValueError(
                f"sqlite_journal_mode は {', '.join(SQLITE_JOURNAL_MODES)} のいずれかです"
            )
        if synchronous not in SQLITE_SYNCHRONOUS_MODES:
            raise ValueError(
                "sqlite_synchronous は "
                f"{', '.join(SQLITE_SYNCHRONOUS_MODES)} のいずれかです"
            )
        wanted: str | None = journal_mode
        database = url.database or ""
        if _is_sqlite_uri(url):
            if url.query.get("mode") == "ro":
                wanted = None  # 読み取り専用で開く DB のジャーナルは変えない
        elif journal_mode == "wal" and is_network_path(database):
            logger.info(
                "SQLite の DB がネットワーク共有上にあるため journal_mode=delete を"
                "使います: %s",
                database,
            )
            wanted = "delete"
        self._pragmas = (int(busy_timeout_ms), wanted, synchronous)

    def _on_sqlite_connect(self, dbapi_conn: Any, _record: Any) -> None:
        """新しい DBAPI コネクションごとに PRAGMA を設定する。

        journal_mode は DB ファイルに保存される設定で、設定済みなら即座に返る。
        temp_store は計測で遅くなったため設定しない。
        """
        assert self._pragmas is not None
        busy_timeout_ms, wanted, synchronous = self._pragmas
        cur = dbapi_conn.cursor()
        try:
            cur.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
            mode = self._apply_journal_mode(cur, wanted)
            if synchronous == "normal" and mode != "wal":
                # normal が安全なのは WAL のときだけ(rollback ジャーナルでは電源断で
                # DB が壊れ得る)。WAL にならなかったら SQLite 既定の full に戻す。
                synchronous = "full"
            cur.execute(f"PRAGMA synchronous = {synchronous}")
        finally:
            cur.close()

    def _apply_journal_mode(self, cur: Any, wanted: str | None) -> str | None:
        """journal_mode を設定し、実際に有効なモードを返す。

        設定できなくても(読み取り専用の場所・別プロセスのロックなど)接続はそのまま
        使い、警告を 1 回だけ出す。``wanted`` が None なら今のモードを読むだけ。
        """
        reason = ""
        try:
            if wanted is None:
                cur.execute("PRAGMA journal_mode")
            else:
                cur.execute(f"PRAGMA journal_mode = {wanted}")
            actual = _fetch_mode(cur)
        except Exception as exc:  # noqa: BLE001 - 設定できなくても接続は使う
            reason = str(exc)
            try:
                # 今のモードを読むだけなら読み取り専用の DB でもできる
                cur.execute("PRAGMA journal_mode")
                actual = _fetch_mode(cur)
            except Exception:  # noqa: BLE001
                actual = None
        if wanted is not None and actual != wanted:
            detail = (actual or "不明") + (f"、{reason}" if reason else "")
            if wanted == "wal" and "database is locked" in reason:
                # ほかのプロセスが使用中(切り替えには排他ロックが要る)。次に誰も
                # 使っていないときに開けば WAL になる。
                self._warn_journal_once(
                    "SQLite を WAL モードにできませんでした(実際のモード: %s)。"
                    "ほかのプロセス(起動中のサーバーなど)がデータベースを使用中です。"
                    "そのプロセスを止めてから開き直すと WAL になります",
                    detail,
                )
            elif wanted == "wal" and "readonly" in reason:
                self._warn_journal_once(
                    "SQLite を WAL モードにできませんでした(実際のモード: %s)。"
                    "データベースファイルかその保存先フォルダに書き込めません"
                    "(所有者と書き込みの権限を確認してください)",
                    detail,
                )
            elif wanted == "wal":
                self._warn_journal_once(
                    "SQLite を WAL モードにできませんでした(実際のモード: %s)。"
                    "ネットワーク共有や読み取り専用の場所に DB がある場合は "
                    "MONITOR_SQLITE_JOURNAL_MODE=delete を指定してください",
                    detail,
                )
            else:
                self._warn_journal_once(
                    "SQLite の journal_mode=%s を設定できませんでした(実際のモード: %s)",
                    wanted,
                    detail,
                )
        self.journal_mode = actual
        return actual

    def _warn_journal_once(self, message: str, *args: Any) -> None:
        if not self._journal_warned:
            self._journal_warned = True
            logger.warning(message, *args)

    # ------------------------------------------------------------------
    # 情報
    # ------------------------------------------------------------------
    @property
    def dialect(self) -> str:
        """エンジンの方言名(sqlite / postgresql / mysql / mariadb など)。"""
        return self.engine.dialect.name

    @property
    def sqlite_path(self) -> Path | None:
        """ファイルの SQLite なら DB ファイルの絶対パス(それ以外は None)。"""
        if self._sqlite_file is None:
            return None
        return Path(os.path.abspath(self._sqlite_file))

    def describe(self) -> str:
        """ログに出してよい 1 行の説明(パスワードは伏せる)。

        SQLite はファイルの絶対パス(相対パスの URL がどこを指しているかを示す)、
        サーバー DB はパスワードを伏せた接続 URL。
        """
        if self.backend == "sqlite":
            if self.is_memory:
                return MEMORY_DESCRIPTION
            path = self.sqlite_path
            return f"SQLite {path if path is not None else self._url_obj.database}"
        return _masked_url(self._url_obj)

    # ------------------------------------------------------------------
    # コネクション
    # ------------------------------------------------------------------
    @contextmanager
    def connect(self) -> Iterator[Connection]:
        """書き込み可能なトランザクション付きコネクション。"""
        with self.engine.begin() as conn:
            yield conn

    @contextmanager
    def write_transaction(self) -> Iterator[Connection]:
        """書き込み用のトランザクション。途中で SAVEPOINT(``begin_nested``)を使える。

        SQLite のドライバ(pysqlite)は最初の INSERT/UPDATE/DELETE の直前まで BEGIN を
        出さない。それより前に SAVEPOINT を出すとトランザクションの外で始まり、その
        RELEASE が確定(COMMIT)になってしまう(外側を取り消しても戻らない)。そこで
        SQLite では最初に ``BEGIN IMMEDIATE`` を出す(書き込みロックを先に取るので、
        読んでから書くときにほかの書き込みと競合して失敗することもない)。
        """
        with self.engine.begin() as conn:
            if self.backend == "sqlite":
                raw = conn.connection.dbapi_connection
                if raw is not None and not getattr(raw, "in_transaction", True):
                    conn.exec_driver_sql("BEGIN IMMEDIATE")
            yield conn

    @contextmanager
    def readonly(self) -> Iterator[Connection]:
        """読み取り専用コネクション。ビューの生 SQL 実行で使う多重防御。

        SQLite の ``PRAGMA query_only`` はコネクション単位で、コネクションは
        プールに再利用される。終了時に必ず解除し、後続の書き込みを汚染しない。
        ``_READONLY_SQL`` に無い方言の DB では追加の制限をかけない。
        """
        begin_sql, end_sql = _READONLY_SQL.get(self.engine.dialect.name, (None, None))
        with self.engine.connect() as conn:
            if begin_sql:
                conn.execute(text(begin_sql))
            try:
                yield conn
            finally:
                # 破棄済み(処理の打ち切りなど)のコネクションはプールへ戻らないため解除不要。
                if end_sql and not conn.invalidated:
                    conn.execute(text(end_sql))

    def wait_until_ready(
        self,
        timeout: float,
        *,
        initial_delay: float = 0.5,
        max_delay: float = 5.0,
    ) -> None:
        """``SELECT 1`` が通るまで、間隔を伸ばしながら最大 ``timeout`` 秒待つ。

        Docker Compose などで DB より先にアプリが起動した場合に備える(CRIT-01)。
        認証の失敗や DB 名の誤りのように待っても直らないものは待たずに、時間切れなら
        最後のエラーをそのまま送出する(CLI のエラー層が短く表示する)。

        待っている間に停止のシグナル(``docker stop`` の SIGTERM・Ctrl+C)を受けたら、
        待ちを打ち切って :class:`DatabaseWaitAborted` を送出する(メインスレッドで
        呼んだとき。uvicorn の起動中でも猶予時間を待たずに終われる)。
        """
        started = time.monotonic()
        deadline = started + max(timeout, 0.0)
        delay = initial_delay
        waited = False
        with _stop_signal_watch():
            while True:
                try:
                    # 1 回の接続の待ちも残り時間までにする(応答の無いホストでも超えない)
                    _wait_deadline.value = deadline
                    try:
                        with self.engine.connect() as conn:
                            conn.execute(text("SELECT 1"))
                    finally:
                        _wait_deadline.value = None
                except (DBAPIError, TimeoutError) as caught:
                    # pg8000 は応答待ちの時間切れを DBAPI の例外にせず TimeoutError の
                    # まま送出する。ほかの接続の失敗と同じく扱う(CLI のエラー層が短く表示)。
                    exc = (
                        caught
                        if isinstance(caught, DBAPIError)
                        else OperationalError("SELECT 1", None, caught)
                    )
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or is_permanent_connect_error(exc):
                        if exc is caught:
                            raise
                        raise exc from caught
                    pause = min(delay, remaining)
                    logger.info(
                        "データベースの起動を待っています(%s): %s — %.1f 秒後に再試行します",
                        self.describe(),
                        driver_message(exc),
                        pause,
                    )
                    waited = True
                    # 停止のシグナルを受けると、ハンドラの例外で sleep から抜ける
                    time.sleep(pause)
                    delay = min(delay * 2, max_delay)
                    continue
                if waited:
                    logger.info(
                        "データベースに接続しました(%.1f 秒待機)",
                        time.monotonic() - started,
                    )
                return

    def dispose(self) -> None:
        self.engine.dispose()


#: 停止の指示があっても実行する SQL(トランザクションの取り消し・確定の制御文)。
_CONTROL_SQL = ("ROLLBACK", "RELEASE", "SAVEPOINT", "COMMIT", "END")


def _abort_if_stopping(
    _conn: Any,
    _cursor: Any,
    statement: str,
    _parameters: Any,
    _context: Any,
    _executemany: bool,
) -> None:
    """停止の指示を受けたバックグラウンド処理なら、SQL を実行せずに打ち切る。

    :func:`~monitor_app.cancel.stop_scope` の外(リクエストの処理など)では何もしない。
    書きかけのトランザクションは取り消される。後片付けの制御文は通す。
    """
    if stop_requested() and not statement.lstrip()[:9].upper().startswith(_CONTROL_SQL):
        raise_if_stop_requested()


def _connect_timeout_setter(name: str, integer: bool, default: float):
    """接続のたびに、ドライバへ接続の待ちの上限を渡す ``do_connect`` のリスナー。

    URL で指定済み(``?connect_timeout=5`` など)ならその値を使う。起動待ちの間は、
    残り時間より長く待たない。
    """

    def set_timeout(_dialect: Any, _record: Any, _cargs: Any, cparams: dict) -> None:
        try:
            value = float(cparams.get(name, default))
        except (TypeError, ValueError):
            return  # 解釈できない指定はドライバに任せる
        deadline = getattr(_wait_deadline, "value", None)
        if deadline is not None:
            value = min(value, max(deadline - time.monotonic(), 1.0))
        cparams[name] = max(math.ceil(value), 1) if integer else max(value, 0.5)

    return set_timeout


def _pg8000_no_read_timeout(dbapi_conn: Any, _record: Any) -> None:
    """pg8000 の接続の待ちの上限(``timeout``)を、接続できた後は外す。

    pg8000 はこの値をソケットのすべての読み書きに使い続けるため、そのままでは
    ``timeout`` 秒より長いクエリ(大きな集計・取り込み)が時間切れで失敗する。
    """
    sock = getattr(dbapi_conn, "_usock", None)
    settimeout = getattr(sock, "settimeout", None)
    if callable(settimeout):
        settimeout(None)


def _fetch_mode(cur: Any) -> str | None:
    row = cur.fetchone()
    return str(row[0]).lower() if row and row[0] is not None else None


def _mysql_session_setup(dbapi_conn: Any, _record: Any) -> None:
    """MySQL のセッションで、明示した主キー 0 を採番に置き換えない(CRIT-01)。

    MySQL は AUTO_INCREMENT 列への 0 を「次の番号を採番」と解釈し、id=0 の行を
    取り込むと別の id になって後続の行と衝突する。SQLite / PostgreSQL と同じく 0 を
    そのまま保存させる(NULL は従来どおり採番する)。
    """
    cur = dbapi_conn.cursor()
    try:
        cur.execute(
            "SET SESSION sql_mode = CONCAT_WS(',', NULLIF(@@SESSION.sql_mode, ''),"
            " 'NO_AUTO_VALUE_ON_ZERO')"
        )
    finally:
        cur.close()


def _make_parent_dir(database: str) -> None:
    """SQLite の DB ファイルを置くフォルダを作る(DX-02)。

    作れなかった場合(権限なしなど)は警告だけ出し、続く接続で SQLite 自身の
    「unable to open database file」に任せる(CLI のエラー層が案内を表示する)。
    """
    parent = Path(database).parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("SQLite の保存先フォルダを作成できません: %s (%s)", parent, exc)
