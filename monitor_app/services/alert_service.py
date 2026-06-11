"""閾値アラートの評価とエッジ検出(フェーズ1・A)。

各ビューのデータに対しルールを評価し、違反を検出する。発火中アラートの集合を
保持し、新規発火(OFF→ON)と復帰(ON→OFF)のときだけ通知することで、
連続通知を抑制する。
"""

from __future__ import annotations

import logging
import operator
from dataclasses import dataclass, field
from typing import Callable, Dict, List

from ..settings.declarative import AlertRule, MonitorConfig
from ..settings.runtime import AppSettings
from .notifiers import build_notifiers, dispatch

logger = logging.getLogger("monitor_app.alerts")

_OPS: Dict[str, Callable[[float, float], bool]] = {
    ">": operator.gt,
    ">=": operator.ge,
    "<": operator.lt,
    "<=": operator.le,
    "==": operator.eq,
    "!=": operator.ne,
}


@dataclass
class ActiveAlert:
    key: str
    view: str
    column: str
    level: str
    message: str
    count: int  # 違反している行数


@dataclass
class AlertEngine:
    config: MonitorConfig
    settings: AppSettings
    _active: Dict[str, ActiveAlert] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._notifiers = build_notifiers(self.settings)
        # ルールを対象ビューごとに索引化。
        self._by_view: Dict[str, List[AlertRule]] = {}
        for rule in self.config.alerts:
            self._by_view.setdefault(rule.view, []).append(rule)

    @staticmethod
    def _rule_key(rule: AlertRule) -> str:
        return f"{rule.view}:{rule.column}:{rule.op}:{rule.value}"

    def _evaluate_rule(self, rule: AlertRule, rows: List[dict]) -> int:
        """ルールに違反する行数を返す。"""
        compare = _OPS[rule.op]
        violations = 0
        for row in rows:
            raw = row.get(rule.column)
            if raw is None:
                continue
            try:
                if compare(float(raw), rule.value):
                    violations += 1
            except (TypeError, ValueError):
                continue
        return violations

    def evaluate_view(self, view_name: str, rows: List[dict]) -> List[ActiveAlert]:
        """1 つのビューのデータを評価し、発火/復帰時に通知する。

        返り値はこのビューに紐づく現在アクティブなアラート。
        """
        results: List[ActiveAlert] = []
        for rule in self._by_view.get(view_name, []):
            key = self._rule_key(rule)
            count = self._evaluate_rule(rule, rows)
            was_active = key in self._active
            if count > 0:
                alert = ActiveAlert(
                    key=key,
                    view=rule.view,
                    column=rule.column,
                    level=rule.level,
                    message=rule.describe(),
                    count=count,
                )
                self._active[key] = alert
                results.append(alert)
                if not was_active:  # OFF → ON のエッジでのみ通知
                    self._notify(rule, alert)
            elif was_active:  # ON → OFF(復帰)
                del self._active[key]
                self._notify_recovery(rule)
        return results

    def _notify(self, rule: AlertRule, alert: ActiveAlert) -> None:
        dispatch(
            self._notifiers,
            rule.notify,
            subject=f"異常検知: {rule.view}",
            body=f"{alert.message}({alert.count} 件)",
            level=rule.level,
        )

    def _notify_recovery(self, rule: AlertRule) -> None:
        dispatch(
            self._notifiers,
            rule.notify,
            subject=f"復帰: {rule.view}",
            body=f"{rule.describe()} は解消しました",
            level="info",
        )

    def active_alerts(self) -> List[dict]:
        """現在アクティブな全アラートを辞書のリストで返す。"""
        return [
            {
                "view": a.view,
                "column": a.column,
                "level": a.level,
                "message": a.message,
                "count": a.count,
            }
            for a in self._active.values()
        ]
