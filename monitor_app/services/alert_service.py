"""閾値アラートの評価とエッジ検出(フェーズ1・A)。

各ビューのデータに対しルールを評価し、違反を検出する。発火中アラートの集合を
保持し、新規発火(OFF→ON)と復帰(ON→OFF)のときだけ通知することで、
連続通知を抑制する。

並行性(#12): ``_active`` は AlertScheduler(単一ループ)と複数リクエストスレッド
から触られるため、``threading.Lock`` で保護する。
通知(#14): 外部送信(webhook/LINE/SMTP)はブロッキングなため、同期パスからは
キューに積むだけにし、デーモンワーカースレッドで送る。
"""

from __future__ import annotations

import logging
import operator
import queue
import threading
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

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

# 通知キューの 1 件: (channels, subject, body, level)。None は停止サインティネル。
_NotifyItem = Optional[Tuple[List[str], str, str, str]]


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
        # _active を保護するロック(#12)。
        self._lock = threading.Lock()
        # 非同期通知のキューとワーカー(#14)。ワーカーは初回通知時に遅延起動する。
        self._notify_queue: "queue.Queue[_NotifyItem]" = queue.Queue()
        self._worker: Optional[threading.Thread] = None
        self._worker_lock = threading.Lock()
        # webhook/LINE/SMTP などブロッキング I/O を伴う通知先がある場合のみ
        # 非同期ワーカーを使う。console だけならログ出力のみで即時送る(#14)。
        self._use_async_notify = any(
            name != "console" for name in self._notifiers
        )

    # ------------------------------------------------------------------
    # 通知(非ブロッキング)
    # ------------------------------------------------------------------
    def _ensure_worker(self) -> None:
        if self._worker is not None:
            return
        with self._worker_lock:
            if self._worker is not None:
                return
            t = threading.Thread(
                target=self._drain_notifications, name="alert-notifier", daemon=True
            )
            t.start()
            self._worker = t

    def _drain_notifications(self) -> None:
        while True:
            item = self._notify_queue.get()
            if item is None:  # 停止サインティネル
                return
            channels, subject, body, level = item
            try:
                dispatch(
                    self._notifiers, channels, subject=subject, body=body, level=level
                )
            except Exception:  # noqa: BLE001 - 通知失敗・終了時の logging 失敗で
                # ワーカースレッドを死なせない(ベストエフォートで記録)。
                try:
                    logger.warning("通知ディスパッチに失敗しました", exc_info=True)
                except Exception:  # noqa: BLE001
                    pass

    def _enqueue(self, channels: List[str], subject: str, body: str, level: str) -> None:
        if not self._use_async_notify:
            # console のみ(ブロッキング I/O 無し)は同期送信で十分。
            dispatch(
                self._notifiers, channels, subject=subject, body=body, level=level
            )
            return
        self._ensure_worker()
        self._notify_queue.put((list(channels), subject, body, level))

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

        返り値はこのビューに紐づく現在アクティブなアラート。``_active`` の更新は
        ロック下で行い、通知はロック外で(キュー経由・非ブロッキングで)送る。
        """
        results: List[ActiveAlert] = []
        edges: List[Tuple[str, AlertRule, Optional[ActiveAlert]]] = []
        with self._lock:
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
                        edges.append(("fire", rule, alert))
                elif was_active:  # ON → OFF(復帰)
                    del self._active[key]
                    edges.append(("recover", rule, None))

        for kind, rule, alert in edges:
            if kind == "fire" and alert is not None:
                self._notify(rule, alert)
            else:
                self._notify_recovery(rule)
        return results

    def _notify(self, rule: AlertRule, alert: ActiveAlert) -> None:
        self._enqueue(
            rule.notify,
            f"異常検知: {rule.view}",
            f"{alert.message}({alert.count} 件)",
            rule.level,
        )

    def _notify_recovery(self, rule: AlertRule) -> None:
        self._enqueue(
            rule.notify,
            f"復帰: {rule.view}",
            f"{rule.describe()} は解消しました",
            "info",
        )

    def alert_views(self) -> List[str]:
        """アラートルールを持つビュー名の一覧(AlertScheduler 用)。"""
        return list(self._by_view.keys())

    def active_alerts(self) -> List[dict]:
        """現在アクティブな全アラートを辞書のリストで返す。"""
        with self._lock:
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
