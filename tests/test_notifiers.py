"""通知チャネルの単体テスト。

外部送信(urllib / smtplib)はモックして、各 Notifier が正しい宛先・
ペイロード・認証ヘッダを組み立てること、``build_notifiers`` が設定から
有効チャネルだけを構築すること、``dispatch`` のルーティングと失敗分離を検証する。
"""

from __future__ import annotations

import json
import logging

import pytest

from monitor_app.services.notifiers import build_notifiers, dispatch
from monitor_app.services.notifiers.base import ConsoleNotifier
from monitor_app.services.notifiers.email import EmailNotifier
from monitor_app.services.notifiers.line import LineNotifier
from monitor_app.services.notifiers.webhook import WebhookNotifier
from monitor_app.settings.runtime import AppSettings


# ---------------------------------------------------------------------------
# テスト用ダブル
# ---------------------------------------------------------------------------
class _Recorder:
    """送信内容を記録するだけの Notifier。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def send(self, subject: str, body: str, level: str) -> None:
        self.calls.append((subject, body, level))


class _Boom:
    """常に例外を投げる Notifier(失敗分離の検証用)。"""

    def send(self, subject: str, body: str, level: str) -> None:
        raise RuntimeError("送信失敗")


class _FakeResp:
    def close(self) -> None:  # urlopen(...).close() を満たす
        pass


# ---------------------------------------------------------------------------
# ConsoleNotifier
# ---------------------------------------------------------------------------
def test_console_logs_at_warning_for_critical():
    # configure_logging が "monitor_app" を propagate=False にするため caplog
    # (root 経由)では捕捉できない。対象ロガーへ直接ハンドラを付けて検証する。
    logger = logging.getLogger("monitor_app.alerts")
    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append
    old_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        ConsoleNotifier().send("件名", "本文", "critical")
        ConsoleNotifier().send("件名", "本文", "info")
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)

    levels = {r.levelno for r in records}
    assert logging.WARNING in levels  # critical → warning ログ
    assert logging.INFO in levels  # info → info ログ


# ---------------------------------------------------------------------------
# WebhookNotifier
# ---------------------------------------------------------------------------
def test_webhook_posts_json_payload(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["data"] = req.data
        captured["content_type"] = req.get_header("Content-type")
        captured["timeout"] = timeout
        return _FakeResp()

    monkeypatch.setattr(
        "monitor_app.services.notifiers.webhook.urllib.request.urlopen", fake_urlopen
    )
    WebhookNotifier("https://hooks.example/x").send("異常", "本文だよ", "critical")

    assert captured["url"] == "https://hooks.example/x"
    assert captured["content_type"] == "application/json"
    assert captured["timeout"] == 5
    payload = json.loads(captured["data"].decode("utf-8"))
    assert payload["text"] == "[critical] 異常\n本文だよ"


# ---------------------------------------------------------------------------
# LineNotifier
# ---------------------------------------------------------------------------
def test_line_posts_with_bearer_auth(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["data"] = req.data
        captured["auth"] = req.get_header("Authorization")
        return _FakeResp()

    monkeypatch.setattr(
        "monitor_app.services.notifiers.line.urllib.request.urlopen", fake_urlopen
    )
    LineNotifier("tok123").send("異常", "本文", "warning")

    assert captured["url"] == LineNotifier.ENDPOINT
    assert captured["auth"] == "Bearer tok123"
    assert b"message=" in captured["data"]


# ---------------------------------------------------------------------------
# EmailNotifier
# ---------------------------------------------------------------------------
class _FakeSMTP:
    instances: list["_FakeSMTP"] = []

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.started_tls = False
        self.login_args = None
        self.sent_messages = []
        _FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        self.started_tls = True

    def login(self, user, password):
        self.login_args = (user, password)

    def send_message(self, msg):
        self.sent_messages.append(msg)


@pytest.fixture
def fake_smtp(monkeypatch):
    _FakeSMTP.instances.clear()
    monkeypatch.setattr("monitor_app.services.notifiers.email.smtplib.SMTP", _FakeSMTP)
    return _FakeSMTP


def test_email_sends_with_login(fake_smtp):
    settings = AppSettings(
        smtp_host="smtp.example",
        smtp_port=2525,
        smtp_user="u@example",
        smtp_password="pw",
        alert_email_to="ops@example",
    )
    EmailNotifier(settings).send("異常", "本文", "critical")

    smtp = fake_smtp.instances[-1]
    assert (smtp.host, smtp.port, smtp.timeout) == ("smtp.example", 2525, 10)
    assert smtp.started_tls is True
    assert smtp.login_args == ("u@example", "pw")
    msg = smtp.sent_messages[-1]
    assert msg["Subject"] == "[critical] 異常"
    assert msg["To"] == "ops@example"
    assert msg["From"] == "u@example"


def test_email_skips_login_without_credentials(fake_smtp):
    settings = AppSettings(smtp_host="smtp.example", alert_email_to="ops@example")
    EmailNotifier(settings).send("異常", "本文", "info")

    smtp = fake_smtp.instances[-1]
    assert smtp.started_tls is True
    assert smtp.login_args is None  # 認証情報が無ければ login しない
    # From は既定値にフォールバック
    assert smtp.sent_messages[-1]["From"] == "monitor-app@localhost"


# ---------------------------------------------------------------------------
# build_notifiers
# ---------------------------------------------------------------------------
def test_build_notifiers_console_only_by_default():
    notifiers = build_notifiers(AppSettings())
    assert set(notifiers) == {"console"}


def test_build_notifiers_adds_enabled_channels():
    settings = AppSettings(
        webhook_url="https://hooks.example/x",
        line_token="tok",
        smtp_host="smtp.example",
        alert_email_to="ops@example",
    )
    notifiers = build_notifiers(settings)
    assert set(notifiers) == {"console", "webhook", "line", "email"}
    assert isinstance(notifiers["webhook"], WebhookNotifier)
    assert isinstance(notifiers["line"], LineNotifier)
    assert isinstance(notifiers["email"], EmailNotifier)


def test_build_notifiers_email_needs_both_host_and_recipient():
    # smtp_host だけ(宛先なし)では email を作らない。
    notifiers = build_notifiers(AppSettings(smtp_host="smtp.example"))
    assert "email" not in notifiers


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------
def test_dispatch_routes_to_named_channels():
    console, webhook = _Recorder(), _Recorder()
    dispatch(
        {"console": console, "webhook": webhook},
        ["webhook"],
        subject="件名",
        body="本文",
        level="warning",
    )
    assert webhook.calls == [("件名", "本文", "warning")]
    assert console.calls == []


def test_dispatch_empty_channels_falls_back_to_console():
    console = _Recorder()
    dispatch({"console": console}, [], subject="s", body="b", level="info")
    assert console.calls == [("s", "b", "info")]


def test_dispatch_unknown_channel_falls_back_to_console():
    console = _Recorder()
    dispatch({"console": console}, ["nope"], subject="s", body="b", level="info")
    assert console.calls == [("s", "b", "info")]


def test_dispatch_isolates_channel_failure():
    console = _Recorder()
    # 失敗するチャネルがあっても、後続の console には届く。
    dispatch(
        {"console": console, "bad": _Boom()},
        ["bad", "console"],
        subject="s",
        body="b",
        level="critical",
    )
    assert console.calls == [("s", "b", "critical")]
