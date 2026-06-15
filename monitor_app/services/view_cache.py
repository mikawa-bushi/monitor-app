"""ビュー結果の共有キャッシュ(#18)。

SSE・ダッシュボード・アラート評価が同一ビューを高頻度に再クエリするため、
TTL 内は計算結果(クエリ結果+ハッシュ)を共有する。書き込み(CRUD/取り込み)
時に :meth:`clear` で全無効化して鮮度を保つ。``ttl_ms<=0`` でキャッシュ無効。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, Tuple


class ViewCache:
    def __init__(self, ttl_ms: int) -> None:
        self.ttl = max(ttl_ms, 0) / 1000.0
        self._lock = threading.Lock()
        self._store: Dict[Any, Tuple[float, Any]] = {}

    def get_or_compute(self, key: Any, compute: Callable[[], Any]) -> Any:
        """``key`` の値を返す。TTL 内のキャッシュがあればそれを、無ければ
        ``compute()`` を実行して保存する。``compute`` はロック外で呼ぶ
        (DB I/O 中にロックを保持しない)。"""
        if self.ttl <= 0:
            return compute()
        now = time.monotonic()
        with self._lock:
            hit = self._store.get(key)
            if hit is not None and (now - hit[0]) < self.ttl:
                return hit[1]
        value = compute()
        with self._lock:
            self._store[key] = (time.monotonic(), value)
        return value

    def clear(self) -> None:
        with self._lock:
            self._store.clear()
