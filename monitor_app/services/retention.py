"""保持期間(``TableDef.retention``)を過ぎた行を定期的に削除する(CRIT-02、任意)。

センサ・PLC から追記し続けるテーブルが無制限に大きくなり、表示・集計・アラート評価が
日ごとに遅くなる(数週間でメモリ不足になる)のを防ぐ。

- ``keep_days`` … ``column`` が「現在 − keep_days」より古い行を削除する。日付・日時の列は
  日時として比べ、文字列の列は ISO 形式の文字列として比べる(行ごとの値の書式
  ``2026-10-21T08:00`` / ``2026-10-21 08:00`` / ``2026-10-21`` に合わせる。ISO 形式で
  ない値(空欄を含む)の行は比べられないため消さず、そうした値があれば警告する)。
- ``keep_rows`` … ``column``(省略時は主キー)の新しい順に N 行を残す。N 行目の値を
  ``ORDER BY ... DESC LIMIT 1 OFFSET N-1`` で求め、それより小さい行を消す(同じ値の
  行は残すため、N 行より少し多く残ることがある)。値が NULL の行は消さない。

削除は主キーで区切った小さなトランザクション(既定 5,000 行)に分けて行い、ほかの
書き込み(取り込み・画面からの登録)を長く待たせない。VACUUM は行わない(削除した
領域は SQLite が再利用する)。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Table, and_, delete, or_, select

from ..db.engine import Database
from ..db.registry import TableRegistry
from ..settings.declarative import MonitorConfig, Retention
from .periodic import PeriodicTask

logger = logging.getLogger("monitor_app.retention")

#: 1 回の DELETE(1 トランザクション)で消す最大行数。
BATCH_SIZE = 5000

_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_ISO_DATETIME = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")


class RetentionTask(PeriodicTask):
    """``TableDef.retention`` のあるテーブルから古い行を定期的に削除する。"""

    name = "retention"
    tick_label = "purge"
    _log = logger

    def __init__(
        self,
        config: MonitorConfig,
        registry: TableRegistry,
        db: Database,
        interval: float,
        *,
        on_change: Callable[[], None] | None = None,
        batch_size: int = BATCH_SIZE,
        clock: Callable[[], datetime] = datetime.now,
    ) -> None:
        super().__init__(interval)
        self.config = config
        self.registry = registry
        self.db = db
        self.on_change = on_change
        self.batch_size = max(int(batch_size), 1)
        self._clock = clock
        self.rules: dict[str, Retention] = {
            name: tdef.retention
            for name, tdef in config.tables.items()
            if tdef.retention is not None
        }
        self._warned: set = set()

    def tick(self) -> None:
        self.purge_once()

    # ------------------------------------------------------------------
    def purge_once(self) -> dict[str, int]:
        """全テーブルに 1 回ずつ保持期間を適用し、テーブルごとの削除行数を返す。

        1 テーブルの失敗(ロック待ちの時間切れなど)は警告に留め、ほかのテーブルは
        続ける。1 行でも消えたら ``on_change`` を呼ぶ(ビューのキャッシュを捨てる)。
        """
        deleted: dict[str, int] = {}
        for name, rule in self.rules.items():
            try:
                count = self._purge_table(name, rule)
            except Exception as exc:  # noqa: BLE001 - 1 テーブルの失敗で全体を止めない
                self.note_error(exc)
                logger.warning("保持期間の適用に失敗しました '%s': %s", name, exc)
                continue
            deleted[name] = count
            if count:
                logger.info("保持期間により %s から %d 行を削除しました", name, count)
        if any(deleted.values()) and self.on_change is not None:
            self.on_change()
        return deleted

    def _warn_once(self, key: str, message: str, *args: Any) -> None:
        if key not in self._warned:
            self._warned.add(key)
            logger.warning(message, *args)

    def _purge_table(self, name: str, rule: Retention) -> int:
        table = self.registry.get(name)
        total = 0
        if rule.keep_days is not None and rule.column is not None:
            cond = self._older_than(name, table, rule.column, rule.keep_days)
            if cond is not None:
                total += self._delete_where(table, cond)
        if rule.keep_rows is not None:
            column = rule.column or self.registry.primary_key(name)
            col = table.c[column]
            with self.db.readonly() as conn:
                cutoff = conn.execute(
                    select(col)
                    .where(col.is_not(None))
                    .order_by(col.desc())
                    .limit(1)
                    .offset(rule.keep_rows - 1)
                ).scalar()
            if cutoff is not None:
                total += self._delete_where(table, col < cutoff)
        return total

    def _older_than(self, name: str, table: Table, column: str, keep_days: float):
        """「column が 現在 − keep_days より古い」の条件(比べられなければ None)。"""
        col = table.c[column]
        cutoff = self._clock() - timedelta(days=keep_days)
        logical = self.config.tables[name].column_type(column)
        if logical == "datetime":
            return col < cutoff
        if logical == "date":
            return col < cutoff.date()
        if logical != "str":
            self._warn_once(
                f"{name}.type",
                "保持期間 keep_days は日付・日時・文字列の列にだけ使えます"
                "(%s.%s は %s)。この設定を飛ばします",
                name,
                column,
                logical,
            )
            return None
        # 文字列の列は、値の形(区切りが T / 空白 / 日付だけ)ごとに同じ形の基準と
        # 比べる(D3-10)。1 行だけ見た区切りで全行を比べると、' ' < 'T' のため区切りの
        # 違う新しい行まで消えていた。ISO 形式でない値(空欄の ''・2026/10/21 など)は
        # どの形にも当てはまらないため消さない。
        stamp = cutoff.strftime("%Y-%m-%d %H:%M:%S")
        iso_cond = or_(
            and_(col.like("____-__-__T%"), col < stamp.replace(" ", "T")),
            and_(col.like("____-__-__ %"), col < stamp),
            and_(col.like("____-__-__"), col < stamp[:10]),
        )
        with self.db.readonly() as conn:
            sample = conn.execute(
                select(col).where(col.is_not(None), col != "").limit(1)
            ).scalar()
        if sample is None:
            return None  # まだ値のある行が無い
        text = str(sample)
        if not (_ISO_DATETIME.match(text) or _ISO_DATE.match(text)):
            self._warn_once(
                f"{name}.format",
                "保持期間 keep_days: %s.%s の値が ISO 形式(例: 2026-10-21 08:00:00)では"
                "ないため、その行の古さを比べられません(値の例: %r)。列の型を datetime"
                " にし、既にある値もこの形にそろえてください(monitor-app import-csv で"
                " CSV から取り込み直すとそろいます)",
                name,
                column,
                text[:40],
            )
        return iso_cond

    def _delete_where(self, table: Table, cond) -> int:
        """``cond`` に合う行を ``batch_size`` 行ずつ、別々のトランザクションで消す。"""
        pk_cols = list(table.primary_key.columns)
        total = 0
        while True:
            with self.db.connect() as conn:
                if len(pk_cols) == 1:
                    pk = pk_cols[0]
                    # MySQL は削除対象の表を IN 句の副問い合わせで直接読めず、IN 句の中の
                    # LIMIT も使えないため、LIMIT 付きの派生表に包む(どの DB でも動く形)。
                    batch = (
                        select(pk).where(cond).limit(self.batch_size).subquery("_old")
                    )
                    result = conn.execute(
                        delete(table).where(pk.in_(select(batch.c[pk.name])))
                    )
                else:
                    result = conn.execute(delete(table).where(cond))
            count = max(result.rowcount or 0, 0)
            total += count
            if len(pk_cols) != 1 or count < self.batch_size:
                return total


def build_retention_task(
    config: MonitorConfig,
    registry: TableRegistry,
    db: Database,
    settings,
    *,
    on_change: Callable[[], None] | None = None,
) -> RetentionTask | None:
    """``retention`` のあるテーブルがあれば :class:`RetentionTask` を作る(無ければ None)。

    ``retention_interval_minutes`` が 0 以下なら作らない(保持期間の適用を止める)。
    """
    if settings.retention_interval_minutes <= 0:
        return None
    if not any(t.retention is not None for t in config.tables.values()):
        return None
    return RetentionTask(
        config,
        registry,
        db,
        settings.retention_interval_minutes * 60.0,
        on_change=on_change,
    )
