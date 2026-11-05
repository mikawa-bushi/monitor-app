"""倉庫デモの入出庫の規則(:mod:`monitor_app.demo.simulation`)。DB を使わない。

過去分(:func:`history`)の台帳を 1 行ずつ在庫に積み直して不変条件を確かめ、
:class:`Warehouse` の 1 歩の規則(FEFO・期限切れの廃棄・入荷の周期)と、起動直後の
画面に出す状態(安全在庫割れ → 約 1〜2 分で補充・期限の近いロット)を固定する。
"""

from __future__ import annotations

import random
from collections import defaultdict
from datetime import date, datetime, timedelta

import pytest

from monitor_app.demo import SOURCE
from monitor_app.demo.simulation import (
    HISTORY_STEP,
    KEEP_DAYS,
    LIVE_STEP,
    Item,
    Location,
    Lot,
    Masters,
    Movement,
    Warehouse,
    books_cutoff,
    history,
)

MASTERS = Masters.from_csv(SOURCE / "csv")
NOWS = [
    datetime(2026, 11, 5, 14, 0),
    datetime(2027, 1, 1, 0, 30),
    datetime(2026, 12, 31, 23, 59),
    datetime(2028, 2, 29, 23, 59),
]


@pytest.fixture(scope="module", params=NOWS, ids=lambda d: d.strftime("%Y%m%d-%H%M"))
def generated(request):
    now = request.param
    lots, movements = history(MASTERS, now)
    return now, lots, movements


def _stock_at_launch(lots, movements) -> tuple[dict, dict]:
    """(lot_no, location) → 個数 と 品目 → 個数(台帳の合計)。"""
    sku_of = {lot.lot_no: lot.sku for lot in lots}
    positions: dict = defaultdict(int)
    by_sku: dict = defaultdict(int)
    for m in movements:
        positions[(m.lot_no, m.location)] += m.qty
        by_sku[sku_of[m.lot_no]] += m.qty
    return positions, by_sku


def _rebuilt(lots, movements) -> Warehouse:
    """起動後のシミュレーターと同じく、台帳から在庫を組み立てた Warehouse。"""
    wh = Warehouse(MASTERS, lots)
    wh.apply(movements)
    return wh


def test_replaying_the_ledger_never_breaks_stock_or_capacity(generated):
    _, lots, movements = generated
    lot_by_no = {lot.lot_no: lot for lot in lots}
    capacity = {loc.code: loc.capacity_cases for loc in MASTERS.locations}
    positions: dict = defaultdict(int)
    cases: dict = defaultdict(float)
    for n, m in enumerate(movements):
        lot = lot_by_no[m.lot_no]
        item = MASTERS.items[lot.sku]
        assert m.location in capacity, m
        positions[(m.lot_no, m.location)] += m.qty
        cases[m.location] += m.qty / item.case_qty
        assert positions[(m.lot_no, m.location)] >= 0, (n, m)
        assert cases[m.location] <= capacity[m.location] + 1e-9, (n, m)


def test_quantity_signs_follow_the_kind(generated):
    _, _, movements = generated
    sign = {"入荷": 1, "繰越": 1, "出荷": -1, "廃棄": -1}
    for m in movements:
        assert m.qty != 0, m
        if m.kind in sign:
            assert m.qty * sign[m.kind] > 0, m


def test_moves_are_pairs_that_sum_to_zero(generated):
    _, _, movements = generated
    pairs: dict = defaultdict(list)
    for m in movements:
        if m.kind == "移動":
            pairs[(m.ts, m.ref_no)].append(m)
    assert pairs, "7 日分に移動が 1 件も無い"
    for rows in pairs.values():
        assert len(rows) == 2 and sum(r.qty for r in rows) == 0, rows
        assert rows[0].lot_no == rows[1].lot_no
        assert rows[0].location != rows[1].location


def test_timestamps_span_the_kept_days_up_to_now(generated):
    now, _, movements = generated
    cutoff = books_cutoff(now)
    assert cutoff == datetime.combine(
        now.date() - timedelta(days=KEEP_DAYS), datetime.min.time()
    )
    for m in movements:
        assert cutoff <= m.ts < now, m
        assert (m.kind == "繰越") == (m.ts == cutoff), m
    days = {m.ts.date() for m in movements if m.kind == "出荷"}
    expected = {now.date() - timedelta(days=d) for d in range(1, KEEP_DAYS + 1)}
    assert expected <= days
    if now.hour >= 1:
        assert now.date() in days
    assert len(movements) < 1.6 * KEEP_DAYS * 86400 / HISTORY_STEP


def test_dates_are_relative_to_now(generated):
    now, lots, movements = generated
    today = now.date()
    positions, _ = _stock_at_launch(lots, movements)
    expiry = {lot.lot_no: lot.expiry_date for lot in lots}
    assert all(lot.received_on <= today for lot in lots)
    for (lot_no, _loc), qty in positions.items():
        if qty > 0 and expiry[lot_no] is not None:
            assert expiry[lot_no] >= today, lot_no
    assert any(
        qty > 0 and expiry[lot_no] and expiry[lot_no] <= today + timedelta(days=14)
        for (lot_no, _loc), qty in positions.items()
    )


def test_same_now_and_seed_give_the_same_ledger():
    now = NOWS[0]
    assert history(MASTERS, now) == history(MASTERS, now)
    assert history(MASTERS, now, seed=1) != history(MASTERS, now)


def test_launch_shows_a_shortage_and_a_lot_expiring_within_two_days(generated):
    now, lots, movements = generated
    positions, by_sku = _stock_at_launch(lots, movements)
    below = [
        sku for sku, item in MASTERS.items.items() if by_sku[sku] < item.safety_stock
    ]
    assert below, "安全在庫を割った品目が無い(警告のバナーが出ない)"
    expiry = {lot.lot_no: lot.expiry_date for lot in lots}
    days_left = [
        (expiry[lot_no] - now.date()).days
        for (lot_no, _loc), qty in positions.items()
        if qty > 0 and expiry[lot_no] is not None
    ]
    assert any(0 <= d <= 2 for d in days_left), "期限まで 2 日以内のロットが無い"


def test_first_replenishment_of_the_shortage_arrives_one_to_two_minutes_after_launch(
    generated,
):
    now, lots, movements = generated
    _, by_sku = _stock_at_launch(lots, movements)
    short = {
        sku for sku, item in MASTERS.items.items() if by_sku[sku] < item.safety_stock
    }
    wh = _rebuilt(lots, movements)
    rng = random.Random(7)
    arrivals: dict[str, datetime] = {}
    for k in range(1, 46):
        ts = now + timedelta(seconds=LIVE_STEP * k)
        for m in wh.step(ts, LIVE_STEP, rng).movements:
            if m.kind == "入荷":
                arrivals.setdefault(wh.lots[m.lot_no].sku, ts)
    first = min(arrivals.get(sku, datetime.max) for sku in short)
    assert now + timedelta(seconds=30) <= first <= now + timedelta(seconds=120)


def _tiny(shelf_life: int | None = 10, safety: int = 100) -> Masters:
    return Masters(
        {"SKU-0001": Item("SKU-0001", 10, safety, shelf_life, "常温")},
        (Location("A-01-01", "常温", 50), Location("A-01-02", "常温", 50)),
    )


MID_CYCLE_DELIVERY = datetime(2026, 11, 5, 6, 0)


def _seeded(masters: Masters, lots: list[Lot], rows: list[tuple]) -> Warehouse:
    wh = Warehouse(masters, lots, {sku: MID_CYCLE_DELIVERY for sku in masters.items})
    t0 = datetime(2026, 11, 1)
    wh.apply(Movement(t0, "繰越", lot, loc, qty) for lot, loc, qty in rows)
    return wh


def _ships(wh: Warehouse, now: datetime, rng: random.Random, n: int) -> list[Movement]:
    out: list[Movement] = []
    for k in range(n):
        out += wh.step(now + timedelta(seconds=k), LIVE_STEP, rng).movements
    return out


def test_shipping_takes_the_earliest_expiry_first():
    masters = _tiny()
    lots = [
        Lot("L-LATE", "SKU-0001", date(2026, 11, 1), date(2026, 11, 30)),
        Lot("L-SOON", "SKU-0001", date(2026, 11, 1), date(2026, 11, 10)),
    ]
    wh = _seeded(
        masters, lots, [("L-LATE", "A-01-01", 150), ("L-SOON", "A-01-02", 150)]
    )
    now = datetime(2026, 11, 5, 10, 0)
    shipped = [m for m in _ships(wh, now, random.Random(0), 30) if m.kind == "出荷"]
    assert shipped
    soon_left = wh.stock.get(("L-SOON", "A-01-02"), 0)
    for m in shipped:
        if m.lot_no == "L-LATE":
            assert soon_left == 0
    assert shipped[0].lot_no == "L-SOON"


def test_expired_lot_is_disposed_before_anything_else():
    masters = _tiny()
    lots = [Lot("L-OLD", "SKU-0001", date(2026, 10, 1), date(2026, 11, 4))]
    wh = _seeded(masters, lots, [("L-OLD", "A-01-01", 120)])
    event = wh.step(datetime(2026, 11, 5, 9, 0), LIVE_STEP, random.Random(0))
    assert [(m.kind, m.lot_no, m.qty) for m in event.movements] == [
        ("廃棄", "L-OLD", -120)
    ]
    assert wh.on_hand("SKU-0001") == 0


def test_an_item_without_stock_ships_nothing():
    masters = _tiny()
    wh = Warehouse(masters, [], {"SKU-0001": MID_CYCLE_DELIVERY})
    for k in range(50):
        event = wh.step(datetime(2026, 11, 5, 12, 0, k), LIVE_STEP, random.Random(k))
        assert event.movements == [] and event.lots == []


def test_replenishment_waits_for_the_cycle_and_respects_capacity():
    masters = _tiny(safety=1000)
    lot = Lot("L-A", "SKU-0001", date(2026, 11, 1), date(2026, 11, 20))
    t0 = datetime(2026, 11, 5, 8, 0)
    wh = Warehouse(masters, [lot], {"SKU-0001": t0})
    wh.apply([Movement(t0, "入荷", "L-A", "A-01-01", 100)])
    rng = random.Random(0)
    within = wh.step(t0 + timedelta(hours=23, minutes=59), LIVE_STEP, rng)
    assert all(m.kind != "入荷" for m in within.movements)
    room = sum(wh.free_cases(loc) for loc in masters.locations)
    due = wh.step(t0 + timedelta(hours=24), LIVE_STEP, rng)
    assert {m.kind for m in due.movements} == {"入荷"}
    assert sum(m.qty for m in due.movements) == room * 10
    assert [wh.free_cases(loc) for loc in masters.locations] == [0, 0]
    again = wh.step(t0 + timedelta(hours=24, seconds=4), LIVE_STEP, rng)
    assert all(m.kind != "入荷" for m in again.movements)


def test_positions_made_negative_by_hand_are_never_shipped_from():
    masters = _tiny()
    lots = [Lot("L-A", "SKU-0001", date(2026, 11, 1), date(2026, 11, 30))]
    wh = _seeded(masters, lots, [("L-A", "A-01-01", 20), ("L-A", "A-01-02", -30)])
    out = _ships(wh, datetime(2026, 11, 5, 12, 0), random.Random(0), 200)
    assert any(m.kind == "出荷" for m in out)
    assert all(m.location == "A-01-01" for m in out if m.kind != "棚卸調整"), out
    assert wh.stock[("L-A", "A-01-02")] == -30
    assert wh.stock.get(("L-A", "A-01-01"), 0) >= 0


def test_apply_keeps_on_hand_equal_to_the_positions():
    lots, movements = history(MASTERS, NOWS[0])
    wh = _rebuilt(lots, movements)
    rng = random.Random(3)
    now = NOWS[0]
    for k in range(500):
        wh.step(now + timedelta(seconds=LIVE_STEP * k), LIVE_STEP, rng)
        if k % 50 == 0:
            totals: dict = defaultdict(int)
            for (lot_no, _loc), qty in wh.stock.items():
                totals[wh.lots[lot_no].sku] += qty
            assert all(wh.on_hand(sku) == totals[sku] for sku in MASTERS.items)
            assert all(qty != 0 for qty in wh.stock.values())
