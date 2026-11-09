"""倉庫デモの DB 側(:func:`close_books`・:class:`WarehouseSimulator`)。

デモのプロジェクトを一時フォルダに書き、通常の CSV 取り込みで入れた SQLite のファイルに
対して、日次締めと tick を確かめる。
"""

from __future__ import annotations

import asyncio
import random
import time
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from monitor_app.cli.data import run_csv_import
from monitor_app.demo import write_project
from monitor_app.demo.simulation import WarehouseSimulator, books_cutoff, close_books
from monitor_app.main import create_app
from monitor_app.services.periodic import STOP_TIMEOUT
from monitor_app.settings.loader import load_config
from monitor_app.settings.runtime import AppSettings

STOCK_SQL = "SELECT lot_no, location, SUM(qty) FROM movements GROUP BY lot_no, location"


@pytest.fixture
def demo(tmp_path):
    """デモのプロジェクトを取り込んだアプリと、生成の基準にした時刻。"""
    now = datetime.now().replace(microsecond=0)
    write_project(tmp_path, now=now)
    config = load_config(tmp_path / "config.py")
    settings = AppSettings(
        _env_file=None,
        database_url=f"sqlite:///{(tmp_path / 'demo.db').as_posix()}",
        csv_dir=tmp_path / "csv",
    )
    run_csv_import(settings, config, only_empty=True)
    app = create_app(config, settings)
    yield app, now
    app.state.services.close()


def _rows(services, sql: str, **params) -> list[tuple]:
    params = {k: str(v) for k, v in params.items()}
    with services.db.connect() as conn:
        return [tuple(r) for r in conn.execute(text(sql), params)]


def _stock(services) -> dict[tuple[str, str], int]:
    return {(lot, loc): qty for lot, loc, qty in _rows(services, STOCK_SQL) if qty}


def _simulator(services, clock: list[datetime], seed: int = 1) -> WarehouseSimulator:
    return WarehouseSimulator(services, clock=lambda: clock[0], rng=random.Random(seed))


def test_close_books_keeps_stock_and_is_idempotent(demo):
    app, now = demo
    services = app.state.services
    tables = services.registry.tables
    before = _stock(services)
    cutoff = datetime.combine(now.date() - timedelta(days=2), datetime.min.time())
    with services.db.write_transaction() as conn:
        folded = close_books(conn, tables, cutoff)
    assert folded > 0
    assert _stock(services) == before
    assert _rows(
        services, "SELECT COUNT(*) FROM movements WHERE ts < :c", c=cutoff
    ) == [(0,)]
    carried = _rows(
        services, "SELECT COUNT(*), MIN(qty) FROM movements WHERE kind = '繰越'"
    )
    assert carried[0][0] > 0 and carried[0][1] > 0
    with services.db.write_transaction() as conn:
        assert close_books(conn, tables, cutoff) == 0
    assert _stock(services) == before


def test_close_books_drops_lots_that_nothing_references(demo):
    app, now = demo
    services = app.state.services
    cutoff = datetime.combine(now.date() - timedelta(days=2), datetime.min.time())
    with services.db.write_transaction() as conn:
        close_books(conn, services.registry.tables, cutoff)
    orphans = _rows(
        services,
        "SELECT COUNT(*) FROM lots WHERE lot_no NOT IN (SELECT lot_no FROM movements)"
        " AND received_on < :d",
        d=cutoff.date(),
    )
    assert orphans == [(0,)]
    with services.db.write_transaction() as conn:
        conn.execute(
            text("INSERT INTO lots VALUES ('L-HAND', 'SKU-1001', :d, NULL)"),
            {"d": now.date().isoformat()},
        )
        close_books(conn, services.registry.tables, cutoff + timedelta(days=1))
    assert _rows(services, "SELECT lot_no FROM lots WHERE lot_no = 'L-HAND'") == [
        ("L-HAND",)
    ]


def test_close_books_rolls_back_with_the_outer_transaction(demo):
    app, now = demo
    services = app.state.services
    count = _rows(services, "SELECT COUNT(*) FROM movements")
    cutoff = datetime.combine(now.date() - timedelta(days=2), datetime.min.time())
    with pytest.raises(RuntimeError), services.db.write_transaction() as conn:
        assert close_books(conn, services.registry.tables, cutoff) > 0
        raise RuntimeError("tick の途中で失敗")
    assert _rows(services, "SELECT COUNT(*) FROM movements") == count


def test_tick_appends_rows_after_the_imported_ids(demo):
    app, now = demo
    services = app.state.services
    (max_id,) = _rows(services, "SELECT MAX(id) FROM movements")[0]
    clock = [now]
    sim = _simulator(services, clock)
    for k in range(1, 21):
        clock[0] = now + timedelta(seconds=4 * k)
        sim.tick()
    added = _rows(services, "SELECT id, ts FROM movements WHERE id > :m", m=max_id)
    assert len(added) >= 15
    assert all(str(ts) > str(now) for _id, ts in added)


def test_tick_sees_rows_entered_through_the_form(demo):
    app, now = demo
    services = app.state.services
    clock = [now]
    sim = _simulator(services, clock, seed=5)
    positions = _rows(
        services,
        "SELECT m.lot_no, m.location, SUM(m.qty) FROM movements m"
        " JOIN lots l ON l.lot_no = m.lot_no WHERE l.sku = 'SKU-1002'"
        " GROUP BY m.lot_no, m.location HAVING SUM(m.qty) > 0",
    )
    assert positions
    for lot_no, location, qty in positions:
        services.crud_service.create_record(
            "movements",
            {
                "ts": now,
                "kind": "出荷",
                "lot_no": lot_no,
                "location": location,
                "qty": -qty,
                "operator": "作業者A",
            },
        )
    for k in range(1, 61):
        clock[0] = now + timedelta(seconds=4 * k)
        sim.tick()
    negative = _rows(services, STOCK_SQL + " HAVING SUM(qty) < 0")
    assert negative == []
    shipped = _rows(
        services,
        "SELECT COUNT(*) FROM movements m JOIN lots l ON l.lot_no = m.lot_no"
        " WHERE l.sku = 'SKU-1002' AND m.kind = '出荷' AND m.ts > :n",
        n=now,
    )
    received = _rows(
        services,
        "SELECT COUNT(*) FROM movements m JOIN lots l ON l.lot_no = m.lot_no"
        " WHERE l.sku = 'SKU-1002' AND m.kind = '入荷' AND m.ts > :n",
        n=now,
    )
    assert received[0][0] > 0 or shipped[0][0] == 0


def test_tick_after_midnight_closes_the_books_without_changing_stock(demo):
    app, now = demo
    services = app.state.services
    before = _stock(services)
    tomorrow = datetime.combine(now.date() + timedelta(days=1), datetime.min.time())
    clock = [tomorrow + timedelta(seconds=5)]
    _simulator(services, clock).tick()
    cutoff = books_cutoff(clock[0])
    assert _rows(
        services, "SELECT COUNT(*) FROM movements WHERE ts < :c", c=cutoff
    ) == [(0,)]
    assert (
        _rows(
            services,
            "SELECT COUNT(*) FROM movements WHERE kind = '繰越' AND ts = :c",
            c=cutoff,
        )[0][0]
        > 0
    )
    expected = dict(before)
    for lot, loc, qty in _rows(
        services,
        "SELECT lot_no, location, qty FROM movements WHERE ts = :t",
        t=clock[0],
    ):
        expected[(lot, loc)] = expected.get((lot, loc), 0) + qty
    assert _stock(services) == {k: v for k, v in expected.items() if v}


def test_simulator_runs_in_the_lifespan_and_reports_health(demo):
    app, _now = demo
    services = app.state.services
    sim = WarehouseSimulator(services, interval=0.05, rng=random.Random(2))
    services.background_tasks.append(sim)
    (count,) = _rows(services, "SELECT COUNT(*) FROM movements")[0]
    with TestClient(app) as client:
        deadline = time.monotonic() + 10
        while sim.last_tick is None and time.monotonic() < deadline:
            time.sleep(0.05)
        task = client.get("/api/health").json()["tasks"]["warehouse demo"]
        assert task["last_tick"] is not None and task["last_error"] is None
        assert task["running"] is True
    assert _rows(services, "SELECT COUNT(*) FROM movements")[0][0] > count
    assert not sim.running


def test_stop_returns_within_the_stop_timeout(demo):
    app, _now = demo
    sim = WarehouseSimulator(app.state.services, interval=0.01)

    async def run() -> tuple[bool, float]:
        sim.start()
        deadline = time.monotonic() + 10
        while sim.last_tick is None and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        started = time.monotonic()
        stopped = await sim.stop()
        return stopped, time.monotonic() - started

    stopped, elapsed = asyncio.run(run())
    assert stopped and elapsed < STOP_TIMEOUT
    assert sim.last_tick is not None and sim.last_error is None
