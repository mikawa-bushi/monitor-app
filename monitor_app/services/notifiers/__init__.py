"""通知チャネル(フェーズ1・A)。

各 Notifier は ``send(subject, body, level)`` を実装する。接続情報が未設定の
チャネルは生成されない。``build_notifiers`` が AppSettings から有効なものを組み立てる。
"""

from __future__ import annotations

import logging
from typing import Dict, List

from ...settings.runtime import AppSettings
from .base import ConsoleNotifier, Notifier
from .email import EmailNotifier
from .line import LineNotifier
from .webhook import WebhookNotifier

logger = logging.getLogger("monitor_app.alerts")


def build_notifiers(settings: AppSettings) -> Dict[str, Notifier]:
    """設定から有効な通知チャネルを名前→Notifier の辞書で返す。"""
    notifiers: Dict[str, Notifier] = {"console": ConsoleNotifier()}
    if settings.webhook_url:
        notifiers["webhook"] = WebhookNotifier(settings.webhook_url)
    if settings.line_token:
        notifiers["line"] = LineNotifier(settings.line_token)
    if settings.smtp_host and settings.alert_email_to:
        notifiers["email"] = EmailNotifier(settings)
    return notifiers


def dispatch(
    notifiers: Dict[str, Notifier],
    channels: List[str],
    subject: str,
    body: str,
    level: str,
) -> None:
    """指定チャネルへ通知する。未知/無効チャネルは console にフォールバック。"""
    targets = channels or ["console"]
    for name in targets:
        notifier = notifiers.get(name) or notifiers["console"]
        try:
            notifier.send(subject, body, level)
        except Exception:  # noqa: BLE001 - 1 チャネルの失敗で他を止めない
            logger.exception("通知の送信に失敗しました (channel=%s)", name)


__all__ = ["Notifier", "build_notifiers", "dispatch"]
