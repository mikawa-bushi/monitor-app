"""バックグラウンド処理の協調的な打ち切り(停止の指示を、実行中のスレッドへ伝える)。

lifespan の終了時(``docker stop``・``systemctl stop``・Ctrl+C)に、フォルダ監視の
取り込み・保持期間の削除・アラート評価などの実行中の処理を、待たずに・壊さずに
切り上げるための小さな仕組み。

- :class:`~monitor_app.services.periodic.PeriodicTask` は setup / tick を実行する
  スレッドに停止の指示(:class:`threading.Event`)を :func:`stop_scope` で結び付ける。
- そのスレッドで動く処理は :func:`stop_requested` で指示を確かめられる(SQL を使わない
  長いループ — バックアップのコピーなど — 向け)。
- ファイルの SQLite・サーバー DB のエンジンは、SQL を実行する直前に
  :func:`raise_if_stop_requested` を呼ぶ(:mod:`monitor_app.db.engine`)。停止の指示が
  出ていれば :class:`TaskStopped` で抜け、書きかけのトランザクションは取り消される。
  個々の処理に確認を書き足さなくても、次の SQL で打ち切られる。

リクエストを処理するスレッドなど、:func:`stop_scope` の外では何も起きない。
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

_local = threading.local()


class TaskStopped(BaseException):
    """停止の指示を受けたバックグラウンド処理を打ち切る。

    KeyboardInterrupt と同じく BaseException から派生させ、1 ファイル・1 ビューの失敗を
    個別に扱う ``except Exception`` に握りつぶされずに、setup / tick の外まで抜ける。
    """


@contextmanager
def stop_scope(event: threading.Event) -> Iterator[None]:
    """このブロックの間、今のスレッドの処理に停止の指示 ``event`` を結び付ける。"""
    previous = getattr(_local, "event", None)
    _local.event = event
    try:
        yield
    finally:
        _local.event = previous


def stop_requested() -> bool:
    """今のスレッドの処理に停止の指示が出ているか(:func:`stop_scope` の外では False)。"""
    event = getattr(_local, "event", None)
    return event is not None and event.is_set()


def raise_if_stop_requested() -> None:
    """停止の指示が出ていれば :class:`TaskStopped` を送出する。"""
    if stop_requested():
        raise TaskStopped
