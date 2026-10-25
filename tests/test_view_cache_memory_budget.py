"""ビューのキャッシュのメモリが、件数だけでなく推定メモリの上限でも抑えられること。

``?limit=&offset=`` の組み合わせごとに大きなスナップショットを保存し続けて、プロセスの
メモリが何 GB にもならないようにする(``MONITOR_VIEW_CACHE_MAX_MB``)。
"""

from __future__ import annotations

import threading

from fastapi.testclient import TestClient
from sqlalchemy import insert

from monitor_app import MonitorConfig, TableDef, ViewDef
from monitor_app.main import create_app
from monitor_app.services.view_cache import ViewCache, entry_size
from monitor_app.services.view_service import (
    MEMORY_PER_JSON_BYTE,
    PAGE_SHARE,
    ViewSnapshot,
)
from monitor_app.settings.runtime import AppSettings


class _Blob:
    def __init__(self, nbytes: int, name: str = "") -> None:
        self.nbytes = nbytes
        self.name = name


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _cache(**kw) -> ViewCache:
    cache = ViewCache(1000, **kw)
    cache._clock = _Clock()
    return cache


def test_entries_are_evicted_least_recently_used_first_over_the_byte_budget():
    cache = _cache(max_bytes=1000)
    for key in "abc":
        cache.get_or_compute(key, lambda key=key: _Blob(400, key))
    # 400 × 3 = 1200 > 1000: 最も古い a を捨てる
    assert cache.size == 2 and cache.nbytes == 800
    computed: list[str] = []

    def again(key: str):
        computed.append(key)
        return _Blob(400, key)

    cache.get_or_compute("b", lambda: again("b"))  # ヒット(最後に使ったものになる)
    cache.get_or_compute("d", lambda: again("d"))  # c を追い出す(b は残る)
    cache.get_or_compute("b", lambda: again("b"))
    cache.get_or_compute("c", lambda: again("c"))
    assert computed == ["d", "c"]
    assert cache.nbytes <= 1000


def test_a_value_bigger_than_the_whole_budget_is_shared_but_not_stored():
    cache = _cache(max_bytes=1000)
    cache.get_or_compute("k", lambda: _Blob(10, "old"))
    cache.clear()  # 古いエントリを無効にする(次は計算し直す)
    cache.get_or_compute("x", lambda: _Blob(10))
    release = threading.Event()
    calls: list[int] = []

    def big():
        calls.append(1)
        release.wait(5)
        return _Blob(5000, "big")

    results: list = []
    threads = [
        threading.Thread(target=lambda: results.append(cache.get_or_compute("k", big)))
        for _ in range(3)
    ]
    for thread in threads:
        thread.start()
    while cache.stats["waits"] < 2:
        pass
    release.set()
    for thread in threads:
        thread.join(5)
    assert len(calls) == 1  # 計算は 1 回、3 件の呼び出しで共有する
    assert [r.name for r in results] == ["big", "big", "big"]
    assert cache.size == 1 and cache.nbytes == 10  # 大きな結果は保存しない


def test_expired_entries_are_freed_on_lookup_without_another_store():
    cache = _cache(max_bytes=10_000)
    clock = cache._clock
    for key in "abcd":
        cache.get_or_compute(key, lambda: _Blob(1000))
    assert cache.nbytes == 4000
    clock.now += 2.0  # TTL(1 秒)を過ぎた
    cache.get_or_compute("a", lambda: _Blob(1000))  # 取得だけで、期限切れの分を捨てる
    assert cache.size == 1 and cache.nbytes == 1000


def test_accounting_follows_discard_clear_and_replacement():
    cache = _cache(max_bytes=10_000)
    cache.get_or_compute("a", lambda: _Blob(100))
    cache.get_or_compute("b", lambda: _Blob(200))
    cache.discard("a")
    assert cache.nbytes == 200
    cache.clear()
    assert cache.nbytes == 0 and cache.size == 0
    cache.get_or_compute("a", lambda: _Blob(300))
    assert cache.nbytes == 300
    assert entry_size(object()) == 0 and entry_size(_Blob(-1)) == 0


def test_no_byte_budget_keeps_the_entry_count_bound_only():
    cache = _cache(max_bytes=0, max_entries=3)
    assert cache.max_bytes is None
    for key in "abcde":
        cache.get_or_compute(key, lambda: _Blob(10**9))
    assert cache.size == 3


def test_snapshot_estimates_its_memory_from_the_json_size():
    snap = ViewSnapshot({"data": [{"id": 1}], "columns": ["id"]})
    assert snap.nbytes == len(snap.json) * MEMORY_PER_JSON_BYTE


def _app(rows: int, max_mb: int):
    config = MonitorConfig(
        tables={"t": TableDef(columns={"id": "int", "name": "str"}, primary_key="id")},
        views={"v": ViewDef(query="SELECT id, name FROM t ORDER BY id")},
    )
    settings = AppSettings(database_url="sqlite://", view_cache_max_mb=max_mb)
    app = create_app(config, settings)
    with app.state.db.connect() as conn:
        conn.execute(
            insert(app.state.registry.get("t")),
            [{"id": i, "name": f"name-{i:06d}"} for i in range(rows)],
        )
    return app


def test_many_distinct_big_pages_stay_within_the_budget():
    app = _app(2500, max_mb=1)
    cache = app.state.view_cache
    budget = 1024 * 1024
    assert cache.max_bytes == budget
    with TestClient(app) as client:
        for offset in range(0, 1000, 50):
            # どれも 1,500 行のページ: ページの保存上限(予算 ÷ PAGE_SHARE)を超える
            response = client.get(f"/api/views/v?limit=1500&offset={offset}")
            assert response.status_code == 200
        assert cache.size == 0  # 大きなページは保存しない
        for offset in range(0, 200, 10):
            assert (
                client.get(f"/api/views/v?limit=5&offset={offset}").status_code == 200
            )
        small = cache.size
        assert small == 20  # 小さなページはこれまでどおり保存する
        assert client.get("/api/views/v").status_code == 200  # 全データ
        assert cache.size == small + 1
        assert cache.nbytes <= budget
    page = cache._store[("v", 5, 0)].value
    assert page.nbytes <= budget // PAGE_SHARE
