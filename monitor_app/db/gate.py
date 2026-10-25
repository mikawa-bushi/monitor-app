"""SQLite の行取り出し(fetch)を直列化するゲート。

CPython の sqlite3 は 1 行進めるたび(``sqlite3_step``)に GIL を手放して取り直す。
複数スレッドが同時に何千行も取り出すと、行ごとに GIL の受け渡しと OS のコンテキスト
スイッチが起き、1 スレッドなら 35 ms の 1 万行が 10 スレッドで 10 秒以上かかる
(GIL コンボイ)。そこで SQLite のときだけ、データベースごとのゲートで取り出しを
1000 行ずつ順番に行う。

- ``execute()``(集計・並べ替えなど SQLite 内部の計算)はゲートの外で並行に走る。
- ゲートは待っているスレッドへ到着順に直接渡す(:class:`FetchGate`)。解放した
  スレッドがすぐに取り直して割り込むことがないため、大きな取り出しが続いていても
  ほかのスレッドは順番どおりに入れる。
- 最初のチャンク(軽い読み取りはこれで終わる)は、2 チャンク目以降より先に通す。
  大きな表の取得が並んでいても、軽い読み取りの待ちは取り出し中の 1 チャンク分程度。
  ただし 2 チャンク目以降の待ち手がいる間に優先の受け渡しが
  :attr:`FetchGate.PRIORITY_STREAK` 回続いたら、一度は 2 チャンク目以降の先頭へ渡す
  (軽い読み取りを繰り返すスレッドが何本あっても、大きな取り出しが止まらない)。
- 再入可能なので、ゲート内から同じデータベースを読んでもデッドロックしない。
- PostgreSQL / MySQL のドライバはこの問題を起こさないため、何もしない。
"""

from __future__ import annotations

import threading
import weakref
from collections import deque
from contextlib import nullcontext
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlalchemy.engine import CursorResult

    from .engine import Database

#: 1 回にゲート内で取り出す行数。
FETCH_CHUNK = 1000


class FetchGate:
    """待っているスレッドへ順番に直接引き渡す、再入可能なゲート。

    通常のロックは、解放したスレッドがすぐに取り直すと待っているスレッドより先に
    入れてしまう(大きな取り出しのチャンクの合間に、軽い読み取りが何十 ms も待たされる)。
    このゲートは解放時に次の待ち手へ所有権ごと渡す。``priority=True`` (最初の
    チャンクと ``with`` 文)の待ち手は、``priority=False``(2 チャンク目以降)の
    待ち手より先に入る。ただし ``priority=False`` の待ち手がいる間に優先の受け渡しが
    :attr:`PRIORITY_STREAK` 回続いたら、次は ``priority=False`` の先頭に渡す
    (追い越され続けて止まらないように)。
    """

    #: 2 チャンク目以降の待ち手がいる間に、続けて優先(最初のチャンク)へ渡す最大回数。
    PRIORITY_STREAK = 3

    def __init__(self) -> None:
        self._mutex = threading.Lock()
        self._owner: int | None = None
        self._depth = 0
        self._first: deque[tuple[int, threading.Lock]] = deque()
        self._rest: deque[tuple[int, threading.Lock]] = deque()
        #: 2 チャンク目以降の待ち手がいる間に、続けて優先へ渡した回数。
        self._streak = 0

    def acquire(self, priority: bool = True) -> None:
        """ゲートに入る(順番が来るまで待つ)。同じスレッドなら入れ子にできる。"""
        me = threading.get_ident()
        with self._mutex:
            if self._owner == me:
                self._depth += 1
                return
            if self._owner is None:
                self._owner, self._depth = me, 1
                return
            entry = (me, threading.Lock())
            entry[1].acquire()
            (self._first if priority else self._rest).append(entry)
        try:
            entry[1].acquire()  # 前の持ち主が release() で所有権ごと渡す
        except BaseException:
            # 待っている間に例外(シグナルなど)で抜けた。順番を残すと、いない
            # スレッドに所有権が渡ってゲートが開かなくなるため取り消す。
            with self._mutex:
                for queue in (self._first, self._rest):
                    if entry in queue:
                        queue.remove(entry)
                        break
                else:  # 既に引き渡されていた → 次の待ち手へ渡す
                    self._depth = 1
                    self._release_locked()
            raise

    def release(self) -> None:
        """ゲートを出る。最も外側なら、次の待ち手に所有権を渡す。"""
        with self._mutex:
            if self._owner != threading.get_ident():
                raise RuntimeError("ゲートに入っていないスレッドが解放しようとしました")
            self._release_locked()

    def _release_locked(self) -> None:
        self._depth -= 1
        if self._depth:
            return
        if self._first and not (self._rest and self._streak >= self.PRIORITY_STREAK):
            queue = self._first
            # 2 チャンク目以降が待っている間だけ数える(誰も追い越していなければ数えない)。
            self._streak = self._streak + 1 if self._rest else 0
        elif self._rest:
            queue = self._rest
            self._streak = 0
        else:
            self._owner = None
            self._streak = 0
            return
        self._owner, waiter = queue.popleft()
        self._depth = 1
        waiter.release()

    def __enter__(self) -> FetchGate:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


_registry_lock = threading.Lock()
#: エンジン → ゲート。エンジン(= Database)が破棄されれば自動で消える。
_gates: weakref.WeakKeyDictionary[Any, FetchGate] = weakref.WeakKeyDictionary()


def fetch_gate(db: Database) -> FetchGate | nullcontext[None]:
    """``db`` の取り出し用ゲートを返す(SQLite 以外では何もしないコンテキスト)。"""
    engine = db.engine
    if engine.dialect.name != "sqlite":
        return nullcontext()
    gate = _gates.get(engine)
    if gate is None:
        with _registry_lock:
            gate = _gates.get(engine)
            if gate is None:
                gate = FetchGate()
                _gates[engine] = gate
    return gate


def fetch_all(
    result: CursorResult[Any], db: Database, chunk: int = FETCH_CHUNK
) -> list[Any]:
    """``result`` の全行を、ゲートを 1 チャンクごとに取り直しながら取り出す。"""
    gate = fetch_gate(db)
    if not isinstance(gate, FetchGate):
        return list(result.fetchall())
    rows: list[Any] = []
    priority = True  # 最初のチャンク(軽い読み取りはこれだけで終わる)を優先する
    while True:
        gate.acquire(priority)
        try:
            part = result.fetchmany(chunk)
        finally:
            gate.release()
        priority = False
        rows.extend(part)
        if len(part) < chunk:
            # 読み切った。カーソルを閉じて SQLite の読み取りロックを残さない。
            result.close()
            return rows
