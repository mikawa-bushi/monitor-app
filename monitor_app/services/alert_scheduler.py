"""アラートをバックグラウンドで定期評価する(#13)。

画面(ビュー取得)に依存せず、誰も見ていなくても閾値を評価・通知する。
FastAPI lifespan から asyncio タスクとして起動/停止する。評価は
:class:`ViewService`(:mod:`view_service`)経由で行うため、ビューキャッシュ
(#18)があれば自動的に共有される。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

logger = logging.getLogger("monitor_app.alerts")

if TYPE_CHECKING:
    from .alert_service import AlertEngine
    from .view_service import ViewService


class AlertScheduler:
    def __init__(
        self,
        engine: "AlertEngine",
        view_service: "ViewService",
        interval_seconds: float,
    ) -> None:
        self.engine = engine
        self.view_service = view_service
        self.interval = max(interval_seconds, 1.0)
        self._views = engine.alert_views()
        self._task: asyncio.Task | None = None

    def run_once(self) -> None:
        """全アラート対象ビューを 1 回評価する(同期)。"""
        for view_name in self._views:
            try:
                payload = self.view_service.get_view(view_name)
                self.engine.evaluate_view(view_name, payload["data"])
            except Exception as exc:  # noqa: BLE001 - 1 ビューの失敗で全体を止めない
                logger.warning(
                    "alert scheduler: ビュー '%s' の評価に失敗: %s", view_name, exc
                )

    async def _run(self) -> None:
        logger.info(
            "alert scheduler started (interval=%.1fs, views=%d)",
            self.interval,
            len(self._views),
        )
        try:
            while True:
                await asyncio.sleep(self.interval)
                try:
                    await asyncio.to_thread(self.run_once)
                except Exception:  # noqa: BLE001 - ループを恒久停止させない
                    logger.exception("alert scheduler tick failed; continuing")
        except asyncio.CancelledError:
            logger.info("alert scheduler stopped")
            raise

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
