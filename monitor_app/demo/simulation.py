"""倉庫デモの入出庫シミュレーション(``monitor-app demo`` / ``startproject --template warehouse``)。

ドメインの規則(いつ・何を入荷/出荷/移動/廃棄するか)は :class:`Warehouse` に集め、
DB・時計・共有の乱数を知らない純粋な処理にする。同じ規則を 2 か所で使う:

- :func:`history` … 「過去 7 日分」の台帳をメモリ上で作る(CSV に書き、通常の CSV
  取り込みで DB に入れる)。1 歩 = ``HISTORY_STEP`` 秒(粗い刻み)。
- :class:`WarehouseSimulator` … 起動後、``LIVE_STEP`` 秒ごとに 1 歩ぶんを DB に足す。

出荷の量は刻み ``dt`` に比例するため、粗い過去分と細かい起動後とで「1 日あたりの量」が
揃う(日別グラフ・時間帯別グラフが起動の前後でつながる)。

在庫の正(single source of truth)は DB の台帳(movements)。起動後のシミュレーターは
tick ごとに台帳から在庫と各品目の最後の入荷時刻を組み立て直して次の 1 歩を決める。
画面のフォーム・API から入力された入出庫とも食い違わない(自分だけのコピーを持たない)。

起動した直後の画面には、必ず次の 2 つが出る(:func:`history` が台帳の最後に仕込む):

- 安全在庫を割った品目(大口出荷の直後)。その品目の補充は起動の約
  ``FIRST_ARRIVAL`` 秒後に届く(前回の入荷が ``REPLENISH_CYCLE - FIRST_ARRIVAL`` 秒前)。
- 賞味期限が翌日のロット(期限の近いロットの入荷)。

不変条件:
- ロット × 棚番の在庫は負にならない(出荷・廃棄・調整は在庫の範囲で行う)
- 棚番の保管ケース数を超えて入荷・移動・調整しない
- 「移動」は同じ ts・同じ伝票番号の 2 行(−q と +q)で、合計は 0
- KEEP_DAYS 日より古い台帳は日次締め(:func:`close_books`)で「繰越」にまとめる。
  在庫(台帳の合計)は締めの前後で変わらず、台帳の行数は上限を持つ
"""

from __future__ import annotations

import csv
import math
import random
from bisect import bisect
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from itertools import accumulate
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from sqlalchemy import delete, func, insert, select

from ..services.periodic import PeriodicTask

if TYPE_CHECKING:
    from sqlalchemy import Connection, Table

    from ..container import Services

Kind = Literal["入荷", "出荷", "移動", "棚卸調整", "廃棄", "繰越"]

#: 台帳を残す日数。これより前は日次締めで「繰越」にまとめる(日別グラフの 7 日と同じ)。
KEEP_DAYS = 7
#: 起動後の 1 歩(tick の間隔、秒)。画面がおよそこの間隔で動く。
LIVE_STEP = 4.0
#: 過去分の 1 歩(秒)。量は刻みに比例するので、1 日あたりの量は起動後と同じ。
HISTORY_STEP = 120.0
#: 安全在庫 = 何日分の出荷量か(マスタの安全在庫から 1 日の出荷量を逆算する)。
SAFETY_DAYS = 2.0
#: 発注点・補充後の在庫(安全在庫に対する倍率)。
REORDER_POINT = 1.3
ORDER_UP_TO = 1.6
#: 入荷の周期(秒)。品目ごとに、前回の入荷からこの時間がたち、在庫が発注点を下回って
#: いれば入荷する(1 日 1 便)。最後の入荷の時刻は台帳から分かるので、待ちの状態を
#: 別に持たない。
REPLENISH_CYCLE = 24 * 3600.0
#: 起動から、仕込んだ安全在庫割れの品目の補充が届くまでの秒数。
FIRST_ARRIVAL = 90.0
#: 仕込んだ安全在庫割れの品目を、大口出荷で安全在庫の何倍まで減らすか。
SHORTAGE_COVER = 0.6
#: 仕込んだ期限の近いロットのケース数(起動後もしばらく画面に残る量)。
EXPIRING_CASES = 12
#: 過去分の最後の通常の 1 歩から起動までの最短の間隔(秒)。この間に仕込みの行を置く
#: (仕込んだ在庫を、起動までの出荷が消さない)。
PLANT_MARGIN = 60.0
#: 1 日あたりの移動・棚卸調整の件数。
MOVES_PER_DAY = 60.0
ADJUSTMENTS_PER_DAY = 20.0
#: 時間帯ごとの出荷量の倍率(0 時〜23 時)。件数は一定のまま量だけ昼に多くする
#: (夜に起動しても画面は動き、時間帯別グラフは昼に山ができる)。
HOURLY_FACTOR = (
    0.3, 0.25, 0.2, 0.2, 0.3, 0.5, 0.8, 1.1, 1.4, 1.5, 1.5, 1.3,
    1.1, 1.3, 1.5, 1.5, 1.4, 1.2, 1.0, 0.8, 0.6, 0.5, 0.4, 0.35,
)  # fmt: skip
STORES = tuple(f"店舗-{n:03d}" for n in range(1, 121))
SUPPLIERS = tuple(f"仕入先-{c}" for c in "ABCDEFGH")
OPERATORS = tuple(f"作業者{c}" for c in "ABCDEFGHJK")


# ---------------------------------------------------------------------------
# ドメインの型(DB の行・CSV の行と 1 対 1)
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Item:
    sku: str
    case_qty: int
    safety_stock: int
    shelf_life_days: int | None
    temp_zone: str

    @property
    def daily_demand(self) -> float:
        return self.safety_stock / SAFETY_DAYS


@dataclass(frozen=True, slots=True)
class Location:
    code: str
    zone: str
    capacity_cases: int


@dataclass(frozen=True, slots=True)
class Lot:
    lot_no: str
    sku: str
    received_on: date
    expiry_date: date | None


@dataclass(frozen=True, slots=True)
class Movement:
    ts: datetime
    kind: Kind
    lot_no: str
    location: str
    qty: int
    partner: str = ""
    ref_no: str = ""
    operator: str = ""


@dataclass(slots=True)
class Event:
    """1 歩で増えるもの(新しいロットと台帳の行)。空なら何も起きなかった。"""

    lots: list[Lot] = field(default_factory=list)
    movements: list[Movement] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Masters:
    """マスタ(items / locations)。DB の行からも CSV からも作る。"""

    items: Mapping[str, Item]
    locations: tuple[Location, ...]

    @classmethod
    def from_rows(
        cls, items: Iterable[Mapping], locations: Iterable[Mapping]
    ) -> Masters:
        """DB・CSV の行から作る(画面や API で空欄にされた値も受け付ける)。"""

        def number(value, default: int) -> int:
            return int(value) if value not in (None, "") else default

        return cls(
            {
                r["sku"]: Item(
                    r["sku"],
                    max(number(r["case_qty"], 1), 1),
                    max(number(r["safety_stock"], 0), 0),
                    number(r["shelf_life_days"], 0) or None,
                    r["temp_zone"],
                )
                for r in items
            },
            tuple(
                Location(r["code"], r["zone"], number(r["capacity_cases"], 0))
                for r in locations
            ),
        )

    @classmethod
    def from_csv(cls, csv_dir: Path) -> Masters:
        with (
            (csv_dir / "items.csv").open(encoding="utf-8", newline="") as items,
            (csv_dir / "locations.csv").open(encoding="utf-8", newline="") as locs,
        ):
            return cls.from_rows(csv.DictReader(items), csv.DictReader(locs))


# ---------------------------------------------------------------------------
# 在庫の状態と、次の 1 歩を決める規則(純粋な処理。DB を知らない)
# ---------------------------------------------------------------------------
class Warehouse:
    """ロット × 棚番の在庫と、次の入出庫を決める規則。

    ``last_received`` は品目ごとの最後の入荷の時刻(台帳の「入荷」の最大の ts)。
    :meth:`apply` が「入荷」の行から更新する。締めで畳まれた入荷は数えない(7 日より
    前の入荷は、入荷の周期より十分に古い)。
    """

    def __init__(
        self,
        masters: Masters,
        lots: Iterable[Lot] = (),
        last_received: Mapping[str, datetime] | None = None,
    ) -> None:
        self.masters = masters
        self.lots: dict[str, Lot] = {lot.lot_no: lot for lot in lots}
        self.last_received: dict[str, datetime] = dict(last_received or {})
        #: (lot_no, location) → 個数(0 の位置は持たない)
        self.stock: dict[tuple[str, str], int] = {}
        # 索引(stock から導く。apply だけが更新する)
        self._by_sku: dict[str, set[tuple[str, str]]] = defaultdict(set)
        self._on_hand: dict[str, int] = defaultdict(int)
        self._cases: dict[str, float] = defaultdict(float)  # 棚番 → 使用ケース数
        self._locations = {loc.code: loc for loc in masters.locations}
        items = list(masters.items.values())
        self._ship_skus = [i.sku for i in items]
        self._ship_cum = list(accumulate(i.daily_demand for i in items))
        self._total_demand = self._ship_cum[-1] if items else 0.0

    # --- 状態 ---------------------------------------------------------------
    def apply(self, movements: Iterable[Movement]) -> None:
        """台帳の行を在庫に反映する(未知のロット・品目の行は数えない)。"""
        for m in movements:
            lot = self.lots.get(m.lot_no)
            item = lot and self.masters.items.get(lot.sku)
            if item is None:
                continue
            key = (m.lot_no, m.location)
            qty = self.stock.get(key, 0) + m.qty
            if qty:
                self.stock[key] = qty
            else:
                self.stock.pop(key, None)
            # 出荷・移動の元に選べるのは在庫のある位置だけ(フォームの入力で負になった
            # 位置からは出さない)
            if qty > 0:
                self._by_sku[item.sku].add(key)
            else:
                self._by_sku[item.sku].discard(key)
            self._on_hand[item.sku] += m.qty
            self._cases[m.location] += m.qty / item.case_qty
            if m.kind == "入荷":
                last = self.last_received.get(item.sku)
                if last is None or m.ts > last:
                    self.last_received[item.sku] = m.ts

    def on_hand(self, sku: str) -> int:
        return self._on_hand[sku]

    def free_cases(self, loc: Location) -> int:
        return math.floor(loc.capacity_cases - self._cases[loc.code])

    def _positions(self) -> list[tuple[tuple[str, str], int]]:
        """在庫のある位置(lot_no, location)と個数。乱数で選ぶため順序を固定する。"""
        return sorted((key, qty) for key, qty in self.stock.items() if qty > 0)

    # --- 規則 ---------------------------------------------------------------
    def step(self, now: datetime, dt: float, rng: random.Random) -> Event:
        """``dt`` 秒ぶんの 1 歩を決め、状態に反映して返す。"""
        event = (
            self._dispose_expired(now)
            or self._receive(now, rng)
            or self._move_or_adjust(now, dt, rng)
            or self._ship(now, dt, rng)
        )
        return self._commit(event)

    def _commit(self, event: Event) -> Event:
        for lot in event.lots:
            self.lots[lot.lot_no] = lot
        self.apply(event.movements)
        return event

    def _dispose_expired(self, now: datetime) -> Event | None:
        today = now.date()
        expired = [
            key
            for key, qty in self.stock.items()
            if qty > 0 and (self.lots[key[0]].expiry_date or date.max) < today
        ]
        if not expired:
            return None
        lot_no, loc = min(expired)
        ref = f"DSP-{now:%y%m%d-%H%M%S}"
        return Event(
            movements=[
                Movement(
                    now,
                    "廃棄",
                    lot_no,
                    loc,
                    -self.stock[(lot_no, loc)],
                    ref_no=ref,
                    operator="在庫管理",
                )
            ]
        )

    def _receive(self, now: datetime, rng: random.Random) -> Event | None:
        """発注点を下回り、前回の入荷から REPLENISH_CYCLE たった品目を入荷する。"""
        for item in self.masters.items.values():
            if self.on_hand(item.sku) >= item.safety_stock * REORDER_POINT:
                continue
            last = self.last_received.get(item.sku)
            if last is not None and (now - last).total_seconds() < REPLENISH_CYCLE:
                continue
            event = self._replenish(item, now, rng)
            if event is not None:
                return event
        return None

    def _replenish(self, item: Item, now: datetime, rng: random.Random) -> Event | None:
        """安全在庫の ORDER_UP_TO 倍まで(少なくとも 1 ケース)入荷する。"""
        want = item.safety_stock * ORDER_UP_TO - self.on_hand(item.sku)
        made = now.date() - timedelta(days=rng.randint(0, 2))
        return self._put_away(
            item, max(math.ceil(want / item.case_qty), 1), now, made, rng
        )

    def _put_away(
        self,
        item: Item,
        cases: int,
        now: datetime,
        made: date,
        rng: random.Random,
    ) -> Event | None:
        """製造日 ``made`` の新しいロットを、温度帯の空いている棚に入荷する。"""
        received = now.date()
        prefix = f"L{received:%y%m%d}-{item.sku[-4:]}-"
        seq = 1
        while f"{prefix}{seq}" in self.lots:
            seq += 1
        expiry = (
            made + timedelta(days=item.shelf_life_days)
            if item.shelf_life_days
            else None
        )
        lot = Lot(f"{prefix}{seq}", item.sku, received, expiry)
        ref = f"ASN-{now:%y%m%d-%H%M%S}"
        supplier, operator = rng.choice(SUPPLIERS), rng.choice(OPERATORS)
        rows = []
        for loc in self._zone_locations(item.temp_zone, rng):
            put = min(self.free_cases(loc), cases)
            if put <= 0:
                continue
            rows.append(
                Movement(
                    now,
                    "入荷",
                    lot.lot_no,
                    loc.code,
                    put * item.case_qty,
                    supplier,
                    ref,
                    operator,
                )
            )
            cases -= put
            if cases <= 0:
                break
        return Event([lot], rows) if rows else None

    def _move_or_adjust(
        self, now: datetime, dt: float, rng: random.Random
    ) -> Event | None:
        r = rng.random()
        p_move = dt * MOVES_PER_DAY / 86400
        p_adjust = dt * ADJUSTMENTS_PER_DAY / 86400
        positions = self._positions() if r < p_move + p_adjust else []
        if not positions:
            return None
        if r < p_move:
            (lot_no, src), qty = rng.choice(positions)
            item = self.masters.items[self.lots[lot_no].sku]
            dests = [
                loc
                for loc in self._zone_locations(item.temp_zone, rng)
                if loc.code != src and self.free_cases(loc) > 0
            ]
            if not dests:
                return None
            moved = min(qty, self.free_cases(dests[0]) * item.case_qty)
            ref, op = f"MV-{now:%y%m%d-%H%M%S}", rng.choice(OPERATORS)
            return Event(
                movements=[
                    Movement(now, "移動", lot_no, src, -moved, "", ref, op),
                    Movement(now, "移動", lot_no, dests[0].code, moved, "", ref, op),
                ]
            )
        if r < p_move + p_adjust:
            (lot_no, loc), qty = rng.choice(positions)
            location = self._locations.get(loc)
            room = location is not None and self.free_cases(location) >= 1
            diff = max(-qty, rng.choice((-3, -2, -1, 1, 2) if room else (-2, -1)))
            ref = f"ADJ-{now:%y%m%d-%H%M%S}"
            return Event(
                movements=[
                    Movement(now, "棚卸調整", lot_no, loc, diff, "", ref, "棚卸担当")
                ]
            )
        return None

    def _ship(self, now: datetime, dt: float, rng: random.Random) -> Event:
        """品目を選び(出荷量に比例)、賞味期限の近いロットから(FEFO)出荷する。

        1 行の平均個数 = 全品目の 1 日の出荷量 × dt / 86400 × 時間帯の倍率。
        """
        if self._total_demand <= 0:
            return Event()
        sku = self._ship_skus[bisect(self._ship_cum, rng.random() * self._total_demand)]
        mean = self._total_demand * dt / 86400 * HOURLY_FACTOR[now.hour]
        want = max(1, round(rng.uniform(0.2, 1.8) * mean))
        return self._pick(sku, want, now, rng)

    def _pick(self, sku: str, want: int, now: datetime, rng: random.Random) -> Event:
        """``sku`` を ``want`` 個まで、賞味期限の近いロットから(FEFO)出荷する。

        在庫が足りなければある分だけ(無ければ空の Event。欠品で台帳は増えない)。
        """
        keys = sorted(
            self._by_sku[sku],
            key=lambda k: (self.lots[k[0]].expiry_date or date.max, k),
        )
        ref, store, op = (
            f"SO-{now:%y%m%d-%H%M%S}",
            rng.choice(STORES),
            rng.choice(OPERATORS),
        )
        rows = []
        for lot_no, loc in keys:
            take = min(self.stock[(lot_no, loc)], want)
            rows.append(Movement(now, "出荷", lot_no, loc, -take, store, ref, op))
            want -= take
            if want <= 0:
                break
        return Event(movements=rows)

    def _zone_locations(self, zone: str, rng: random.Random) -> list[Location]:
        locs = [loc for loc in self.masters.locations if loc.zone == zone]
        rng.shuffle(locs)
        return locs

    # --- 起動直後の画面のための仕込み(history だけが使う)----------------------
    def plant_shortage(self, item: Item, now: datetime, rng: random.Random) -> Event:
        """大口出荷で ``item`` を安全在庫の SHORTAGE_COVER 倍まで減らす。"""
        excess = self.on_hand(item.sku) - math.floor(item.safety_stock * SHORTAGE_COVER)
        if excess <= 0:
            return Event()
        return self._commit(self._pick(item.sku, excess, now, rng))

    def plant_expiring(self, item: Item, now: datetime, rng: random.Random) -> Event:
        """賞味期限が翌日のロットを EXPIRING_CASES ケース入荷する。"""
        made = now.date() + timedelta(days=1 - (item.shelf_life_days or 0))
        event = self._put_away(item, EXPIRING_CASES, now, made, rng)
        return self._commit(event or Event())

    def force_replenish(self, item: Item, now: datetime, rng: random.Random) -> Event:
        """入荷の周期・発注点にかかわらず ``item`` を補充する(次の便の時刻を決める)。"""
        return self._commit(self._replenish(item, now, rng) or Event())


# ---------------------------------------------------------------------------
# 過去分(seed)
# ---------------------------------------------------------------------------
def books_cutoff(now: datetime) -> datetime:
    """台帳を残す期間の始まり(KEEP_DAYS 日前の 0 時)。これより前は繰越にまとめる。"""
    return datetime.combine(now.date() - timedelta(days=KEEP_DAYS), time())


def opening_stock(masters: Masters, cutoff: datetime, rng: random.Random) -> Warehouse:
    """締めの時点(cutoff)の在庫を「繰越」で作る(各品目 安全在庫の 1.0〜1.8 倍)。"""
    wh = Warehouse(masters)
    for item in masters.items.values():
        cases = math.ceil(item.safety_stock * rng.uniform(1.0, 1.8) / item.case_qty)
        n_lots = rng.randint(1, 3)
        for n in range(n_lots):
            received = cutoff.date() - timedelta(days=rng.randint(1, 3) + 2 * n)
            lot = Lot(
                f"L{received:%y%m%d}-{item.sku[-4:]}-{n + 1}",
                item.sku,
                received,
                (
                    received + timedelta(days=item.shelf_life_days)
                    if item.shelf_life_days
                    else None
                ),
            )
            if lot.expiry_date is not None and lot.expiry_date <= cutoff.date():
                continue
            wh.lots[lot.lot_no] = lot
            share = math.ceil(cases / (n_lots - n))
            for loc in wh._zone_locations(item.temp_zone, rng):
                put = min(wh.free_cases(loc), share)
                if put <= 0:
                    continue
                wh.apply(
                    [
                        Movement(
                            cutoff,
                            "繰越",
                            lot.lot_no,
                            loc.code,
                            put * item.case_qty,
                            ref_no=f"CF-{cutoff:%y%m%d}",
                        )
                    ]
                )
                share -= put
                cases -= put
                if share <= 0:
                    break
    return wh


def planted_items(masters: Masters) -> tuple[Item, Item | None]:
    """起動直後の画面に出す品目: (安全在庫を割らせる品目, 期限の近いロットの品目)。

    安全在庫を割らせるのは安全在庫の最も小さい品目(大口出荷が出荷のグラフを乱さない)。
    期限の近いロットは賞味期間の最も短い品目(期限の無い品目しか無ければ None)。
    """
    items = list(masters.items.values())
    shortage = min(items, key=lambda i: (i.safety_stock, i.sku))
    perishable = [i for i in items if i.shelf_life_days]
    expiring = min(perishable, key=lambda i: (i.shelf_life_days, i.sku), default=None)
    return shortage, expiring


def _step_times(
    start: datetime, end: datetime, rng: random.Random
) -> Iterator[datetime]:
    """``start`` から ``end`` の手前まで、平均 HISTORY_STEP 秒の間隔の時刻(秒単位)。"""
    ts = start
    while True:
        ts += timedelta(seconds=HISTORY_STEP * rng.uniform(0.5, 1.5))
        if ts >= end:
            return
        yield ts.replace(microsecond=0)


def history(
    masters: Masters, now: datetime, *, seed: int = 0
) -> tuple[list[Lot], list[Movement]]:
    """``now`` までの KEEP_DAYS 日分の台帳(繰越 + 入出庫)を作る。

    日付はすべて ``now`` からの相対(いつ起動しても「今日」「昨日」が埋まっている)。
    ``seed`` が同じなら同じ結果(テストで固定できる)。最後に起動直後の画面のための
    行を仕込む(モジュールの説明を参照)。
    """
    rng = random.Random(seed)
    now = now.replace(microsecond=0)
    cutoff = books_cutoff(now)
    wh = opening_stock(masters, cutoff, rng)
    lots = list(wh.lots.values())
    movements = [
        Movement(cutoff, "繰越", lot_no, loc, qty, ref_no=f"CF-{cutoff:%y%m%d}")
        for (lot_no, loc), qty in wh.stock.items()
    ]

    def record(event: Event) -> None:
        lots.extend(event.lots)
        movements.extend(event.movements)

    shortage, expiring = planted_items(masters)
    # 仕込んだ品目の前回の入荷を、次の便が起動の FIRST_ARRIVAL 秒後に来る時刻に置く。
    last_delivery = now - timedelta(seconds=REPLENISH_CYCLE - FIRST_ARRIVAL)
    delivered = False
    for ts in _step_times(cutoff, now - timedelta(seconds=PLANT_MARGIN), rng):
        if not delivered and ts >= last_delivery:
            record(wh.force_replenish(shortage, last_delivery, rng))
            delivered = True
        record(wh.step(ts, HISTORY_STEP, rng))
    if not delivered:
        record(wh.force_replenish(shortage, last_delivery, rng))
    record(wh.plant_shortage(shortage, now - timedelta(seconds=40), rng))
    if expiring is not None:
        record(wh.plant_expiring(expiring, now - timedelta(seconds=20), rng))
    return lots, movements


def write_history_csv(csv_dir: Path, now: datetime, *, seed: int = 0) -> None:
    """マスタ CSV のある ``csv_dir`` に lots.csv と movements.csv を書く。"""
    lots, movements = history(Masters.from_csv(csv_dir), now, seed=seed)
    with (csv_dir / "lots.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["lot_no", "sku", "received_on", "expiry_date"])
        w.writerows(
            (lot.lot_no, lot.sku, lot.received_on, lot.expiry_date or "")
            for lot in lots
        )
    with (csv_dir / "movements.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(
            [
                "id",
                "ts",
                "kind",
                "lot_no",
                "location",
                "qty",
                "partner",
                "ref_no",
                "operator",
            ]
        )
        w.writerows(
            (
                n,
                m.ts.isoformat(sep=" "),
                m.kind,
                m.lot_no,
                m.location,
                m.qty,
                m.partner,
                m.ref_no,
                m.operator,
            )
            for n, m in enumerate(movements, 1)
        )


# ---------------------------------------------------------------------------
# 起動後(DB に読み書きする殻)
# ---------------------------------------------------------------------------
def close_books(conn: Connection, tables: Mapping[str, Table], cutoff: datetime) -> int:
    """``cutoff`` より前の台帳を、ロット × 棚番ごとの「繰越」1 行(ts=cutoff)にまとめる。

    冪等: まとめた行の ts は cutoff ちょうどなので、同じ cutoff で再実行しても対象が
    無い(0 を返す)。呼び出し側のトランザクションの中で行うため、途中で落ちても
    半端な状態は残らない。合計 0 の位置は繰越を作らず、cutoff より前に入荷して台帳から
    参照されなくなったロットも消す(lots も上限を持つ)。戻り値はまとめた位置の数。
    """
    m, lots = tables["movements"], tables["lots"]
    sums = conn.execute(
        select(m.c.lot_no, m.c.location, func.sum(m.c.qty).label("qty"))
        .where(m.c.ts < cutoff)
        .group_by(m.c.lot_no, m.c.location)
    ).all()
    if not sums:
        return 0
    conn.execute(delete(m).where(m.c.ts < cutoff))
    carried = [
        {
            "ts": cutoff,
            "kind": "繰越",
            "lot_no": r.lot_no,
            "location": r.location,
            "qty": int(r.qty),
            "partner": "",
            "ref_no": f"CF-{cutoff:%y%m%d}",
            "operator": "",
        }
        for r in sums
        if r.qty
    ]
    if carried:
        conn.execute(insert(m), carried)
    referenced = select(m.c.lot_no).where(m.c.lot_no.is_not(None))
    conn.execute(
        delete(lots).where(
            lots.c.received_on < cutoff.date(), lots.c.lot_no.not_in(referenced)
        )
    )
    return len(sums)


class WarehouseSimulator(PeriodicTask):
    """``LIVE_STEP`` 秒ごとに入出庫を 1 歩ぶん台帳に足す(デモ専用のバックグラウンドタスク)。

    tick = 1 つの書き込みトランザクション(SQLite では BEGIN IMMEDIATE):
    台帳から在庫を組み立てる → :meth:`Warehouse.step` → lots / movements に挿入 →
    日付が変わっていれば :func:`close_books`。読む・決める・書くの間にフォームや API の
    書き込みが割り込まないので、在庫を負にしない判断が DB の状態と一致する。
    接続はプールから tick ごとに借りて返す(停止後に DB ファイルを掴まない)。
    """

    name = "warehouse demo"
    tick_label = "step"

    def __init__(
        self,
        services: Services,
        *,
        interval: float = LIVE_STEP,
        clock: Callable[[], datetime] = datetime.now,
        rng: random.Random | None = None,
    ) -> None:
        super().__init__(interval)
        self.db = services.db
        self.tables = services.registry.tables
        self.on_change = services.view_cache.clear
        self._clock = clock
        self._rng = rng or random.Random()

    def tick(self) -> None:
        now = self._clock().replace(microsecond=0)
        with self.db.write_transaction() as conn:
            wh = self._load(conn)
            event = wh.step(now, self.interval, self._rng)
            if event.lots:
                conn.execute(
                    insert(self.tables["lots"]), [asdict(x) for x in event.lots]
                )
            if event.movements:
                # id は自動採番(CSV の続きから)
                conn.execute(
                    insert(self.tables["movements"]),
                    [asdict(x) for x in event.movements],
                )
            close_books(conn, self.tables, books_cutoff(now))
        self.on_change()

    def _load(self, conn: Connection) -> Warehouse:
        """マスタ・ロット・在庫(ロット × 棚番ごとの台帳の合計)・最後の入荷から作る。"""
        t = self.tables
        m, lots = t["movements"], t["lots"]
        masters = Masters.from_rows(
            conn.execute(select(t["items"])).mappings(),
            conn.execute(select(t["locations"])).mappings(),
        )
        received = conn.execute(
            select(lots.c.sku, func.max(m.c.ts))
            .join_from(m, lots, m.c.lot_no == lots.c.lot_no)
            .where(m.c.kind == "入荷")
            .group_by(lots.c.sku)
        )
        wh = Warehouse(
            masters,
            (
                Lot(r.lot_no, r.sku, r.received_on, r.expiry_date)
                for r in conn.execute(select(lots))
            ),
            # 画面から日時でない値を入れた行は数えない
            {sku: ts for sku, ts in received if isinstance(ts, datetime)},
        )
        totals = conn.execute(
            select(m.c.lot_no, m.c.location, func.sum(m.c.qty).label("qty")).group_by(
                m.c.lot_no, m.c.location
            )
        )
        wh.apply(
            Movement(datetime.min, "繰越", r.lot_no, r.location, int(r.qty or 0))
            for r in totals
        )
        return wh
