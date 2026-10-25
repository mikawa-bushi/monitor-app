"""通知チャネル(フェーズ1・A)。

各 Notifier は ``send(subject, body, level)`` を実装する。接続情報が未設定の
チャネルは生成されない。``build_notifiers`` が AppSettings から有効なものを組み立てる。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from urllib.parse import urlsplit

from ...settings.runtime import AppSettings
from .base import ConsoleNotifier, Notifier
from .email import EmailNotifier
from .webhook import WebhookNotifier

logger = logging.getLogger("monitor_app.alerts")

#: 組み込みの通知チャネル名。
KNOWN_CHANNELS = ("console", "webhook", "email")
#: 再試行の初回の待ち(秒)。2 回目以降は倍々に伸ばす。
RETRY_BACKOFF = 1.0
#: 再試行の待ちの上限(秒)。
MAX_RETRY_DELAY = 30.0


def build_notifiers(settings: AppSettings) -> dict[str, Notifier]:
    """設定から有効な通知チャネルを名前→Notifier の辞書で返す。"""
    notifiers: dict[str, Notifier] = {"console": ConsoleNotifier()}
    if settings.webhook_url:
        notifiers["webhook"] = WebhookNotifier(settings.webhook_url)
    if settings.smtp_host and settings.alert_email_to:
        notifiers["email"] = EmailNotifier(settings)
    return notifiers


def channel_problem(
    name: str, settings: AppSettings, notifiers: dict[str, Notifier]
) -> str | None:
    """チャネル ``name`` で通知が届かない(または危険な)理由。問題なければ None。

    未知・未設定のチャネルは console へ出力される(``dispatch`` のフォールバック)。
    """
    if name not in KNOWN_CHANNELS:
        known = " / ".join(KNOWN_CHANNELS)
        return f"未知のチャネルです({known} のいずれか)。console に出力します"
    if name == "webhook":
        if name not in notifiers:
            return "MONITOR_WEBHOOK_URL が未設定のため送れません。console に出力します"
        # URL 自体は秘密情報(Slack の Webhook など)を含むため、メッセージに出さない。
        scheme = urlsplit(settings.webhook_url or "").scheme.lower()
        if scheme not in ("http", "https"):
            return (
                "MONITOR_WEBHOOK_URL が http:// または https:// で始まっていないため"
                "送れません"
            )
    if name == "email":
        if name not in notifiers:
            missing = [
                env
                for env, value in (
                    ("MONITOR_SMTP_HOST", settings.smtp_host),
                    ("MONITOR_ALERT_EMAIL_TO", settings.alert_email_to),
                )
                if not value
            ]
            return (
                f"{' と '.join(missing)} が未設定のため送れません。console に出力します"
            )
        security = getattr(settings, "smtp_security", "starttls")
        if security == "none" and settings.smtp_user and settings.smtp_password:
            return (
                "MONITOR_SMTP_SECURITY=none のため、SMTP の認証情報が暗号化されずに"
                "送られます(starttls か ssl を推奨)"
            )
        verify = getattr(settings, "smtp_tls_verify", True)
        if security != "none" and not verify and settings.smtp_password:
            return (
                "MONITOR_SMTP_TLS_VERIFY=false のため、SMTP サーバーの証明書を検証しません。"
                "通信を中継できる相手に SMTP のパスワードを盗まれるおそれがあります"
                "(社内の CA なら SSL_CERT_FILE で CA の証明書を指定して検証を推奨)"
            )
    return None


def send_with_retry(
    notifier: Notifier,
    name: str,
    subject: str,
    body: str,
    level: str,
    *,
    retries: int = 0,
    backoff: float | None = None,
    wait: Callable[[float], object] | None = None,
) -> bool:
    """1 チャネルへ送る。失敗したら最大 ``retries`` 回、間隔を倍々に伸ばして再試行する。

    ``wait(秒)`` は待ちの関数(既定は ``time.sleep``)。真を返したら(終了要求など)
    待ちを打ち切ってもう 1 回だけ試し、それ以上は再試行しない。成功したら True。
    """
    attempts = max(int(retries), 0) + 1
    base = RETRY_BACKOFF if backoff is None else backoff
    stopping = False
    for attempt in range(1, attempts + 1):
        try:
            notifier.send(subject, body, level)
            return True
        except Exception as exc:  # 1 チャネルの失敗で他を止めない
            if attempt >= attempts or stopping:
                logger.error(
                    "通知の送信に失敗しました (channel=%s, %d 回試行)",
                    name,
                    attempt,
                    exc_info=True,
                )
                return False
            delay = min(base * (2 ** (attempt - 1)), MAX_RETRY_DELAY)
            logger.warning(
                "通知の送信に失敗しました (channel=%s, %d/%d 回目): %s。"
                "%.0f 秒後に再試行します",
                name,
                attempt,
                attempts,
                exc,
                delay,
            )
            stopping = bool((wait or time.sleep)(delay))
    return False  # pragma: no cover - ループ内で必ず return する


def dispatch(
    notifiers: dict[str, Notifier],
    channels: list[str],
    subject: str,
    body: str,
    level: str,
    *,
    retries: int = 0,
    wait: Callable[[float], object] | None = None,
) -> None:
    """指定チャネルへ通知する。未知/無効チャネルは console にフォールバック。

    チャネルごとに独立して再試行する(成功したチャネルへは重複して送らない)。
    """
    targets = channels or ["console"]
    for name in targets:
        notifier = notifiers.get(name) or notifiers["console"]
        send_with_retry(
            notifier, name, subject, body, level, retries=retries, wait=wait
        )


__all__ = [
    "KNOWN_CHANNELS",
    "Notifier",
    "build_notifiers",
    "channel_problem",
    "dispatch",
    "send_with_retry",
]
