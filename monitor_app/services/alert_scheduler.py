"""アラートをバックグラウンドで定期評価する(#13)。

画面(ビュー取得)に依存せず、誰も見ていなくても閾値を評価・通知する。
FastAPI lifespan から asyncio タスクとして起動/停止する(ループと起動/停止は
:class:`~monitor_app.services.periodic.PeriodicTask` が担う)。評価は
:class:`ViewService`(:mod:`view_service`)経由で行うため、ビューキャッシュ
(#18)があれば自動的に共有される。データが前回の評価から変わっていなければ
(スナップショットのハッシュが同じなら)評価し直さない。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .periodic import PeriodicTask

logger = logging.getLogger("monitor_app.alerts")

if TYPE_CHECKING:
    from .alert_service import AlertEngine
    from .view_service import ViewService


class AlertScheduler(PeriodicTask):
    name = "alert scheduler"
    _log = logger

    def __init__(
        self,
        engine: AlertEngine,
        view_service: ViewService,
        interval_seconds: float,
    ) -> None:
        super().__init__(max(interval_seconds, 1.0))
        self.engine = engine
        self.view_service = view_service
        self._views = engine.alert_views()

    def _evaluate(self, view_name: str) -> None:
        get_snapshot = getattr(self.view_service, "get_snapshot", None)
        evaluate = getattr(self.engine, "evaluate_view_if_changed", None)
        if get_snapshot is None or evaluate is None:
            # 旧来のインターフェースだけを持つサービス(差し替え・テスト用の代役)。
            payload = self.view_service.get_view(view_name)
            self.engine.evaluate_view(view_name, payload["data"])
            return
        snap = get_snapshot(view_name)
        evaluate(view_name, snap.digest, snap.rows, version=snap.seq)

    def run_once(self) -> None:
        """全アラート対象ビューを 1 回評価する(同期)。"""
        for view_name in self._views:
            try:
                self._evaluate(view_name)
            except Exception as exc:  # noqa: BLE001 - 1 ビューの失敗で全体を止めない
                self.note_error(exc)
                logger.warning(
                    "alert scheduler: ビュー '%s' の評価に失敗: %s", view_name, exc
                )

    def tick(self) -> None:
        self.run_once()

    def _log_started(self) -> None:
        self._log.info(
            "alert scheduler started (interval=%.1fs, views=%d)",
            self.interval,
            len(self._views),
        )
