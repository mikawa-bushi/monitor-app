"""サーバー DB の起動待ち(``Database.wait_until_ready``)は停止の指示で打ち切れる。

uvicorn はアプリの読み込み中に受けた SIGTERM を記録するだけで、読み込み(起動待ちを
含む)が終わるまで停止しない。``docker stop`` の猶予時間を待たずに終われるよう、待ちの
間だけハンドラを重ねて打ち切る。
"""

from __future__ import annotations

import os
import signal
import socket
import threading
import time

import pytest
from sqlalchemy.exc import DBAPIError

from monitor_app import main as main_mod
from monitor_app.cli._common import StopRequested, stop_signals
from monitor_app.db.engine import Database, DatabaseWaitAborted

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="POSIX のシグナル(SIGTERM)をプロセスへ送って確かめる"
)


def _closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def unreachable():
    db = Database(f"postgresql+pg8000://u:p@127.0.0.1:{_closed_port()}/none")
    yield db
    db.dispose()


def _sigterm_later(delay: float = 0.3) -> threading.Timer:
    timer = threading.Timer(delay, os.kill, args=(os.getpid(), signal.SIGTERM))
    timer.start()
    return timer


@pytest.fixture
def uvicorn_like_handler():
    """uvicorn の handle_exit と同じく、受けたシグナルを記録するだけのハンドラ。"""
    captured: list[int] = []
    previous = signal.signal(
        signal.SIGTERM, lambda signum, frame: captured.append(signum)
    )
    yield captured
    signal.signal(signal.SIGTERM, previous)


def test_sigterm_during_the_wait_aborts_promptly(unreachable, uvicorn_like_handler):
    installed = signal.getsignal(signal.SIGTERM)
    timer = _sigterm_later()
    started = time.monotonic()
    try:
        with pytest.raises(DatabaseWaitAborted) as info:
            unreachable.wait_until_ready(30)
    finally:
        timer.cancel()
    assert time.monotonic() - started < 2.0
    assert info.value.signum == signal.SIGTERM
    # 元のハンドラ(uvicorn)にも届き、待ちのあとは元のハンドラに戻る
    assert uvicorn_like_handler == [signal.SIGTERM]
    assert signal.getsignal(signal.SIGTERM) is installed


def test_a_raising_handler_aborts_with_its_own_exception(unreachable):
    """runserver の起動の準備中(stop_signals)は、その StopRequested で抜ける。"""
    timer = _sigterm_later()
    started = time.monotonic()
    try:
        with pytest.raises(StopRequested), stop_signals():
            unreachable.wait_until_ready(30)
    finally:
        timer.cancel()
    assert time.monotonic() - started < 2.0


def test_default_handlers_are_left_alone(unreachable, monkeypatch):
    """Python のハンドラが無い(既定の動作)シグナルには重ねない。"""
    previous = signal.signal(signal.SIGTERM, signal.SIG_DFL)
    seen: list = []
    real_sleep = time.sleep

    def sleep(seconds):
        seen.append(signal.getsignal(signal.SIGTERM))
        real_sleep(min(seconds, 0.01))

    monkeypatch.setattr("monitor_app.db.engine.time.sleep", sleep)
    try:
        with pytest.raises(DBAPIError):
            unreachable.wait_until_ready(0.3, initial_delay=0.05, max_delay=0.05)
    finally:
        signal.signal(signal.SIGTERM, previous)
    assert seen and all(handler is signal.SIG_DFL for handler in seen)


def test_waiting_outside_the_main_thread_does_not_touch_signals(unreachable):
    errors: list[BaseException] = []
    before = signal.getsignal(signal.SIGTERM)

    def worker():
        try:
            unreachable.wait_until_ready(0.3, initial_delay=0.05, max_delay=0.05)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(10)
    assert len(errors) == 1 and isinstance(errors[0], DBAPIError)
    assert signal.getsignal(signal.SIGTERM) is before


def test_app_factory_exits_cleanly_when_the_wait_is_aborted(
    tmp_path, monkeypatch, caplog
):
    import logging

    (tmp_path / "config.py").write_text(
        "from monitor_app import MonitorConfig\nconfig = MonitorConfig()\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    def aborted(config, settings):
        raise DatabaseWaitAborted(signal.SIGTERM)

    monkeypatch.setattr(main_mod, "create_app", aborted)
    monkeypatch.setattr(
        "monitor_app.logging_conf.configure_logging", lambda *a, **kw: None
    )
    monkeypatch.setattr(logging.getLogger("monitor_app"), "propagate", True)
    with (
        caplog.at_level(logging.INFO, logger="monitor_app.main"),
        pytest.raises(SystemExit) as info,
    ):
        main_mod.app_factory()
    assert info.value.code == 0
    assert "起動待ちを中止" in caplog.text
