"""KPI サマリーカードの評価(フェーズ2・E)。

各カードはスカラー 1 値を返す SELECT を持つ。読み取り専用接続で実行し、
目標との比較で状態(good / bad / neutral)を付けて返す。全カードの評価結果は
ビューと同じ共有キャッシュ(キー ``('__kpis__',)``)に載せ、ホーム・Kiosk の
定期ポーリングでデータが変わっていなければ再評価しない。
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from ..db.engine import Database
from ..db.gate import fetch_gate
from ..settings.declarative import KpiCard, MonitorConfig
from .view_cache import ViewCache
from .view_service import is_time_relative

logger = logging.getLogger("monitor_app.kpi")

#: 共有キャッシュ上のキー(ビュー名と衝突しないタプル)。
CACHE_KEY = ("__kpis__",)


def _all_succeeded(result: tuple[list[dict[str, Any]], bool]) -> bool:
    """失敗した KPI を含む結果はキャッシュに残さない(次の呼び出しで評価し直す)。"""
    return not result[1]


class KpiService:
    def __init__(
        self,
        config: MonitorConfig,
        db: Database,
        cache: ViewCache | None = None,
    ) -> None:
        self.config = config
        self.db = db
        self.cache = cache

    def _evaluate_card(
        self, conn, key: str, card: KpiCard
    ) -> tuple[dict[str, Any], bool]:
        """1 枚を評価し、(結果, 失敗したか) を返す。"""
        value: float | None = None
        failed = False
        try:
            result = conn.execute(text(card.query))
            with fetch_gate(self.db):
                row = result.first()
            if row is not None and row[0] is not None:
                value = float(row[0])
        except (SQLAlchemyError, TypeError, ValueError):
            failed = True
            logger.exception("KPI '%s' の評価に失敗しました", key)
            # 共有コネクションを後続 KPI のため健全な状態(読み取り専用)へ戻す。
            # 戻せなければ、残りの KPI を評価せずに失敗させる。
            self.db.rollback_readonly(conn)

        return {
            "key": key,
            "title": card.title,
            "value": value,
            "display": self._format(card, value),
            "unit": card.unit,
            "target": card.target,
            "status": self._status(card, value),
            "link_view": card.link_view,
        }, failed

    @staticmethod
    def _format(card: KpiCard, value: float | None) -> str:
        if value is None:
            return "—"
        try:
            return card.format.format(value)
        except (ValueError, KeyError, IndexError, TypeError):
            # 書けない形式("{}{}"・"{:d}" など)は値をそのまま出す(check が警告する)
            return str(value)

    @staticmethod
    def _status(card: KpiCard, value: float | None) -> str:
        """目標との比較で good / bad / neutral を返す。"""
        if value is None or card.target is None:
            return "neutral"
        meets = value >= card.target if card.higher_is_better else value <= card.target
        return "good" if meets else "bad"

    def _compute(self) -> tuple[list[dict[str, Any]], bool]:
        with self.db.readonly() as conn:
            results = [
                self._evaluate_card(conn, key, card)
                for key, card in self.config.kpis.items()
            ]
        return [r for r, _ in results], any(failed for _, failed in results)

    def evaluate_all(self) -> list[dict[str, Any]]:
        """全 KPI を 1 本の読み取り専用コネクションで評価する(#V4)。

        以前は KPI ごとにコネクションを開いており、home/kiosk の定期ポーリングで
        「KPI 枚数 × 頻度」の接続が発生していた。結果は共有キャッシュに載せ、
        失敗した KPI を含む結果は残さない(次の呼び出しで評価し直す)。
        """
        if self.cache is None:
            return self._compute()[0]
        time_relative = any(
            is_time_relative(c.query) for c in self.config.kpis.values()
        )
        cards, _failed = self.cache.get_or_compute(
            CACHE_KEY,
            self._compute,
            time_relative=time_relative,
            cacheable=_all_succeeded,
        )
        # 呼び出し側が書き換えても共有キャッシュを壊さないようコピーを返す。
        return [dict(card) for card in cards]
