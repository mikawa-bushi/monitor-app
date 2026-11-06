"""CLI の停止のシグナル(docker stop・systemctl stop・端末を閉じる)の扱い。

:func:`stop_signals` のブロックの間、SIGTERM(と既定の動作のままの SIGHUP)を
:class:`StopRequested` にする。
:mod:`monitor_app.cli._common` からも同じ名前で import できる。
"""

from __future__ import annotations

import signal
import threading
from collections.abc import Iterator
from contextlib import contextmanager

__all__ = ["StopRequested", "stop_signals"]


class StopRequested(BaseException):
    """停止のシグナル(SIGTERM・端末を閉じたときの SIGHUP)を受けた。

    KeyboardInterrupt と同じく BaseException から派生させ、取り込みなどの
    ``except Exception``(1 ファイルの失敗で全体を止めない、など)に握りつぶされない
    ようにする。
    """


@contextmanager
def stop_signals() -> Iterator[None]:
    """このブロックの間、SIGTERM を :class:`StopRequested` にして処理を打ち切れるようにする。

    - コンテナでは monitor-app が PID 1 になり、ハンドラの無い SIGTERM はカーネルに
      無視される。uvicorn がハンドラを登録する前(大きな CSV の取り込み中など)に
      ``docker stop`` すると、猶予時間が切れて強制終了(SIGKILL)されるまで止まらない。
      例外で打ち切れば、取り込み中のトランザクションは戻り、finally の後片付けも走る。
    - 端末を閉じたときの SIGHUP(POSIX)も、既定の動作(その場で終了し、後片付けが
      走らない)のままなら SIGTERM に置き換える。uvicorn の動作中は uvicorn の
      ハンドラが受けて正常に停止する(nohup などで SIGHUP を無視していれば変えない)。
    - 抜けるときに元のハンドラへ戻す。uvicorn は停止の処理のあと、受けたシグナルを
      その時点のハンドラへ送り直す。メインスレッド以外では何もしない(シグナルの
      ハンドラはメインスレッドでしか設定できない)。
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def raise_stop(signum: int, frame: object) -> None:
        raise StopRequested

    def hangup_as_sigterm(signum: int, frame: object) -> None:
        signal.raise_signal(signal.SIGTERM)

    previous = signal.signal(signal.SIGTERM, raise_stop)
    sighup = getattr(signal, "SIGHUP", None)
    previous_hup = None
    if sighup is not None and signal.getsignal(sighup) is signal.SIG_DFL:
        previous_hup = signal.signal(sighup, hangup_as_sigterm)
    try:
        yield
    finally:
        signal.signal(
            signal.SIGTERM, previous if previous is not None else signal.SIG_DFL
        )
        if previous_hup is not None:
            signal.signal(sighup, previous_hup)
