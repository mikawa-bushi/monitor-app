"""SMTP 通知の TLS: サーバーの証明書を検証する(STARTTLS・SMTPS)。

偽の SMTP サーバー(自己署名の証明書)を立て、検証しないと SMTP のパスワードが
通信を中継した相手に渡ってしまうこと、検証すれば送る前に止まることを確かめる。
社内の CA なら SSL_CERT_FILE で信頼でき、MONITOR_SMTP_TLS_VERIFY=false で検証を止められる。
"""

from __future__ import annotations

import base64
import shutil
import socket
import ssl
import subprocess
import threading
from pathlib import Path

import pytest
from contract import helpers

from monitor_app.services.notifiers import build_notifiers, channel_problem
from monitor_app.services.notifiers.email import (
    VERIFY_HINT,
    EmailNotifier,
    tls_context,
)
from monitor_app.settings.runtime import AppSettings

pytestmark = pytest.mark.skipif(
    shutil.which("openssl") is None, reason="証明書を作る openssl が無い"
)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    helpers.isolate_from_host(monkeypatch, tmp_path)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)


@pytest.fixture(scope="module")
def certificate(tmp_path_factory) -> tuple[Path, Path]:
    """127.0.0.1 向けの自己署名の証明書(信頼されていない)。"""
    folder = tmp_path_factory.mktemp("cert")
    cert, key = folder / "cert.pem", folder / "key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=127.0.0.1",
            "-addext",
            "subjectAltName=IP:127.0.0.1",
            "-keyout",
            str(key),
            "-out",
            str(cert),
        ],
        check=True,
        capture_output=True,
    )
    return cert, key


class FakeSmtpServer:
    """STARTTLS / SMTPS に対応した、受け取った AUTH を記録する最小の SMTP サーバー。"""

    def __init__(self, cert: Path, key: Path, security: str) -> None:
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(cert, key)
        self.security = security
        self.credentials: list[str] = []
        self.messages = 0
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.port = self.listener.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        try:
            conn, _ = self.listener.accept()
        except OSError:
            return
        try:
            if self.security == "ssl":
                conn = self.context.wrap_socket(conn, server_side=True)
            stream = conn.makefile("rwb")
            self._reply(stream, b"220 fake ESMTP")
            while line := stream.readline():
                command = line.decode().strip()
                verb = command.split(" ", 1)[0].upper()
                if verb == "EHLO":
                    tls = self.security == "starttls" and not isinstance(
                        conn, ssl.SSLSocket
                    )
                    lines = [b"250-fake"]
                    if tls:
                        lines.append(b"250-STARTTLS")
                    lines.append(b"250 AUTH PLAIN")
                    self._reply(stream, b"\r\n".join(lines))
                elif verb == "STARTTLS":
                    self._reply(stream, b"220 go ahead")
                    conn = self.context.wrap_socket(conn, server_side=True)
                    stream = conn.makefile("rwb")
                elif verb == "AUTH":
                    self.credentials.append(
                        base64.b64decode(command.split()[2]).decode()
                    )
                    self._reply(stream, b"235 ok")
                elif verb == "DATA":
                    self._reply(stream, b"354 go ahead")
                    while stream.readline().strip() != b".":
                        pass
                    self.messages += 1
                    self._reply(stream, b"250 ok")
                elif verb == "QUIT":
                    self._reply(stream, b"221 bye")
                    return
                else:
                    self._reply(stream, b"250 ok")
        except (ssl.SSLError, OSError):
            return
        finally:
            conn.close()
            self.listener.close()

    @staticmethod
    def _reply(stream, data: bytes) -> None:
        stream.write(data + b"\r\n")
        stream.flush()

    def close(self) -> None:
        self.listener.close()
        self.thread.join(5)


@pytest.fixture
def smtp_server(certificate):
    servers: list[FakeSmtpServer] = []

    def start(security: str) -> FakeSmtpServer:
        server = FakeSmtpServer(*certificate, security)
        servers.append(server)
        return server

    yield start
    for server in servers:
        server.close()


def _settings(port: int, security: str, **overrides) -> AppSettings:
    values = {
        "smtp_host": "127.0.0.1",
        "smtp_port": port,
        "smtp_security": security,
        "smtp_user": "alert@example.com",
        "smtp_password": "SmtpSecret_X2",
        "alert_email_to": "ops@example.com",
    }
    values.update(overrides)
    return AppSettings(**values)


@pytest.mark.parametrize("security", ["starttls", "ssl"])
def test_untrusted_certificate_stops_before_the_password_is_sent(smtp_server, security):
    server = smtp_server(security)
    with pytest.raises(ssl.SSLCertVerificationError) as info:
        EmailNotifier(_settings(server.port, security)).send("s", "b", "critical")
    assert "MONITOR_SMTP_TLS_VERIFY" in str(info.value)  # 直し方を示す
    assert "SSL_CERT_FILE" in str(info.value)
    server.close()
    assert server.credentials == []


@pytest.mark.parametrize("security", ["starttls", "ssl"])
def test_a_ca_given_by_ssl_cert_file_is_trusted(
    smtp_server, certificate, monkeypatch, security
):
    monkeypatch.setenv("SSL_CERT_FILE", str(certificate[0]))
    server = smtp_server(security)
    EmailNotifier(_settings(server.port, security)).send("s", "b", "critical")
    server.close()
    assert server.credentials == ["\x00alert@example.com\x00SmtpSecret_X2"]
    assert server.messages == 1


@pytest.mark.parametrize("security", ["starttls", "ssl"])
def test_verification_can_be_turned_off_for_self_signed_relays(smtp_server, security):
    server = smtp_server(security)
    settings = _settings(server.port, security, smtp_tls_verify=False)
    EmailNotifier(settings).send("s", "b", "critical")
    server.close()
    assert server.messages == 1


def test_tls_verify_defaults_to_on_and_reads_the_environment(monkeypatch):
    assert AppSettings().smtp_tls_verify is True
    monkeypatch.setenv("MONITOR_SMTP_TLS_VERIFY", "false")
    assert AppSettings().smtp_tls_verify is False


def test_tls_context_verifies_by_default():
    context = tls_context()
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    off = tls_context(verify=False)
    assert off.verify_mode == ssl.CERT_NONE and not off.check_hostname


def test_turning_verification_off_is_warned_like_plain_smtp():
    settings = AppSettings(
        smtp_host="smtp.example",
        alert_email_to="ops@example",
        smtp_password="p",
        smtp_tls_verify=False,
    )
    problem = channel_problem("email", settings, build_notifiers(settings))
    assert problem is not None and "MONITOR_SMTP_TLS_VERIFY=false" in problem
    verified = AppSettings(
        smtp_host="smtp.example", alert_email_to="ops@example", smtp_password="p"
    )
    assert channel_problem("email", verified, build_notifiers(verified)) is None


def test_hint_text_names_both_ways_out():
    assert "SSL_CERT_FILE" in VERIFY_HINT and "MONITOR_SMTP_TLS_VERIFY" in VERIFY_HINT
