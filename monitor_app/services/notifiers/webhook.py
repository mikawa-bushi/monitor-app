"""Webhook 通知(Slack / Teams / 汎用 JSON POST)。"""

from __future__ import annotations

import json
import urllib.request


class WebhookNotifier:
    def __init__(self, url: str) -> None:
        self.url = url

    def send(self, subject: str, body: str, level: str) -> None:
        # Slack / Teams いずれも "text" フィールドを解釈できる汎用ペイロード。
        payload = json.dumps({"text": f"[{level}] {subject}\n{body}"}).encode("utf-8")
        req = urllib.request.Request(
            self.url, data=payload, headers={"Content-Type": "application/json"}
        )
        urllib.request.urlopen(req, timeout=5).close()
