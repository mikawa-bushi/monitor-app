"""SMTP メール通知。

暗号化方式は ``MONITOR_SMTP_SECURITY`` で選ぶ:

- ``starttls``(既定)… 平文で接続して STARTTLS で暗号化する(多くはポート 587)。
- ``ssl`` … 最初から TLS で接続する SMTPS(ポート 465)。
- ``none`` … 暗号化しない。工場内の中継サーバ(ポート 25)など STARTTLS に
  対応しないサーバ向け。

``MONITOR_SMTP_PORT`` を指定しなければ、方式に合わせて 587 / 465 / 25 を使う。

starttls / ssl では、サーバーの証明書(発行元とホスト名)を OS の信頼する CA で検証する
(#D6-02。検証しないと、通信を中継できる攻撃者に SMTP のパスワードを盗まれる)。
社内の CA や自己署名の証明書を使う中継サーバーは、CA の証明書を環境変数
``SSL_CERT_FILE`` で指定するか、``MONITOR_SMTP_TLS_VERIFY=false`` で検証を止める。
"""

from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage

from ...settings.runtime import AppSettings

#: ``MONITOR_SMTP_PORT`` を指定しないときの方式ごとのポート。
DEFAULT_PORTS = {"starttls": 587, "ssl": 465, "none": 25}
#: SMTP サーバとの通信のタイムアウト(秒)。
TIMEOUT = 10
#: 証明書を検証できなかったときの案内。
VERIFY_HINT = (
    "SMTP サーバーの証明書を検証できません。社内の CA や自己署名の証明書なら、"
    "CA の証明書を環境変数 SSL_CERT_FILE で指定するか、"
    "MONITOR_SMTP_TLS_VERIFY=false で検証を止めてください"
)


def tls_context(verify: bool = True) -> ssl.SSLContext:
    """SMTP の TLS に使うコンテキスト。``verify=False`` なら証明書を検証しない。"""
    context = ssl.create_default_context()
    if not verify:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


class EmailNotifier:
    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings

    @property
    def security(self) -> str:
        return getattr(self.settings, "smtp_security", "starttls")

    @property
    def port(self) -> int:
        """実際に接続するポート。明示されていなければ方式の既定ポート。"""
        s = self.settings
        if "smtp_port" in getattr(s, "model_fields_set", {"smtp_port"}):
            return s.smtp_port
        return DEFAULT_PORTS.get(self.security, s.smtp_port)

    @property
    def verify(self) -> bool:
        """サーバーの証明書を検証するか(``MONITOR_SMTP_TLS_VERIFY``)。"""
        return bool(getattr(self.settings, "smtp_tls_verify", True))

    def _connect(self, context: ssl.SSLContext) -> smtplib.SMTP:
        host = self.settings.smtp_host
        if self.security == "ssl":
            return smtplib.SMTP_SSL(host, self.port, timeout=TIMEOUT, context=context)
        return smtplib.SMTP(host, self.port, timeout=TIMEOUT)

    def send(self, subject: str, body: str, level: str) -> None:
        s = self.settings
        msg = EmailMessage()
        msg["Subject"] = f"[{level}] {subject}"
        msg["From"] = s.smtp_user or "monitor-app@localhost"
        msg["To"] = s.alert_email_to
        msg.set_content(body)

        context = tls_context(self.verify)
        try:
            with self._connect(context) as server:
                if self.security == "starttls":
                    server.starttls(context=context)
                if s.smtp_user and s.smtp_password:
                    server.login(s.smtp_user, s.smtp_password)
                server.send_message(msg)
        except ssl.SSLCertVerificationError as exc:
            # 再試行のログ・失敗のログに直し方が出るよう、案内を足して送り直す。
            raise ssl.SSLCertVerificationError(
                f"{VERIFY_HINT}({exc.verify_message or exc})"
            ) from exc
