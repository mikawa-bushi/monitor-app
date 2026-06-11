"""LINE Notify 通知。現場のスマホへ手軽にプッシュできる。"""

from __future__ import annotations

import urllib.parse
import urllib.request


class LineNotifier:
    ENDPOINT = "https://notify-api.line.me/api/notify"

    def __init__(self, token: str) -> None:
        self.token = token

    def send(self, subject: str, body: str, level: str) -> None:
        data = urllib.parse.urlencode({"message": f"[{level}] {subject}\n{body}"})
        req = urllib.request.Request(
            self.ENDPOINT,
            data=data.encode("utf-8"),
            headers={"Authorization": f"Bearer {self.token}"},
        )
        urllib.request.urlopen(req, timeout=5).close()
