"""SMTP メール通知。"""

from __future__ import annotations

import smtplib
from email.message import EmailMessage

from ...settings.runtime import AppSettings


class EmailNotifier:
    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings

    def send(self, subject: str, body: str, level: str) -> None:
        s = self.settings
        msg = EmailMessage()
        msg["Subject"] = f"[{level}] {subject}"
        msg["From"] = s.smtp_user or "monitor-app@localhost"
        msg["To"] = s.alert_email_to
        msg.set_content(body)

        with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=10) as server:
            server.starttls()
            if s.smtp_user and s.smtp_password:
                server.login(s.smtp_user, s.smtp_password)
            server.send_message(msg)
