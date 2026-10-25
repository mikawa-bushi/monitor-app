"""同時アクセス時のビュー計算のテスト(PERF-01)。

キャッシュが空のときに同じビューへ 20 スレッドが同時に来ても計算は 1 回で、
所要時間は 1 回の計算のわずかな倍数に収まる(以前はスレッド数に比例して秒単位に
伸びた)。別々のビューを同時に計算しても、行の取り出しゲートで GIL コンボイが
起きない。
"""

from __future__ import annotations

import threading
import time

import pytest
from contract import helpers
from sqlalchemy import insert

from monitor_app import MonitorConfig, TableDef, ViewDef
from monitor_app.db.change_token import change_token_for
from monitor_app.db.engine import Database
from monitor_app.db.registry import TableRegistry
from monitor_app.services.view_cache import ViewCache
from monitor_app.services.view_service import ViewService

ROWS = 5000
DISTINCT = 8


@pytest.fixture(scope="module")
def big_db(tmp_path_factory):
    path = tmp_path_factory.mktemp("concurrency") / "big.db"
    views = {"big": ViewDef(query="SELECT * FROM m ORDER BY id")}
    for i in range(DISTINCT):
        views[f"d{i}"] = ViewDef(query=f"SELECT * FROM m WHERE id % {DISTINCT} != {i}")
    config = MonitorConfig(
        tables={
            "m": TableDef(
                columns={"id": "int", "line": "str", "temp": "float", "ok": "bool"}
            )
        },
        views=views,
    )
    db = Database(helpers.sqlite_url(path))
    registry = TableRegistry(config)
    registry.create_all(db)
    with db.connect() as conn:
        conn.execute(
            insert(registry.get("m")),
            [
                {"id": i, "line": f"L{i % 7}", "temp": i * 0.5, "ok": i % 2 == 0}
                for i in range(1, ROWS + 1)
            ],
        )
    yield config, db
    db.dispose()


def _service(config, db) -> tuple[ViewService, list[str]]:
    service = ViewService(
        config, db, cache=ViewCache(60_000, change_token=change_token_for(db))
    )
    computes: list[str] = []
    original = service._fetch

    def counting_fetch(sql, params):
        computes.append(sql)
        return original(sql, params)

    service._fetch = counting_fetch
    return service, computes


def _timed_round(n: int, target) -> float:
    barrier = threading.Barrier(n)
    errors: list[BaseException] = []

    def run(i: int) -> None:
        barrier.wait()
        try:
            target(i)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    started = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not errors, errors
    return time.perf_counter() - started


@pytest.fixture
def service_factory():
    caches: list[ViewCache] = []

    def make(config, db):
        service, computes = _service(config, db)
        caches.append(service.cache)
        return service, computes

    yield make
    for cache in caches:
        cache.close()  # 変更トークンの SQLite コネクションを閉じる


def test_twenty_cold_callers_compute_once(big_db, service_factory):
    config, db = big_db
    service, computes = service_factory(config, db)
    started = time.perf_counter()
    service.get_view_hashed("big")
    single = time.perf_counter() - started
    service.cache.clear()
    computes.clear()

    digests: list[str] = []
    wall = _timed_round(20, lambda i: digests.append(service.get_view_hashed("big")[1]))

    assert len(computes) == 1
    assert len(set(digests)) == 1 and len(digests) == 20
    # 1 回の計算のわずかな倍数(以前は 20 回分の計算 × GIL コンボイで数十倍)。
    assert wall < 5 * single + 0.5, (wall, single)


def test_distinct_views_computed_together_do_not_convoy(big_db, service_factory):
    config, db = big_db
    service, computes = service_factory(config, db)
    started = time.perf_counter()
    for i in range(DISTINCT):
        service.get_snapshot(f"d{i}")
    serial = time.perf_counter() - started
    service.cache.clear()
    computes.clear()

    wall = _timed_round(DISTINCT, lambda i: service.get_snapshot(f"d{i}"))

    assert len(computes) == DISTINCT  # 別々のビューはそれぞれ計算する
    # 直列に計算するのと同程度(ゲートが無いとスレッド数に比例して何倍にも伸びる)。
    assert wall < 3 * serial + 0.5, (wall, serial)
