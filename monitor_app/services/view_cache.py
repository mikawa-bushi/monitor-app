"""ビュー結果の共有キャッシュ(#18)。

SSE・ダッシュボード・アラート評価・KPI が同じビューを高頻度に読むため、計算結果を
共有する。書き込み(CRUD/取り込み)時は :meth:`ViewCache.clear` で全無効化する。
``ttl_ms<=0`` でキャッシュ無効(毎回計算する)。

鮮度の判定:

- 変更トークン(ファイル SQLite の ``PRAGMA data_version``。
  :mod:`monitor_app.db.change_token`)がある間は、トークンが変わらない限り
  ``max_age_ms``(既定 15 秒)まで有効。誰も書き込まなければ再計算しない。
- トークンが無い DB(PostgreSQL / MySQL / インメモリ)や、``now`` や ``random()`` の
  ように時刻・乱数に依存する SQL(``time_relative=True``)は従来どおり TTL で判定する。

同時アクセス(PERF-01): 同じキーの計算中に来た呼び出しは、先行する計算(リーダー)の
完了を待ってその値か例外を受け取る(single-flight)。待ちは最大 ``wait_timeout`` 秒で、
超えたら最初に待ちを打ち切った呼び出しが新しいリーダーとして計算し直し、ほかの
呼び出しはその計算に相乗りする(遅い計算のたびに全員が一斉に計算し始めない)。
:meth:`ViewCache.clear` は世代を進め、それ以降に来た呼び出しは clear 前に始まった
計算に相乗りせず、その結果は保存されない。
エントリ数は ``max_entries``(既定 256)を上限に、古いものから捨てる(ARCH-06)。
保存する値の推定メモリ(値の ``nbytes``。:class:`~monitor_app.services.view_service.ViewSnapshot`
が持つ)の合計も ``max_bytes`` を上限に、最近使っていないものから捨てる。1 件で上限を
超える値は保存しない(計算中に相乗りした呼び出しには渡す)。期限切れのエントリは、
保存のときに加えて取得のときにも(最大 1 秒に 1 回)捨てる。
"""

from __future__ import annotations

import itertools
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Hashable
from types import TracebackType
from typing import Any, Protocol

#: 変更トークンの組(clear の世代, データのバージョン)。バージョン不明は None。
_Token = tuple[int, Hashable | None]
#: 取得のときに期限切れのエントリを捨てる最短の間隔(秒)。
_PRUNE_INTERVAL = 1.0


def entry_size(value: Any) -> int:
    """キャッシュする値の推定メモリ(バイト)。``nbytes`` を持たない値は 0 とみなす。"""
    size = getattr(value, "nbytes", None)
    return size if isinstance(size, int) and size > 0 else 0


class ChangeToken(Protocol):
    """データが変わると値が変わるトークン(読めなければ None)。"""

    def current(self) -> Hashable | None: ...


class _Entry:
    __slots__ = ("value", "token", "stored_at", "time_relative", "seq", "size")

    def __init__(
        self,
        value: Any,
        token: _Token,
        stored_at: float,
        time_relative: bool,
        seq: int,
        size: int = 0,
    ) -> None:
        self.value = value
        self.token = token
        self.stored_at = stored_at
        self.time_relative = time_relative
        self.seq = seq
        self.size = size


class _Flight:
    """進行中の計算 1 件。完了するとフォロワーへ値か例外を渡す。"""

    __slots__ = ("token", "seq", "done", "value", "error", "error_tb")

    def __init__(self, token: _Token, seq: int) -> None:
        self.token = token
        self.seq = seq
        self.done = threading.Event()
        self.value: Any = None
        self.error: BaseException | None = None
        #: 計算したスレッドでの例外のトレースバック(フォロワーが送出し直すときに使う)。
        self.error_tb: TracebackType | None = None

    def outcome(self) -> Any:
        """完了した計算の値を返すか、計算が送出した例外を送出する。

        例外は全フォロワーで同じオブジェクトなので、計算したスレッドのトレースバックに
        戻してから送出する(送出のたびにほかのスレッドのフレームが積み重ならない)。
        """
        error = self.error
        if error is None:
            return self.value
        raise error.with_traceback(self.error_tb)


class ComputationPending(Exception):
    """待たない取得(``wait=False``)で、同じキーをほかの呼び出しが計算中だった。

    非同期の呼び出し側(SSE)が、スレッドを使わずに計算の終わりを待つために使う。
    ``done`` は計算が終わると立つイベント。終わったら :meth:`result` で、相乗りして
    待ったときと同じ値(または例外)を受け取れる。
    """

    def __init__(self, flight: _Flight) -> None:
        super().__init__("同じキーをほかの呼び出しが計算中です")
        self._flight = flight
        self.done = flight.done

    def result(self) -> Any:
        """計算の値。まだ終わっていない・中断されて値が無ければ :class:`LookupError`。"""
        flight = self._flight
        if not flight.done.is_set():
            raise LookupError("計算はまだ終わっていません")
        if flight.error is not None and not isinstance(flight.error, Exception):
            raise LookupError("計算は中断されました")
        return flight.outcome()


class ViewCache:
    def __init__(
        self,
        ttl_ms: int,
        *,
        change_token: ChangeToken | None = None,
        max_age_ms: int = 15000,
        max_entries: int = 256,
        wait_timeout: float = 30.0,
        max_bytes: int | None = None,
    ) -> None:
        self.ttl = max(ttl_ms, 0) / 1000.0
        #: 変更トークンで「データが変わっていない」と分かっている間の有効期間(秒)。
        #: TTL より短くはしない(TTL を長く設定している環境の挙動を変えない)。
        self.max_age = max(max_age_ms / 1000.0, self.ttl)
        self.max_entries = max(max_entries, 1)
        #: 保存する値の推定メモリの合計の上限(バイト)。None なら件数の上限だけ。
        self.max_bytes = max_bytes if max_bytes is not None and max_bytes > 0 else None
        self.wait_timeout = wait_timeout
        self._change_token = change_token
        self._lock = threading.Lock()
        self._store: OrderedDict[Any, _Entry] = OrderedDict()
        #: 保存している値の推定メモリの合計(バイト)。
        self._bytes = 0
        self._pruned_at = float("-inf")
        self._flights: dict[Any, _Flight] = {}
        self._gen = 0
        self._seq = itertools.count(1)
        #: 経過時間の計測に使う時計(テストで差し替えられるよう属性にしておく)。
        self._clock: Callable[[], float] = time.monotonic
        #: 診断用カウンタ(ヒット・計算・相乗り待ち)。
        self.stats = {"hits": 0, "computes": 0, "waits": 0}

    # ------------------------------------------------------------------
    # 判定
    # ------------------------------------------------------------------
    @property
    def generation(self) -> int:
        """:meth:`clear` のたびに増える世代番号。"""
        return self._gen

    def _data_version(self) -> Hashable | None:
        if self._change_token is None:
            return None
        try:
            return self._change_token.current()
        except Exception:  # noqa: BLE001 - トークンが読めなければ TTL で判定する
            return None

    def _limit(self, entry: _Entry, version: Hashable | None) -> float:
        """エントリの有効期間(秒)。データが変わっていれば 0。"""
        stored = entry.token[1]
        if version is not None and stored is not None:
            if stored != version:
                return 0.0
            if not entry.time_relative:
                return self.max_age
        return self.ttl

    def _is_valid(
        self, entry: _Entry, gen: int, version: Hashable | None, now: float
    ) -> bool:
        if entry.token[0] != gen:
            return False
        return now - entry.stored_at < self._limit(entry, version)

    # ------------------------------------------------------------------
    # 取得
    # ------------------------------------------------------------------
    def get_or_compute(
        self,
        key: Any,
        compute: Callable[[], Any],
        *,
        time_relative: bool = False,
        cacheable: Callable[[Any], bool] | None = None,
        wait: bool = True,
    ) -> Any:
        """``key`` の値を返す。有効なキャッシュがあればそれを、無ければ ``compute()``
        の結果を保存して返す。``compute`` はロック外で呼ぶ(DB I/O 中にロックを保持しない)。

        ``time_relative=True`` は時刻・乱数に依存する計算で、データが変わらなくても
        TTL を過ぎたら計算し直す。``cacheable(value)`` が偽を返した結果は保存しない
        (一時的な失敗を含む結果など。計算中に相乗りした呼び出しにはそのまま渡す)。
        ``wait=False`` なら、同じキーの計算中に相乗りして待つ代わりに
        :class:`ComputationPending` を送出する。
        """
        if self.ttl <= 0:
            return compute()
        version = self._data_version()  # I/O を伴うのでロックの外で読む
        now = self._clock()
        with self._lock:
            if now - self._pruned_at >= _PRUNE_INTERVAL:
                self._prune_locked(now)  # 保存が無くても期限切れの分のメモリを返す
            gen = self._gen
            entry = self._store.get(key)
            if entry is not None and self._is_valid(entry, gen, version, now):
                self._store.move_to_end(key)
                self.stats["hits"] += 1
                return entry.value
            token: _Token = (gen, version)
            flight = self._flights.get(key)
            if flight is not None and flight.token == token:
                if not wait:
                    raise ComputationPending(flight)
                self.stats["waits"] += 1
                leader = False
            else:
                # 計算中のものが無いか、clear やデータ変更より前に始まったもの。
                flight = _Flight(token, next(self._seq))
                self._flights[key] = flight
                self.stats["computes"] += 1
                leader = True
        if leader:
            return self._lead(key, flight, compute, time_relative, cacheable)
        return self._follow(key, flight, compute, time_relative, cacheable)

    def _lead(
        self,
        key: Any,
        flight: _Flight,
        compute: Callable[[], Any],
        time_relative: bool,
        cacheable: Callable[[Any], bool] | None,
    ) -> Any:
        try:
            value = compute()
            store = cacheable is None or cacheable(value)
        except BaseException as exc:
            flight.error = exc
            flight.error_tb = exc.__traceback__
            raise
        else:
            flight.value = value
            if store:
                with self._lock:
                    self._store_locked(key, flight, value, time_relative)
            return value
        finally:
            with self._lock:
                if self._flights.get(key) is flight:
                    del self._flights[key]
                # 登録の解除と完了を、待っている呼び出しが同時に見られるようロック下で。
                flight.done.set()

    def _follow(
        self,
        key: Any,
        flight: _Flight,
        compute: Callable[[], Any],
        time_relative: bool,
        cacheable: Callable[[Any], bool] | None,
    ) -> Any:
        while not flight.done.wait(self.wait_timeout):
            # リーダーが極端に遅い(wait_timeout を超えた)。全員が一斉に計算し直さない
            # よう、引き継ぎは 1 件だけにして、ほかの呼び出しはそれに相乗りする。
            with self._lock:
                if flight.done.is_set():
                    break  # ちょうど終わった
                newer = self._store.get(key)
                if newer is not None and newer.seq > flight.seq:
                    # 待っていた計算より後に始まった計算(引き継ぎ)の結果がもうある。
                    self.stats["hits"] += 1
                    return newer.value
                current = self._flights.get(key)
                if current is not None and current is not flight:
                    # ほかの呼び出しが引き継いだ(か、より新しい計算がある)。それを待つ。
                    flight = current
                    self.stats["waits"] += 1
                    continue
                # 最初に待ちを打ち切った呼び出しが新しいリーダーになる。遅いリーダーの
                # 結果は、後から始まったこの計算の結果を上書きしない。
                own = _Flight(flight.token, next(self._seq))
                self._flights[key] = own
                self.stats["computes"] += 1
            return self._lead(key, own, compute, time_relative, cacheable)
        error = flight.error
        if error is None or isinstance(error, Exception):
            return flight.outcome()
        # リーダーが KeyboardInterrupt などで中断され、結果が無い。改めて取得する
        # (相乗りしていた呼び出し同士は、その中でまた 1 回の計算を共有する)。
        return self.get_or_compute(
            key, compute, time_relative=time_relative, cacheable=cacheable
        )

    def _store_locked(
        self, key: Any, flight: _Flight, value: Any, time_relative: bool
    ) -> None:
        if flight.token[0] != self._gen:
            return  # 計算中に clear された: 古い結果は保存しない
        current = self._store.get(key)
        if current is not None and current.seq > flight.seq:
            return  # 後から始まった計算の結果がすでにある
        now = self._clock()
        size = entry_size(value)
        if self.max_bytes is not None and size > self.max_bytes:
            # 1 件で上限を超える: 保存せず(古い結果も捨てる)、相乗りした呼び出しにだけ渡す。
            self._remove_locked(key)
            return
        self._remove_locked(key)
        self._store[key] = _Entry(
            value, flight.token, now, time_relative, flight.seq, size
        )
        self._bytes += size
        self._prune_locked(now)

    def _remove_locked(self, key: Any) -> None:
        entry = self._store.pop(key, None)
        if entry is not None:
            self._bytes -= entry.size

    def _prune_locked(self, now: float) -> None:
        """期限切れのエントリを捨て、件数・推定メモリの上限を超えた分を古い順に捨てる。

        「古い順」は最後に使った順(ヒットしたエントリは末尾へ移す)。
        """
        self._pruned_at = now
        expired = [
            k
            for k, e in self._store.items()
            if now - e.stored_at
            >= (self.ttl if e.time_relative or e.token[1] is None else self.max_age)
        ]
        for k in expired:
            self._remove_locked(k)
        while len(self._store) > self.max_entries:
            _, entry = self._store.popitem(last=False)
            self._bytes -= entry.size
        if self.max_bytes is not None:
            while self._bytes > self.max_bytes and self._store:
                _, entry = self._store.popitem(last=False)
                self._bytes -= entry.size

    # ------------------------------------------------------------------
    # 無効化・後片付け
    # ------------------------------------------------------------------
    def discard(self, key: Any) -> None:
        """``key`` のエントリだけを捨てる(一時的な失敗を含む結果を残さないため)。"""
        with self._lock:
            self._remove_locked(key)

    def clear(self) -> None:
        """全エントリを捨て、世代を進める(計算中の結果も保存させない)。"""
        with self._lock:
            self._gen += 1
            self._store.clear()
            self._bytes = 0

    @property
    def size(self) -> int:
        """保存しているエントリの数(診断・テスト用)。"""
        return len(self._store)

    @property
    def nbytes(self) -> int:
        """保存している値の推定メモリの合計(バイト。診断・テスト用)。"""
        return self._bytes

    def close(self) -> None:
        """変更トークンの資源(SQLite コネクション)を閉じる。キャッシュは引き続き使える。"""
        close = getattr(self._change_token, "close", None)
        if close is not None:
            close()
