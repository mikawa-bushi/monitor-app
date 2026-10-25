"""サーバー DB へ 1 回接続するときの待ちに上限があること。

応答の無いホスト(パケットを捨てるファイアウォールなど)では、ドライバの既定のままだと
1 回の接続に 20 秒〜2 分かかり、起動待ち(MONITOR_DB_WAIT_TIMEOUT=5 でも 22 秒)や
``check``(21 秒)・リクエストのスレッドがその間止まっていた。
"""

from __future__ import annotations

import importlib.util
import socket
import threading
import time

import pytest
from sqlalchemy import event
from sqlalchemy.exc import DBAPIError

from monitor_app.db import engine as engine_mod
from monitor_app.db.engine import DEFAULT_CONNECT_TIMEOUT, Database


class _Captured(Exception):
    pass


def _cparams(url: str, **kwargs) -> dict:
    """接続のときドライバへ渡す引数(実際には接続しない)。"""
    db = Database(url, **kwargs)
    seen: dict = {}

    def capture(_dialect, _record, _cargs, cparams):
        seen.update(cparams)
        raise _Captured

    event.listen(db.engine, "do_connect", capture)
    try:
        with pytest.raises(_Captured):
            db.engine.connect()
    finally:
        db.dispose()
    return seen


@pytest.mark.parametrize(
    ("url", "name", "value"),
    [
        ("postgresql+pg8000://u:p@127.0.0.1:1/x", "timeout", DEFAULT_CONNECT_TIMEOUT),
        ("mysql+pymysql://u:p@127.0.0.1:1/x", "connect_timeout", 10),
    ],
)
def test_each_driver_gets_a_connect_timeout(url, name, value):
    assert _cparams(url)[name] == value


@pytest.mark.skipif(
    importlib.util.find_spec("psycopg") is None, reason="psycopg が無い"
)
def test_psycopg_gets_an_integer_connect_timeout():
    params = _cparams("postgresql+psycopg://u:p@127.0.0.1:1/x", connect_timeout=2.2)
    assert params["connect_timeout"] == 3


def test_a_timeout_in_the_url_is_kept():
    params = _cparams("mysql+pymysql://u:p@127.0.0.1:1/x?connect_timeout=4")
    assert params["connect_timeout"] == 4


def test_no_timeout_for_sqlite_or_when_disabled(tmp_path):
    assert Database(f"sqlite:///{tmp_path / 'a.db'}").connect_timeout is None
    params = _cparams("postgresql+pg8000://u:p@127.0.0.1:1/x", connect_timeout=None)
    assert "timeout" not in params


def test_waiting_for_the_database_caps_each_attempt_by_the_time_left():
    setter = engine_mod._connect_timeout_setter("timeout", False, 10.0)
    params: dict = {}
    engine_mod._wait_deadline.value = time.monotonic() + 2.5
    try:
        setter(None, None, (), params)
    finally:
        engine_mod._wait_deadline.value = None
    assert 1.0 <= params["timeout"] <= 2.5
    params = {}
    setter(None, None, (), params)  # 起動待ちの外では既定のまま
    assert params["timeout"] == 10.0


@pytest.fixture
def silent_server():
    """接続は受け付けるが何も返さないサーバー(応答の無い DB の代わり)。"""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(16)
    held: list[socket.socket] = []
    stop = threading.Event()

    def accept() -> None:
        server.settimeout(0.1)
        while not stop.is_set():
            try:
                conn, _ = server.accept()
            except OSError:
                continue
            held.append(conn)

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    yield server.getsockname()[1]
    stop.set()
    thread.join(5)
    for conn in held:
        conn.close()
    server.close()


def _drivers() -> list[str]:
    drivers = ["pg8000"]
    if importlib.util.find_spec("psycopg") is not None:
        drivers.append("psycopg")
    return drivers


@pytest.mark.parametrize("driver", _drivers())
def test_waiting_for_an_unresponsive_database_ends_within_the_wait(
    silent_server, driver
):
    db = Database(f"postgresql+{driver}://u:p@127.0.0.1:{silent_server}/x")
    outcome: list = []

    def wait() -> None:
        began = time.monotonic()
        try:
            db.wait_until_ready(2.0, initial_delay=0.2)
        except DBAPIError as exc:
            outcome.append((time.monotonic() - began, exc))

    worker = threading.Thread(target=wait, daemon=True)
    worker.start()
    worker.join(30)
    db.dispose()
    assert not worker.is_alive(), "接続の待ちに上限がありません"
    took, _exc = outcome[0]
    assert took < 6.0  # 待つのは MONITOR_DB_WAIT_TIMEOUT(ここでは 2 秒)+ α まで


def test_pg8000_queries_are_not_limited_by_the_connect_timeout():
    # pg8000 は timeout をソケットの読み書きすべてに使うため、接続後に外す
    calls: list = []

    class _Sock:
        def settimeout(self, value) -> None:
            calls.append(value)

    class _Conn:
        _usock = _Sock()

    db = Database("postgresql+pg8000://u:p@127.0.0.1:1/x")
    try:
        assert event.contains(db.engine, "connect", engine_mod._pg8000_no_read_timeout)
    finally:
        db.dispose()
    engine_mod._pg8000_no_read_timeout(_Conn(), None)
    engine_mod._pg8000_no_read_timeout(object(), None)  # 内部が違う版でも失敗しない
    assert calls == [None]
