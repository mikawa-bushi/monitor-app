"""通知チャネルの基底と、既定の console 実装。"""

from __future__ import annotations

import logging
from typing import Protocol

logger = logging.getLogger("monitor_app.alerts")


class Notifier(Protocol):
    def send(self, subject: str, body: str, level: str) -> None: ...


class ConsoleNotifier:
    """ログに出力するだけの既定チャネル。外部設定なしで常に使える。"""

    def send(self, subject: str, body: str, level: str) -> None:
        log = logger.warning if level in ("warning", "critical") else logger.info
        log("[ALERT:%s] %s — %s", level, subject, body)
