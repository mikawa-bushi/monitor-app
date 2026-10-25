"""ビュー共有キャッシュ(ViewCache)の単体テスト。

single-flight(同時に来た同じキーの呼び出しは 1 回の計算を共有する)、clear の世代、
変更トークンによる有効期間の延長、時刻依存 SQL の TTL、件数の上限を確かめる。
"""

from __future__ import annotations

import itertools
import threading
import time

import pytest

from monitor_app.services.view_cache import ViewCache


class _Token:
    """値を手で進められる変更トークン。"""

    def __init__(self, value=1) -> None:
        self.value = value
        self.closed = 0

    def current(self):
        return self.value

    def close(self) -> None:
        self.closed += 1


class _Clock:
    """手で進める時計(実時間に依存せず有効期限を確かめる)。"""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _cache(ttl_ms: int, **kwargs) -> tuple[ViewCache, _Clock]:
    cache = ViewCache(ttl_ms, **kwargs)
    clock = _Clock()
    cache._clock = clock
    return cache, clock


def _wait_for(pred, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise AssertionError("条件が満たされませんでした")
        time.sleep(0.002)


def _run_threads(n: int, target) -> list[threading.Thread]:
    threads = [threading.Thread(target=target) for _ in range(n)]
    for t in threads:
        t.start()
    return threads


# ---------------------------------------------------------------------------
# single-flight
# ---------------------------------------------------------------------------
def test_concurrent_callers_share_one_compute():
    cache = ViewCache(1000)
    release = threading.Event()
    calls: list[int] = []

    def compute():
        calls.append(1)
        release.wait(5)
        return "value"

    results: list[str] = []
    lock = threading.Lock()

    def call():
        value = cache.get_or_compute("k", compute)
        with lock:
            results.append(value)

    threads = _run_threads(20, call)
    _wait_for(lambda: cache.stats["waits"] == 19)  # 19 件がリーダーを待っている
    release.set()
    for t in threads:
        t.join(5)
    assert calls == [1]
    assert results == ["value"] * 20
    assert cache.get_or_compute("k", lambda: "recomputed") == "value"  # 保存済み


def test_leader_exception_is_raised_in_every_follower_and_not_stored():
    cache = ViewCache(1000)
    release = threading.Event()
    calls: list[int] = []

    def compute():
        calls.append(1)
        release.wait(5)
        raise RuntimeError("DB が落ちた")

    errors: list[BaseException] = []
    lock = threading.Lock()

    def call():
        try:
            cache.get_or_compute("k", compute)
        except RuntimeError as exc:
            with lock:
                errors.append(exc)

    threads = _run_threads(10, call)
    _wait_for(lambda: cache.stats["waits"] == 9)
    release.set()
    for t in threads:
        t.join(5)
    assert calls == [1]  # フォロワーは計算し直さない(相乗りした結果=例外を受け取る)
    assert len(errors) == 10 and all(str(e) == "DB が落ちた" for e in errors)
    assert cache.size == 0
    assert cache.get_or_compute("k", lambda: "ok") == "ok"  # 次の呼び出しは計算する


def test_clear_during_compute_is_neither_joined_nor_stored():
    cache = ViewCache(60_000)
    release = threading.Event()
    old_results: list[str] = []

    def slow_old():
        release.wait(5)
        return "old"

    leader = threading.Thread(
        target=lambda: old_results.append(cache.get_or_compute("k", slow_old))
    )
    leader.start()
    _wait_for(lambda: cache.stats["computes"] == 1)
    follower = threading.Thread(  # clear より前に来た呼び出し: 相乗りする
        target=lambda: old_results.append(cache.get_or_compute("k", slow_old))
    )
    follower.start()
    _wait_for(lambda: cache.stats["waits"] == 1)

    cache.clear()  # 書き込みがあった
    # clear 後に来た呼び出しは、clear 前に始まった計算に相乗りしない。
    assert cache.get_or_compute("k", lambda: "new") == "new"

    release.set()
    leader.join(5)
    follower.join(5)
    assert old_results == ["old", "old"]
    # clear 前に始まった計算の結果は保存されない(新しい結果が残る)。
    assert cache.get_or_compute("k", lambda: "unexpected") == "new"


def test_stale_result_after_clear_does_not_overwrite_nor_get_stored():
    cache = ViewCache(60_000)
    release = threading.Event()

    def slow():
        release.wait(5)
        return "before-clear"

    t = threading.Thread(target=lambda: cache.get_or_compute("k", slow))
    t.start()
    _wait_for(lambda: cache.stats["computes"] == 1)
    cache.clear()
    release.set()
    t.join(5)
    assert cache.size == 0
    assert cache.get_or_compute("k", lambda: "fresh") == "fresh"


def test_follower_computes_itself_after_the_wait_timeout():
    cache = ViewCache(1000, wait_timeout=0.05)
    release = threading.Event()

    def slow():
        release.wait(5)
        return "leader"

    leader = threading.Thread(target=lambda: cache.get_or_compute("k", slow))
    leader.start()
    _wait_for(lambda: cache.stats["computes"] == 1)
    try:
        assert cache.get_or_compute("k", lambda: "follower") == "follower"
    finally:
        release.set()
        leader.join(5)
    # 後から始まった(新しい)follower の結果を、遅れて終わった leader が上書きしない。
    assert cache.get_or_compute("k", lambda: "x") == "follower"


def test_timed_out_followers_share_one_recompute():
    """遅いリーダーを待ちきれなかった呼び出しは、全員が別々に計算し直さない。"""
    cache = ViewCache(1000, wait_timeout=0.2)
    release = threading.Event()
    calls: list[str] = []
    lock = threading.Lock()

    def slow():
        with lock:
            calls.append("leader")
        release.wait(5)
        return "leader"

    def retry():
        with lock:
            calls.append("retry")
        return "retry"

    leader = threading.Thread(target=lambda: cache.get_or_compute("k", slow))
    leader.start()
    _wait_for(lambda: cache.stats["computes"] == 1)
    results: list[str] = []

    def follow():
        value = cache.get_or_compute("k", retry)
        with lock:
            results.append(value)

    followers = _run_threads(10, follow)
    try:
        for t in followers:
            t.join(5)
        assert results == ["retry"] * 10
        assert calls == ["leader", "retry"]  # 引き継いだ 1 件だけが計算し直した
    finally:
        release.set()
        leader.join(5)
    assert cache.get_or_compute("k", lambda: "x") == "retry"  # 遅い結果で上書きしない
    assert cache._flights == {}


def test_timed_out_follower_waits_for_a_newer_flight_instead_of_computing():
    """データ変更の後に始まった計算があれば、待ちを打ち切った呼び出しはそれを待つ。"""
    token = _Token(1)
    cache = ViewCache(1000, change_token=token, wait_timeout=0.2)
    release_old, release_new = threading.Event(), threading.Event()
    calls: list[str] = []

    def old():
        calls.append("old")
        release_old.wait(5)
        return "old"

    def new():
        calls.append("new")
        release_new.wait(5)
        return "new"

    results: dict[str, str] = {}
    leader = threading.Thread(target=lambda: cache.get_or_compute("k", old))
    leader.start()
    _wait_for(lambda: calls == ["old"])
    follower = threading.Thread(
        target=lambda: results.setdefault("follower", cache.get_or_compute("k", old))
    )
    follower.start()
    _wait_for(lambda: cache.stats["waits"] == 1)
    token.value = 2  # 書き込みがあった: 次の呼び出しは新しい計算を始める
    newer = threading.Thread(
        target=lambda: results.setdefault("newer", cache.get_or_compute("k", new))
    )
    newer.start()
    _wait_for(lambda: calls == ["old", "new"])
    _wait_for(lambda: cache.stats["waits"] == 2)  # 打ち切った後、新しい計算を待つ
    release_new.set()
    newer.join(5)
    follower.join(5)
    release_old.set()
    leader.join(5)
    assert results == {"follower": "new", "newer": "new"}
    assert calls == ["old", "new"]  # 待ちを打ち切った呼び出しは計算し直していない


def test_follower_whose_leader_finishes_at_the_timeout_takes_its_result(monkeypatch):
    """待ちの打ち切りと同時にリーダーが終わったら、その結果を使う(計算し直さない)。"""
    from monitor_app.services import view_cache as view_cache_mod

    class _LateEvent(threading.Event):
        """最初の wait() だけ、完了していても「時間切れ」と答える。"""

        def wait(self, timeout=None):
            if not getattr(self, "_lied", False):
                self._lied = True
                super().wait(timeout)
                return False
            return super().wait(timeout)

    release = threading.Event()  # 差し替える前の本物
    monkeypatch.setattr(view_cache_mod.threading, "Event", _LateEvent)
    cache = ViewCache(1000, wait_timeout=0.05)
    leader = threading.Thread(
        target=lambda: cache.get_or_compute("k", lambda: release.wait(5) and "leader")
    )
    leader.start()
    _wait_for(lambda: cache.stats["computes"] == 1)
    result: list[str] = []
    follower = threading.Thread(
        target=lambda: result.append(cache.get_or_compute("k", lambda: "retry"))
    )
    follower.start()
    _wait_for(lambda: cache.stats["waits"] == 1)
    release.set()
    leader.join(5)
    follower.join(5)
    assert result == ["leader"]
    assert cache.stats["computes"] == 1


def test_followers_of_an_interrupted_leader_share_one_recompute():
    cache = ViewCache(1000)
    release = threading.Event()
    calls: list[str] = []

    def interrupted():
        release.wait(5)
        raise KeyboardInterrupt

    def leader():
        with pytest.raises(KeyboardInterrupt):
            cache.get_or_compute("k", interrupted)

    def own():
        calls.append("own")
        time.sleep(0.05)
        return "own"

    t = threading.Thread(target=leader)
    t.start()
    _wait_for(lambda: cache.stats["computes"] == 1)
    results: list[str] = []
    followers = _run_threads(5, lambda: results.append(cache.get_or_compute("k", own)))
    _wait_for(lambda: cache.stats["waits"] == 5)
    release.set()
    t.join(5)
    for f in followers:
        f.join(5)
    assert results == ["own"] * 5
    assert calls == ["own"]  # 相乗りしていた呼び出し同士で 1 回の計算を共有した


def test_uncacheable_results_are_shared_but_not_stored():
    cache = ViewCache(60_000)
    release = threading.Event()

    def compute():
        release.wait(5)
        return ("partial", True)

    results: list[tuple] = []
    threads = _run_threads(
        3,
        lambda: results.append(
            cache.get_or_compute("k", compute, cacheable=lambda v: not v[1])
        ),
    )
    _wait_for(lambda: cache.stats["waits"] == 2)
    release.set()
    for t in threads:
        t.join(5)
    assert results == [("partial", True)] * 3  # 相乗りした呼び出しには渡す
    assert cache.size == 0  # 保存はしない
    assert cache.get_or_compute("k", lambda: ("ok", False)) == ("ok", False)
    assert cache.size == 1


def test_follower_of_an_interrupted_leader_computes_itself():
    cache = ViewCache(1000)
    release = threading.Event()

    def interrupted():
        release.wait(5)
        raise KeyboardInterrupt

    def leader():
        with pytest.raises(KeyboardInterrupt):
            cache.get_or_compute("k", interrupted)

    t = threading.Thread(target=leader)
    t.start()
    _wait_for(lambda: cache.stats["computes"] == 1)
    result: list[str] = []
    follower = threading.Thread(
        target=lambda: result.append(cache.get_or_compute("k", lambda: "own"))
    )
    follower.start()
    _wait_for(lambda: cache.stats["waits"] == 1)
    release.set()
    t.join(5)
    follower.join(5)
    assert result == ["own"]


# ---------------------------------------------------------------------------
# 有効期間: 変更トークン / TTL / 時刻依存
# ---------------------------------------------------------------------------
def test_without_token_entries_expire_after_the_ttl():
    cache, clock = _cache(1000)
    counter = itertools.count()
    first = cache.get_or_compute("k", lambda: next(counter))
    clock.advance(0.9)
    assert cache.get_or_compute("k", lambda: next(counter)) == first  # TTL 内
    clock.advance(0.2)
    assert cache.get_or_compute("k", lambda: next(counter)) != first


def test_token_keeps_entries_valid_beyond_the_ttl_until_the_data_changes():
    token = _Token()
    cache, clock = _cache(1000, change_token=token, max_age_ms=60_000)
    counter = itertools.count()
    first = cache.get_or_compute("k", lambda: next(counter))
    clock.advance(30)  # TTL(1 秒)を過ぎても、データが変わっていなければ使い続ける
    assert cache.get_or_compute("k", lambda: next(counter)) == first
    token.value = 2  # ほかのコネクション・プロセスが書き込んだ
    second = cache.get_or_compute("k", lambda: next(counter))
    assert second != first
    assert cache.get_or_compute("k", lambda: next(counter)) == second


def test_token_entries_expire_after_max_age():
    token = _Token()
    cache, clock = _cache(1000, change_token=token, max_age_ms=15_000)
    counter = itertools.count()
    first = cache.get_or_compute("k", lambda: next(counter))
    clock.advance(14.9)
    assert cache.get_or_compute("k", lambda: next(counter)) == first
    clock.advance(0.2)
    assert cache.get_or_compute("k", lambda: next(counter)) != first


def test_max_age_is_never_shorter_than_the_ttl():
    cache = ViewCache(5000, change_token=_Token(), max_age_ms=10)
    assert cache.max_age == 5.0


def test_time_relative_entries_fall_back_to_the_ttl():
    token = _Token()
    cache, clock = _cache(1000, change_token=token, max_age_ms=60_000)
    counter = itertools.count()
    first = cache.get_or_compute("k", lambda: next(counter), time_relative=True)
    clock.advance(0.5)
    assert cache.get_or_compute("k", lambda: next(counter), time_relative=True) == first
    clock.advance(0.6)  # データは同じでも now() の結果は変わる
    assert cache.get_or_compute("k", lambda: next(counter), time_relative=True) != first


def test_time_relative_entries_are_also_invalidated_by_a_data_change():
    token = _Token()
    cache = ViewCache(60_000, change_token=token)
    counter = itertools.count()
    first = cache.get_or_compute("k", lambda: next(counter), time_relative=True)
    token.value = 2
    assert cache.get_or_compute("k", lambda: next(counter), time_relative=True) != first


@pytest.mark.parametrize("broken", ["none", "raises"])
def test_unreadable_token_falls_back_to_the_ttl(broken):
    class _Broken:
        def current(self):
            if broken == "raises":
                raise OSError("読めない")
            return None

    cache, clock = _cache(1000, change_token=_Broken(), max_age_ms=60_000)
    counter = itertools.count()
    first = cache.get_or_compute("k", lambda: next(counter))
    clock.advance(0.5)
    assert cache.get_or_compute("k", lambda: next(counter)) == first
    clock.advance(0.6)
    assert cache.get_or_compute("k", lambda: next(counter)) != first


def test_ttl_zero_disables_caching_entirely():
    token = _Token()
    cache = ViewCache(0, change_token=token)
    counter = itertools.count()
    values = [cache.get_or_compute("k", lambda: next(counter)) for _ in range(3)]
    assert values == [0, 1, 2]
    assert cache.size == 0


def test_clear_bumps_the_generation_and_empties_the_store():
    cache = ViewCache(60_000)
    cache.get_or_compute("k", lambda: 1)
    gen = cache.generation
    cache.clear()
    assert cache.generation == gen + 1
    assert cache.get_or_compute("k", lambda: 2) == 2


# ---------------------------------------------------------------------------
# メモリの上限(ARCH-06)
# ---------------------------------------------------------------------------
def test_store_is_capped_by_lru():
    cache = ViewCache(60_000, max_entries=3)
    for key in ("a", "b", "c"):
        cache.get_or_compute(key, lambda key=key: key)
    cache.get_or_compute("a", lambda: "unused")  # a を最近使ったことにする
    cache.get_or_compute("d", lambda: "d")
    assert cache.size == 3
    assert (
        cache.get_or_compute("b", lambda: "recomputed") == "recomputed"
    )  # 最古を捨てた
    assert cache.get_or_compute("a", lambda: "unused") == "a"


def test_expired_entries_are_pruned_on_insert():
    cache, clock = _cache(1000)
    for i in range(50):  # ページングのように多数の別キー
        cache.get_or_compute(("v", 100, i), lambda: "page")
    assert cache.size == 50
    clock.advance(1.5)
    cache.get_or_compute("new", lambda: 1)
    assert cache.size == 1  # 期限切れは挿入時に捨てる


def test_discard_drops_one_entry():
    cache = ViewCache(60_000)
    cache.get_or_compute("a", lambda: 1)
    cache.get_or_compute("b", lambda: 2)
    cache.discard("a")
    cache.discard("missing")  # 無くてもよい
    assert cache.get_or_compute("a", lambda: 3) == 3
    assert cache.get_or_compute("b", lambda: 4) == 2


def test_close_closes_the_token_and_cache_stays_usable():
    token = _Token()
    cache = ViewCache(60_000, change_token=token)
    cache.close()
    assert token.closed == 1
    assert cache.get_or_compute("k", lambda: "v") == "v"
    ViewCache(1000).close()  # トークンが無くてもよい
