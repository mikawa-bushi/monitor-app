"""SQLite の取り出しゲート(:class:`FetchGate`)の順番の公平さと、表・監査ログの取得への適用。

大きな表の取得(``GET /api/tables/{t}``)が同時に走っても、GIL コンボイで止まらず、
軽い読み取りは取り出し中の 1 チャンク分ほどしか待たない。
"""

from __future__ import annotations

import signal
import threading
import time

import pytest
from sqlalchemy import text

from monitor_app import MonitorConfig, TableDef
from monitor_app.db.engine import Database
from monitor_app.db.gate import FetchGate, fetch_all, fetch_gate
from monitor_app.db.registry import TableRegistry
from monitor_app.db.repository import TableRepository
from monitor_app.services.audit_service import AuditService


def _wait_for(pred, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.005)
    return True


def _queued(gate: FetchGate) -> int:
    return len(gate._first) + len(gate._rest)


def test_waiting_threads_enter_in_order_and_first_chunks_go_first():
    gate = FetchGate()
    order: list[str] = []
    gate.acquire()

    def enter(name: str, priority: bool) -> threading.Thread:
        def run():
            gate.acquire(priority)
            order.append(name)
            gate.release()

        thread = threading.Thread(target=run)
        before = _queued(gate)
        thread.start()
        assert _wait_for(lambda: _queued(gate) == before + 1)
        return thread

    threads = [
        enter("rest-1", False),
        enter("rest-2", False),
        enter("first-1", True),
        enter("first-2", True),
    ]
    gate.release()
    for thread in threads:
        thread.join(5)
    # 最初のチャンク(軽い読み取り)が先、それぞれの中では到着順
    assert order == ["first-1", "first-2", "rest-1", "rest-2"]


def test_releasing_thread_cannot_barge_ahead_of_a_waiting_thread():
    """解放した直後に取り直しても、待っていたスレッドが先に入る(通常のロックとの違い)。"""
    gate = FetchGate()
    events: list[str] = []
    gate.acquire()

    def waiter():
        gate.acquire(True)
        events.append("waiter")
        time.sleep(0.05)
        gate.release()

    thread = threading.Thread(target=waiter)
    thread.start()
    assert _wait_for(lambda: _queued(gate) == 1)
    gate.release()
    gate.acquire(False)  # すぐに取り直す(大きな取り出しの次のチャンク)
    events.append("releaser")
    gate.release()
    thread.join(5)
    assert events == ["waiter", "releaser"]


def test_gate_is_reentrant_and_rejects_a_release_by_another_thread():
    gate = FetchGate()
    with gate:
        with gate:
            pass
        errors: list[BaseException] = []

        def other():
            try:
                gate.release()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        thread = threading.Thread(target=other)
        thread.start()
        thread.join(5)
        assert errors and isinstance(errors[0], RuntimeError)
    # 出たあとはほかのスレッドがすぐ入れる
    entered = threading.Event()
    thread = threading.Thread(target=lambda: (gate.acquire(), entered.set()))
    thread.start()
    assert entered.wait(5)
    thread.join(5)


@pytest.mark.skipif(not hasattr(signal, "setitimer"), reason="POSIX のタイマーを使う")
def test_a_waiter_interrupted_by_a_signal_does_not_wedge_the_gate():
    """待っている間にシグナル(SIGTERM など)で抜けても、順番が残ってゲートが開かなくならない。"""
    gate = FetchGate()
    held = threading.Event()
    release = threading.Event()

    def holder():
        with gate:
            held.set()
            release.wait(5)

    thread = threading.Thread(target=holder)
    thread.start()
    assert held.wait(5)

    class Interrupted(Exception):
        pass

    def on_alarm(signum, frame):
        raise Interrupted

    previous = signal.signal(signal.SIGALRM, on_alarm)
    try:
        signal.setitimer(signal.ITIMER_REAL, 0.05)
        with pytest.raises(Interrupted):
            gate.acquire()  # メインスレッドで待っている間にシグナルが届く
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
    assert _queued(gate) == 0  # 順番は取り消された
    release.set()
    thread.join(5)
    entered = threading.Event()
    other = threading.Thread(target=lambda: (gate.acquire(), entered.set()))
    other.start()
    assert entered.wait(5)
    other.join(5)


def test_fetch_all_still_reads_everything_with_concurrent_fetchers(tmp_path):
    db = Database(f"sqlite:///{(tmp_path / 'g.db').as_posix()}")
    try:
        with db.connect() as conn:
            conn.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)"))
            conn.execute(
                text("INSERT INTO t (id, v) VALUES (:id, :v)"),
                [{"id": i, "v": str(i)} for i in range(5000)],
            )
        results: list[int] = []
        errors: list[BaseException] = []

        def reader():
            try:
                with db.readonly() as conn:
                    result = conn.execute(text("SELECT * FROM t ORDER BY id"))
                    results.append(len(fetch_all(result, db, chunk=100)))
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=reader) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        assert errors == [] and results == [5000] * 6
        gate = fetch_gate(db)
        assert gate._owner is None and _queued(gate) == 0
    finally:
        db.dispose()


# ---------------------------------------------------------------------------
# 表の取得・監査ログの一覧もゲートを通す
# ---------------------------------------------------------------------------
def _repository(tmp_path):
    config = MonitorConfig(
        tables={"m": TableDef(columns={"id": "int", "v": "str"}, primary_key="id")}
    )
    db = Database(f"sqlite:///{(tmp_path / 'r.db').as_posix()}")
    registry = TableRegistry(config)
    registry.create_all(db)
    return db, TableRepository(db, registry)


def _blocks_while_the_gate_is_held(db, call) -> None:
    done = threading.Event()
    box: list = []

    def run():
        box.append(call())
        done.set()

    with fetch_gate(db):
        thread = threading.Thread(target=run)
        thread.start()
        assert not done.wait(0.2)  # ゲートが空くまで取り出せない
    assert done.wait(5)
    thread.join(5)
    return box[0]


def test_table_listing_goes_through_the_gate(tmp_path):
    db, repo = _repository(tmp_path)
    try:
        repo.insert_many("m", [{"id": i, "v": f"x{i}"} for i in range(1, 4)])
        rows = _blocks_while_the_gate_is_held(db, lambda: repo.list("m"))
        assert rows == [
            {"id": 1, "v": "x1"},
            {"id": 2, "v": "x2"},
            {"id": 3, "v": "x3"},
        ]
        assert repo.list("m", limit=1, offset=1) == [{"id": 2, "v": "x2"}]
    finally:
        db.dispose()


def test_audit_listing_goes_through_the_gate(tmp_path):
    db = Database(f"sqlite:///{(tmp_path / 'a.db').as_posix()}")
    try:
        audit = AuditService(db, enabled=True)
        audit.record("m", 1, "create", after={"id": 1, "v": "x"})
        entries = _blocks_while_the_gate_is_held(db, lambda: audit.list_recent())
        assert [(e["table_name"], e["action"], e["after"]) for e in entries] == [
            ("m", "create", {"id": 1, "v": "x"})
        ]
    finally:
        db.dispose()


def test_gate_is_not_used_for_server_databases():
    """PostgreSQL / MySQL では fetchall のまま(ゲートは何もしない)。"""
    db = Database("postgresql+pg8000://u:p@127.0.0.1:1/none")
    try:
        assert not isinstance(fetch_gate(db), FetchGate)
    finally:
        db.dispose()


def test_big_table_reads_do_not_convoy(tmp_path):
    """大きな表を 8 スレッドで同時に取得しても、取り出しを順番に行うため数倍で終わる。

    ゲートが無いと行ごとの GIL の受け渡しが詰まり(GIL コンボイ)、1 スレッドで数 ms の
    1 万行が 8 スレッドでは数秒かかる(実測: 5 ms → 2.7 秒。ゲートありは 64 ms)。
    """
    db, repo = _repository(tmp_path)
    try:
        repo.insert_many("m", [{"id": i, "v": f"value-{i}"} for i in range(1, 10_001)])
        repo.list("m")
        start = time.perf_counter()
        assert len(repo.list("m")) == 10_000
        single = time.perf_counter() - start

        threads = [threading.Thread(target=lambda: repo.list("m")) for _ in range(8)]
        start = time.perf_counter()
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        together = time.perf_counter() - start
        assert together < single * 8 * 6 + 0.75
    finally:
        db.dispose()
