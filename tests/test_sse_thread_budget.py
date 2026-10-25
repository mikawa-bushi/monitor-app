"""大勢が遅いビューを SSE で購読しても、スレッドを使い切らないこと。

- SSE のフレームは専用の枠(``SSE_THREADS``)のスレッドで計算し、AnyIO の既定の
  スレッド(通常の API が使う)には手を付けない。
- ほかの購読者が計算中のビューは、スレッドを使わずに非同期で待って結果を受け取る
  (計算は 1 回、待つだけのスレッドで枠が埋まらない)。
- ``/api/health`` は専用の枠で DB を確かめ、既定のスレッドが埋まっていても応答する。
"""

from __future__ import annotations

import threading
import time
import traceback

import anyio
import pytest
from fastapi.testclient import TestClient

from monitor_app import MonitorConfig, TableDef, ViewDef
from monitor_app.api.routers import meta
from monitor_app.api.routers import views as views_router
from monitor_app.exceptions import QueryExecutionError
from monitor_app.main import create_app
from monitor_app.services.view_cache import ComputationPending, ViewCache
from monitor_app.settings.runtime import AppSettings


# ---------------------------------------------------------------------------
# ViewCache: 待たない取得
# ---------------------------------------------------------------------------
def _start_leader(cache: ViewCache, compute) -> tuple[threading.Thread, list]:
    out: list = []

    def run() -> None:
        try:
            out.append(cache.get_or_compute("k", compute))
        except BaseException as exc:  # noqa: BLE001
            out.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    return thread, out


def test_a_non_waiting_lookup_reports_the_running_computation():
    cache = ViewCache(1000)
    started, release = threading.Event(), threading.Event()

    def slow():
        started.set()
        release.wait(5)
        return "value"

    leader, out = _start_leader(cache, slow)
    assert started.wait(5)
    with pytest.raises(ComputationPending) as info:
        cache.get_or_compute("k", lambda: "other", wait=False)
    pending = info.value
    with pytest.raises(LookupError):
        pending.result()  # まだ終わっていない
    release.set()
    leader.join(5)
    assert pending.done.is_set()
    assert pending.result() == "value" and out == ["value"]
    assert cache.stats["computes"] == 1
    # 計算が無ければ、待たない取得でもふつうに計算・ヒットする
    assert cache.get_or_compute("k", lambda: "x", wait=False) == "value"


def test_a_failed_computation_is_raised_with_the_leaders_traceback():
    cache = ViewCache(1000)
    started, release = threading.Event(), threading.Event()

    def boom():
        started.set()
        release.wait(5)
        raise QueryExecutionError("ビュー 'v' のクエリ実行に失敗しました")

    leader, _out = _start_leader(cache, boom)
    assert started.wait(5)
    with pytest.raises(ComputationPending) as info:
        cache.get_or_compute("k", lambda: None, wait=False)
    release.set()
    leader.join(5)
    depths = []
    for _ in range(5):
        with pytest.raises(QueryExecutionError) as raised:
            info.value.result()
        depths.append(len(traceback.extract_tb(raised.value.__traceback__)))
    assert len(set(depths)) == 1  # 送出のたびにトレースバックが伸びない


def test_an_interrupted_computation_has_no_result():
    cache = ViewCache(1000)
    started, release = threading.Event(), threading.Event()

    def interrupted():
        started.set()
        release.wait(5)
        raise KeyboardInterrupt

    leader, _out = _start_leader(cache, interrupted)
    assert started.wait(5)
    with pytest.raises(ComputationPending) as info:
        cache.get_or_compute("k", lambda: None, wait=False)
    release.set()
    leader.join(5)
    with pytest.raises(LookupError):
        info.value.result()


# ---------------------------------------------------------------------------
# SSE: 専用の枠と非同期の相乗り
# ---------------------------------------------------------------------------
def _slow_app(delay: float):
    config = MonitorConfig(
        tables={"t": TableDef(columns={"id": "int"}, primary_key="id")},
        views={"slow": ViewDef(query="SELECT id FROM t")},
    )
    app = create_app(config, AppSettings(database_url="sqlite://"))
    service = app.state.view_service
    original = service._compute_snapshot
    calls: list[int] = []

    def slow_compute(*args):
        calls.append(1)
        time.sleep(delay)
        return original(*args)

    service._compute_snapshot = slow_compute
    return app, calls


def test_many_sse_viewers_of_a_slow_view_hold_one_thread_and_share_one_compute():
    app, calls = _slow_app(0.5)
    service = app.state.view_service
    engine = app.state.alert_engine
    viewers = 50
    observed: dict[str, int] = {}

    async def main() -> list:
        results: list = []

        async def viewer() -> None:
            results.append(
                await views_router._next_frame(service, engine, "slow", None)
            )

        async with anyio.create_task_group() as tg:
            for _ in range(viewers):
                tg.start_soon(viewer)
            await anyio.sleep(0.25)  # 計算の途中
            default = anyio.to_thread.current_default_thread_limiter()
            observed["default"] = default.borrowed_tokens
            observed["sse"] = views_router._sse_thread_limiter().borrowed_tokens
        return results

    results = anyio.run(main)
    assert len(calls) == 1  # 計算は 1 回だけ
    assert observed["default"] == 0  # 通常の API のスレッドには手を付けない
    assert observed["sse"] == 1  # 計算しているリーダーだけがスレッドを使う
    assert len(results) == viewers
    assert len({frame[1] for frame in results}) == 1  # 全員に同じデータが届く
    app.state.services.close()
    app.state.db.dispose()


def test_sse_frames_use_at_most_sse_threads_threads():
    app, _calls = _slow_app(0.0)
    service = app.state.view_service
    engine = app.state.alert_engine
    peak = {"sse": 0}

    original = views_router._sse_frame

    def counting(*args):
        limiter = sse_limiter[0]
        peak["sse"] = max(peak["sse"], limiter.borrowed_tokens)
        time.sleep(0.02)
        return original(*args)

    sse_limiter: list = []

    async def main() -> None:
        sse_limiter.append(views_router._sse_thread_limiter())
        async with anyio.create_task_group() as tg:
            for i in range(40):
                tg.start_soon(
                    views_router._next_frame, service, engine, "slow", (str(i),)
                )

    views_router._sse_frame = counting
    try:
        anyio.run(main)
    finally:
        views_router._sse_frame = original
    assert 1 <= peak["sse"] <= views_router.SSE_THREADS
    app.state.services.close()
    app.state.db.dispose()


def test_health_answers_while_the_default_worker_threads_are_all_busy():
    config = MonitorConfig(tables={"t": TableDef(columns={"id": "int"})})
    app = create_app(config, AppSettings(database_url="sqlite://"))

    class _Request:
        def __init__(self, application) -> None:
            self.app = application

    release = threading.Event()

    async def main() -> float:
        default = anyio.to_thread.current_default_thread_limiter()
        default.total_tokens = 2
        async with anyio.create_task_group() as tg:
            for _ in range(2):  # 既定のスレッドをすべて塞ぐ(遅いビューの計算待ちなど)
                tg.start_soon(anyio.to_thread.run_sync, release.wait, 10)
            await anyio.sleep(0.1)
            assert default.borrowed_tokens == 2
            began = time.monotonic()
            with anyio.fail_after(5):
                response = await meta.health(_Request(app))
            took = time.monotonic() - began
            assert response.status_code == 200
            release.set()
        return took

    try:
        assert anyio.run(main) < 2.0
    finally:
        release.set()
        app.state.services.close()
        app.state.db.dispose()


def test_health_endpoints_still_work_through_http():
    config = MonitorConfig(tables={"t": TableDef(columns={"id": "int"})})
    app = create_app(config, AppSettings(database_url="sqlite://"))
    with TestClient(app) as client:
        response = client.get("/api/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"
        assert client.head("/api/health").status_code == 200
    app.state.db.dispose()
