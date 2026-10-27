"""データの変更を検知するトークン(ビュー共有キャッシュの有効性判定に使う)。

ファイルの SQLite では、書き込みを一切しない専用の sqlite3 コネクションで
``PRAGMA data_version`` を読む。この値は「ほかのコネクションがコミットしたとき」に
変わる。同じプロセスの API・取り込みはもちろん、``monitor-app sync-sources`` の
cron など別プロセスの書き込みも検知でき、1 回あたり数マイクロ秒と安い。

PostgreSQL / MySQL / インメモリ SQLite には相当する安価な仕組みがないため、
:func:`change_token_for` は None を返す(キャッシュは従来どおり TTL で鮮度を保つ)。
"""

from __future__ import annotations

import contextlib
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy.pool import StaticPool

if TYPE_CHECKING:
    from .engine import Database

logger = logging.getLogger("monitor_app.views")

#: ロック待ちの上限(秒)。ロールバックジャーナルで書き込み中に読めなければ、
#: 待たずに「不明」として扱う(キャッシュは TTL で判定する)。待つと、キャッシュを
#: 参照する全スレッドがこのコネクションのロックの前に並び、大きな取り込みの間は
#: キャッシュのヒットでさえ (スレッド数 × 待ち時間) だけ遅れるため、0 にしている。
_BUSY_TIMEOUT = 0.0
#: DB ファイルを開けなかったとき、次に開き直すまでの間隔(秒)。
_RETRY_INTERVAL = 1.0
#: 一時的なエラー(ロック中)を表す SQLite のエラーコード(SQLITE_BUSY / SQLITE_LOCKED)。
_TRANSIENT_CODES = (5, 6)


class SqliteDataVersion:
    """ファイル SQLite の ``PRAGMA data_version`` を返す変更トークン。

    コネクションは初回の :meth:`current` で開き(DB ファイルがまだ無ければ作らない)、
    ``PRAGMA query_only`` で書き込みを禁止する。複数スレッドから呼べるようロックで守る。
    """

    def __init__(self, filename: str, *, uri: bool = False) -> None:
        self.filename = filename
        self._uri = uri
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._next_open = 0.0
        #: ロック中で読めなかった回数。待っている間に増えたら、並んで読み直さない。
        self._busy_count = 0

    def _open(self) -> sqlite3.Connection:
        if self._uri:
            target, uri = self.filename, True
        else:
            # mode=rw: 読み書き可能なら読み書き(WAL の共有メモリを扱える)で開くが、
            # ファイルが無ければ作らない。
            target, uri = Path(self.filename).as_uri() + "?mode=rw", True
        conn = sqlite3.connect(
            target,
            uri=uri,
            timeout=_BUSY_TIMEOUT,
            check_same_thread=False,
            isolation_level=None,  # 自動コミット(読み取りトランザクションを残さない)
        )
        try:
            conn.execute("PRAGMA query_only = 1")
        except sqlite3.Error:
            conn.close()
            raise
        return conn

    def current(self) -> int | None:
        """現在のトークン。読めないとき(ファイルが無い・ロック中など)は None。"""
        busy_count = self._busy_count
        with self._lock:
            if self._busy_count != busy_count:
                # 待っている間に、ほかのスレッドがロック中で読めなかった。Windows の
                # SQLite はロックを取れないと Sleep(1)(実際には約 16 ms)を挟んで取り直す
                # ため 1 回の読み取りに数十 ms かかり、順に読み直すと全スレッドが
                # (スレッド数 × 数十 ms) 遅れる。並ばずに「不明」を返す。
                return None
            conn = self._conn
            if conn is None:
                now = time.monotonic()
                if now < self._next_open:
                    return None
                try:
                    conn = self._open()
                except sqlite3.Error:
                    self._next_open = now + _RETRY_INTERVAL
                    return None
                self._conn = conn
            try:
                cursor = conn.execute("PRAGMA data_version")
                try:
                    row = cursor.fetchone()
                finally:
                    cursor.close()
            except sqlite3.Error as exc:
                if _is_transient(exc):
                    self._busy_count += 1
                else:
                    # 壊れたコネクションは捨て、少し後に開き直す。
                    self._close_locked()
                    self._next_open = time.monotonic() + _RETRY_INTERVAL
                return None
            return int(row[0]) if row else None

    def _close_locked(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            with contextlib.suppress(sqlite3.Error):  # 閉じる際の失敗は無視
                conn.close()

    def close(self) -> None:
        """コネクションを閉じる(次の :meth:`current` で開き直す)。"""
        with self._lock:
            self._close_locked()
            self._next_open = 0.0


def _is_transient(exc: sqlite3.Error) -> bool:
    code = getattr(exc, "sqlite_errorcode", None)  # Python 3.11+
    if code is not None:
        return code & 0xFF in _TRANSIENT_CODES
    text = str(exc).lower()
    return "locked" in text or "busy" in text


def change_token_for(db: Database) -> SqliteDataVersion | None:
    """``db`` の変更トークンを返す。ファイル SQLite 以外では None。"""
    engine = db.engine
    if engine.dialect.name != "sqlite" or isinstance(engine.pool, StaticPool):
        return None
    try:
        # SQLAlchemy がコネクションを開くときと同じファイル名を得る
        # (相対パスの絶対化・``?uri=true`` の扱いを含む)。
        args, kwargs = engine.dialect.create_connect_args(engine.url)
    except Exception:  # 判定できない URL はトークンなし(TTL)で扱う
        logger.debug("SQLite のファイル名を判定できません", exc_info=True)
        return None
    filename = args[0] if args else None
    if not isinstance(filename, str) or filename in ("", ":memory:"):
        return None
    if kwargs.get("uri"):
        lowered = filename.lower()
        if lowered.startswith("file::memory:") or "mode=memory" in lowered:
            return None
        return SqliteDataVersion(filename, uri=True)
    return SqliteDataVersion(os.path.abspath(filename))
