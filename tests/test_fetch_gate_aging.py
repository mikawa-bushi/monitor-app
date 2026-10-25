"""取り出しゲートで、大きな取り出しが軽い読み取りに追い越され続けて止まらないこと。

最初のチャンク(軽い読み取り)は 2 チャンク目以降より先に通すが、2 チャンク目以降の
待ち手がいる間に優先の受け渡しが続いたら、一度は 2 チャンク目以降の先頭に渡す。
軽い読み取りを繰り返すスレッドが何本あっても、大きな取り出しは進み続ける。
"""

from __future__ import annotations

import threading
import time

from sqlalchemy import text

from monitor_app.db.engine import Database
from monitor_app.db.gate import FetchGate, fetch_all, fetch_gate


def _hold(seconds: float) -> None:
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        pass


def test_a_big_fetch_finishes_while_six_threads_keep_doing_small_reads():
    gate = FetchGate()
    stop = threading.Event()

    def small_reads() -> None:
        while not stop.is_set():
            with gate:  # 最初のチャンク(優先)だけで終わる軽い読み取り
                _hold(0.0005)

    loops = [threading.Thread(target=small_reads, daemon=True) for _ in range(6)]
    for thread in loops:
        thread.start()
    chunks = 60
    done = threading.Event()

    def big_fetch() -> None:
        for i in range(chunks):
            gate.acquire(priority=i == 0)
            try:
                _hold(0.0005)
            finally:
                gate.release()
        done.set()

    time.sleep(0.05)  # 軽い読み取りが回り始めてから
    began = time.monotonic()
    threading.Thread(target=big_fetch, daemon=True).start()
    try:
        assert done.wait(10), "大きな取り出しが軽い読み取りに追い越され続けました"
        took = time.monotonic() - began
    finally:
        stop.set()
        for thread in loops:
            thread.join(5)
    # 1 チャンクにつき、優先の受け渡しは高々数回(全体でも数秒にならない)。
    assert took < 5


def test_small_reads_still_wait_only_a_few_hand_offs():
    """2 チャンク目以降が待っていても、最初のチャンクは数回の受け渡しで入れる。"""
    gate = FetchGate()
    order: list[str] = []
    gate.acquire()

    def enter(name: str, priority: bool) -> threading.Thread:
        def run() -> None:
            gate.acquire(priority)
            order.append(name)
            gate.release()

        before = len(gate._first) + len(gate._rest)
        thread = threading.Thread(target=run)
        thread.start()
        deadline = time.monotonic() + 5
        while len(gate._first) + len(gate._rest) != before + 1:
            assert time.monotonic() < deadline
            time.sleep(0.001)
        return thread

    threads = [enter("rest-1", False)]
    threads += [enter(f"first-{i}", True) for i in range(1, 6)]
    gate.release()
    for thread in threads:
        thread.join(5)
    # 優先の受け渡しが続いたら 2 チャンク目以降の先頭に一度渡し、また優先に戻る。
    streak = FetchGate.PRIORITY_STREAK
    assert order[:streak] == [f"first-{i}" for i in range(1, streak + 1)]
    assert order[streak] == "rest-1"
    assert sorted(order[streak + 1 :]) == [f"first-{i}" for i in range(streak + 1, 6)]


def test_fetch_all_with_concurrent_small_reads_on_sqlite(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'big.db'}")
    try:
        with db.connect() as conn:
            conn.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)"))
            conn.execute(
                text(
                    "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n"
                    " WHERE i < 40000) INSERT INTO t SELECT i, 'x' FROM n"
                )
            )
        gate = fetch_gate(db)
        assert isinstance(gate, FetchGate)
        stop = threading.Event()

        def small_reads() -> None:
            while not stop.is_set():
                with db.readonly() as conn:
                    result = conn.execute(text("SELECT id FROM t LIMIT 5"))
                    fetch_all(result, db)

        loops = [threading.Thread(target=small_reads, daemon=True) for _ in range(6)]
        for thread in loops:
            thread.start()
        rows: list = []

        def big() -> None:
            with db.readonly() as conn:
                rows.extend(fetch_all(conn.execute(text("SELECT * FROM t")), db))

        try:
            worker = threading.Thread(target=big, daemon=True)
            worker.start()
            worker.join(30)
            assert not worker.is_alive(), "大きな取り出しが終わりませんでした"
        finally:
            stop.set()
            for thread in loops:
                thread.join(10)
        assert len(rows) == 40000
    finally:
        db.dispose()
