"""アラート通知の配送のテスト(CRIT-13)。

再試行(間隔を倍々に伸ばす)、終了時にキューを送り切る ``AlertEngine.close``、
SMTP の暗号化方式(starttls / ssl / none)、起動時の通知先の検証を確かめる。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import ClassVar

import pytest

from monitor_app import AlertRule, MonitorConfig, TableDef, ViewDef
from monitor_app.container import build_services
from monitor_app.services import notifiers as notifiers_mod
from monitor_app.services.alert_service import AlertEngine
from monitor_app.services.notifiers import (
    channel_problem,
    dispatch,
    send_with_retry,
)
from monitor_app.services.notifiers.email import EmailNotifier
from monitor_app.settings.runtime import AppSettings


def _settings(**overrides) -> AppSettings:
    values = {"database_url": "sqlite://", **overrides}
    return AppSettings(_env_file=None, **values)


def _config(*channels: str) -> MonitorConfig:
    return MonitorConfig(
        tables={"t": TableDef(columns={"id": "int", "x": "float"})},
        views={"v": ViewDef(query="SELECT * FROM t")},
        alerts=[
            AlertRule(view="v", column="x", op=">", value=0, notify=list(channels))
        ],
    )


class _Flaky:
    """最初の ``failures`` 回だけ失敗する Notifier。"""

    def __init__(self, failures: int = 0, delay: float = 0.0) -> None:
        self.failures = failures
        self.delay = delay
        self.attempts = 0
        self.sent: list[tuple[str, str, str]] = []
        self.lock = threading.Lock()

    def send(self, subject: str, body: str, level: str) -> None:
        with self.lock:
            self.attempts += 1
            attempt = self.attempts
        if self.delay:
            time.sleep(self.delay)
        if attempt <= self.failures:
            raise ConnectionError(f"一時的な失敗 {attempt}")
        with self.lock:
            self.sent.append((subject, body, level))


@pytest.fixture
def alerts_log():
    logger = logging.getLogger("monitor_app.alerts")
    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append
    old_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    yield records
    logger.removeHandler(handler)
    logger.setLevel(old_level)


def _wait_for(pred, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


# ---------------------------------------------------------------------------
# 再試行
# ---------------------------------------------------------------------------
def test_retries_with_exponential_backoff_until_success(alerts_log):
    notifier, waits = _Flaky(failures=2), []
    ok = send_with_retry(
        notifier, "webhook", "s", "b", "critical", retries=2, wait=waits.append
    )
    assert ok is True
    assert notifier.attempts == 3
    assert waits == [1.0, 2.0]
    assert sum(r.levelno == logging.WARNING for r in alerts_log) == 2


def test_gives_up_after_the_retries_and_logs_an_error(alerts_log):
    notifier, waits = _Flaky(failures=10), []
    assert not send_with_retry(
        notifier, "email", "s", "b", "info", retries=3, wait=waits.append
    )
    assert notifier.attempts == 4
    assert waits == [1.0, 2.0, 4.0]
    errors = [r for r in alerts_log if r.levelno == logging.ERROR]
    assert len(errors) == 1 and "channel=email" in errors[0].getMessage()


def test_backoff_is_capped(monkeypatch):
    waits: list[float] = []
    send_with_retry(
        _Flaky(failures=99), "x", "s", "b", "info", retries=8, wait=waits.append
    )
    assert max(waits) == notifiers_mod.MAX_RETRY_DELAY


def test_stop_request_during_backoff_makes_one_last_attempt():
    notifier = _Flaky(failures=10)
    assert not send_with_retry(
        notifier, "webhook", "s", "b", "info", retries=5, wait=lambda _s: True
    )
    assert notifier.attempts == 2  # 待ちを打ち切って 1 回だけ試し、それ以上は待たない


def test_dispatch_retries_each_channel_independently():
    good, flaky = _Flaky(), _Flaky(failures=1)
    dispatch(
        {"console": good, "webhook": flaky},
        ["console", "webhook"],
        subject="s",
        body="b",
        level="warning",
        retries=1,
        wait=lambda _s: None,
    )
    assert good.attempts == 1  # 成功したチャネルへは重複して送らない
    assert flaky.attempts == 2 and flaky.sent


def test_async_worker_uses_notify_retries(monkeypatch):
    monkeypatch.setattr(notifiers_mod, "RETRY_BACKOFF", 0.01)
    engine = AlertEngine(_config("webhook"), _settings(notify_retries=2))
    flaky = _Flaky(failures=2)
    engine._notifiers = {"console": flaky, "webhook": flaky}
    engine._use_async_notify = True
    try:
        engine.evaluate_view("v", [{"x": 1}])
        assert _wait_for(lambda: flaky.sent)
        assert flaky.attempts == 3
    finally:
        engine.close()


# ---------------------------------------------------------------------------
# 終了時の送り切り(AlertEngine.close)
# ---------------------------------------------------------------------------
def test_close_drains_the_queue_before_stopping():
    engine = AlertEngine(_config("webhook"), _settings())
    slow = _Flaky(delay=0.05)
    engine._notifiers = {"console": slow, "webhook": slow}
    engine._use_async_notify = True
    for i in range(5):
        engine._enqueue(["webhook"], f"件名{i}", "本文", "warning")
    assert engine.close(timeout=5) is True
    assert [s for s, _b, _l in slow.sent] == [f"件名{i}" for i in range(5)]
    assert engine._worker is None


def test_close_stops_retrying_and_tries_once_more(monkeypatch):
    monkeypatch.setattr(notifiers_mod, "RETRY_BACKOFF", 60.0)  # 再試行の待ちは長い
    engine = AlertEngine(_config("webhook"), _settings(notify_retries=5))
    down = _Flaky(failures=99)
    engine._notifiers = {"console": down, "webhook": down}
    engine._use_async_notify = True
    engine._enqueue(["webhook"], "s", "b", "critical")
    assert _wait_for(lambda: down.attempts == 1)
    started = time.monotonic()
    assert engine.close(timeout=5) is True
    assert time.monotonic() - started < 2.0  # 60 秒の待ちを打ち切った
    assert down.attempts == 2


def test_close_gives_up_after_the_timeout(alerts_log):
    engine = AlertEngine(_config("webhook"), _settings())
    release = threading.Event()

    class _Stuck:
        def send(self, subject, body, level):
            release.wait(5)

    engine._notifiers = {"console": _Stuck(), "webhook": _Stuck()}
    engine._use_async_notify = True
    engine._enqueue(["webhook"], "s", "b", "info")
    try:
        assert engine.close(timeout=0.1) is False
        assert any("未送信" in r.getMessage() for r in alerts_log)
    finally:
        release.set()


def test_engine_keeps_working_after_close():
    engine = AlertEngine(_config("webhook"), _settings())
    rec = _Flaky()
    engine._notifiers = {"console": rec, "webhook": rec}
    engine._use_async_notify = True
    assert engine.close() is True  # ワーカー未起動でもよい
    engine._enqueue(["webhook"], "after", "b", "info")
    assert _wait_for(lambda: rec.sent)
    assert engine.close() is True


def test_close_sends_items_left_behind_by_a_dead_worker():
    engine = AlertEngine(_config("webhook"), _settings())
    rec = _Flaky()
    engine._notifiers = {"console": rec, "webhook": rec}
    engine._notify_queue.put((["webhook"], "残り", "b", "info"))
    assert engine.close() is True
    assert [s for s, _b, _l in rec.sent] == ["残り"]


def test_services_stop_drains_alert_notifications(tmp_path):
    services = build_services(
        _config("webhook"),
        _settings(database_url=f"sqlite:///{tmp_path / 'a.db'}"),
    )
    calls: list[bool] = []
    services.alert_engine.close = lambda *a, **k: calls.append(True)
    try:
        asyncio.run(services.stop())
        assert calls == [True]
    finally:
        services.close()


# ---------------------------------------------------------------------------
# SMTP の暗号化方式
# ---------------------------------------------------------------------------
class _FakeSMTP:
    instances: ClassVar[list[_FakeSMTP]] = []
    kind = "SMTP"

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.timeout = host, port, timeout
        self.started_tls = False
        self.login_args = None
        self.sent = []
        _FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        self.started_tls = True

    def login(self, user, password):
        self.login_args = (user, password)

    def send_message(self, msg):
        self.sent.append(msg)


class _FakeSMTPSSL(_FakeSMTP):
    kind = "SMTP_SSL"


@pytest.fixture
def fake_smtp(monkeypatch):
    _FakeSMTP.instances.clear()
    monkeypatch.setattr("monitor_app.services.notifiers.email.smtplib.SMTP", _FakeSMTP)
    monkeypatch.setattr(
        "monitor_app.services.notifiers.email.smtplib.SMTP_SSL", _FakeSMTPSSL
    )
    return _FakeSMTP.instances


def _email(**overrides) -> EmailNotifier:
    values = {"smtp_host": "smtp.example", "alert_email_to": "ops@example"}
    values.update(overrides)
    return EmailNotifier(_settings(**values))


def test_ssl_uses_smtps_on_port_465_by_default(fake_smtp):
    _email(smtp_security="ssl", smtp_user="u", smtp_password="p").send(
        "件名", "本文", "info"
    )
    smtp = fake_smtp[-1]
    assert smtp.kind == "SMTP_SSL"
    assert (smtp.host, smtp.port, smtp.timeout) == ("smtp.example", 465, 10)
    assert smtp.started_tls is False  # 最初から TLS なので STARTTLS はしない
    assert smtp.login_args == ("u", "p")
    assert smtp.sent[-1]["Subject"] == "[info] 件名"


def test_none_sends_plain_smtp_on_port_25_by_default(fake_smtp):
    _email(smtp_security="none").send("件名", "本文", "warning")
    smtp = fake_smtp[-1]
    assert smtp.kind == "SMTP"
    assert smtp.port == 25
    assert smtp.started_tls is False
    assert smtp.login_args is None
    assert smtp.sent


@pytest.mark.parametrize("security", ["starttls", "ssl", "none"])
def test_an_explicit_port_is_always_used(fake_smtp, security):
    _email(smtp_security=security, smtp_port=2525).send("s", "b", "info")
    assert fake_smtp[-1].port == 2525


def test_starttls_stays_the_default(fake_smtp):
    _email().send("s", "b", "info")
    smtp = fake_smtp[-1]
    assert smtp.kind == "SMTP" and smtp.port == 587 and smtp.started_tls is True


def test_smtp_port_from_the_environment_counts_as_explicit(fake_smtp, monkeypatch):
    monkeypatch.setenv("MONITOR_SMTP_PORT", "2626")
    _email(smtp_security="ssl").send("s", "b", "info")
    assert fake_smtp[-1].port == 2626


# ---------------------------------------------------------------------------
# 起動時の通知先の検証
# ---------------------------------------------------------------------------
def test_unconfigured_and_unknown_channels_warn_once_each(alerts_log):
    config = _config("email", "slack", "webhook", "console")
    config.alerts.append(
        AlertRule(view="v", column="x", op="<", value=0, notify=["email", "slack"])
    )
    AlertEngine(config, _settings())
    warnings = [r.getMessage() for r in alerts_log if r.levelno == logging.WARNING]
    assert len(warnings) == 3  # email / slack / webhook を 1 回ずつ(console は問題なし)
    email, slack, webhook = warnings
    assert "'email'" in email
    assert "MONITOR_SMTP_HOST" in email and "MONITOR_ALERT_EMAIL_TO" in email
    assert "'slack'" in slack and "未知" in slack
    assert "MONITOR_WEBHOOK_URL" in webhook


def test_configured_channels_do_not_warn(alerts_log):
    AlertEngine(
        _config("email", "webhook"),
        _settings(
            smtp_host="smtp.example",
            alert_email_to="ops@example",
            webhook_url="https://hooks.example/x",
        ),
    )
    assert not [r for r in alerts_log if r.levelno >= logging.WARNING]


def test_partially_configured_email_names_only_the_missing_setting():
    settings = _settings(smtp_host="smtp.example")
    problem = channel_problem("email", settings, {"console": object()})
    assert "MONITOR_ALERT_EMAIL_TO" in problem and "MONITOR_SMTP_HOST" not in problem


def test_plaintext_credentials_are_warned_about():
    settings = _settings(
        smtp_host="relay.local",
        alert_email_to="ops@example",
        smtp_security="none",
        smtp_user="u",
        smtp_password="s3cr3t-pass",
    )
    problem = channel_problem("email", settings, {"email": object()})
    assert problem and "暗号化" in problem
    assert "s3cr3t-pass" not in problem  # 秘密情報はログに出さない


@pytest.mark.parametrize(
    "url", ["hooks.slack.com/services/T0/B0/SECRET", "ftp://x/SECRET", "SECRET"]
)
def test_webhook_url_without_http_scheme_is_reported_without_the_url(url):
    settings = _settings(webhook_url=url)
    problem = channel_problem("webhook", settings, {"webhook": object()})
    assert problem and "http" in problem
    assert "SECRET" not in problem  # Webhook の URL は秘密情報


def test_webhook_errors_do_not_log_the_secret_url(alerts_log):
    from monitor_app.services.notifiers.webhook import WebhookNotifier

    for url in (
        "hooks.slack.com/services/T0/B0/SECRET",  # スキームなし
        "https://hooks.slack.com/services/T0/B0/SEC RET",  # 空白(InvalidURL)
    ):
        notifier = WebhookNotifier(url)
        ok = send_with_retry(
            notifier, "webhook", "s", "b", "info", retries=1, wait=lambda _s: None
        )
        assert ok is False
    formatter = logging.Formatter()
    text = "\n".join(
        r.getMessage() + (formatter.formatException(r.exc_info) if r.exc_info else "")
        for r in alerts_log
    )
    assert "形式が正しくありません" in text
    assert "SECRET" not in text and "SEC RET" not in text


def test_invalid_smtp_security_is_rejected():
    with pytest.raises(ValueError):
        _settings(smtp_security="tls")


def test_worker_survives_unexpected_dispatch_errors(monkeypatch, alerts_log):
    import monitor_app.services.alert_service as alert_service_mod

    engine = AlertEngine(_config("webhook"), _settings())
    rec = _Flaky()
    engine._notifiers = {"console": rec, "webhook": rec}
    engine._use_async_notify = True
    real_dispatch = alert_service_mod.dispatch
    calls = {"n": 0}

    def flaky_dispatch(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("想定外")
        return real_dispatch(*args, **kwargs)

    monkeypatch.setattr(alert_service_mod, "dispatch", flaky_dispatch)
    engine._ensure_worker()
    engine._enqueue(["webhook"], "1", "b", "info")
    engine._enqueue(["webhook"], "2", "b", "info")
    assert engine.close() is True
    assert [s for s, _b, _l in rec.sent] == ["2"]  # 1 件目の失敗でワーカーは止まらない
    assert any("ディスパッチに失敗" in r.getMessage() for r in alerts_log)


def test_close_skips_stop_markers_left_in_a_dead_queue():
    engine = AlertEngine(_config("webhook"), _settings())
    rec = _Flaky()
    engine._notifiers = {"console": rec, "webhook": rec}
    engine._notify_queue.put(None)
    engine._notify_queue.put((["webhook"], "残り", "b", "info"))
    assert engine.close() is True
    assert [s for s, _b, _l in rec.sent] == ["残り"]
