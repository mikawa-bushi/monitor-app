"""KPI サマリーカードの評価(フェーズ2・E)。

各カードはスカラー 1 値を返す SELECT を持つ。読み取り専用接続で実行し、
目標との比較で状態(good / bad / neutral)を付けて返す。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from ..settings.declarative import KpiCard, MonitorConfig
from ..db.engine import Database

logger = logging.getLogger("monitor_app.kpi")


class KpiService:
    def __init__(self, config: MonitorConfig, db: Database) -> None:
        self.config = config
        self.db = db

    def _evaluate_one(self, key: str, card: KpiCard) -> Dict[str, Any]:
        value: float | None = None
        try:
            with self.db.readonly() as conn:
                row = conn.execute(text(card.query)).first()
                if row is not None and row[0] is not None:
                    value = float(row[0])
        except (SQLAlchemyError, TypeError, ValueError):
            logger.exception("KPI '%s' の評価に失敗しました", key)

        return {
            "key": key,
            "title": card.title,
            "value": value,
            "display": self._format(card, value),
            "unit": card.unit,
            "target": card.target,
            "status": self._status(card, value),
            "link_view": card.link_view,
        }

    @staticmethod
    def _format(card: KpiCard, value: float | None) -> str:
        if value is None:
            return "—"
        try:
            return card.format.format(value)
        except (ValueError, KeyError):
            return str(value)

    @staticmethod
    def _status(card: KpiCard, value: float | None) -> str:
        """目標との比較で good / bad / neutral を返す。"""
        if value is None or card.target is None:
            return "neutral"
        meets = value >= card.target if card.higher_is_better else value <= card.target
        return "good" if meets else "bad"

    def evaluate_all(self) -> List[Dict[str, Any]]:
        return [self._evaluate_one(key, card) for key, card in self.config.kpis.items()]
