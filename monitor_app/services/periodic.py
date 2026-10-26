"""lifespan で起動/停止する定期バックグラウンドタスクの共通基盤。

フォルダ監視(:class:`~monitor_app.services.ingest_watcher.IngestWatcher`)と
アラート評価(:class:`~monitor_app.services.alert_scheduler.AlertScheduler`)が
共有する asyncio のパターンをまとめる。

- ``setup()`` … 開始時に 1 回だけスレッドで実行する(既定は何もしない)。テーブル
  作成や大きな CSV の行数カウントのようなブロッキング処理でイベントループを止めない。
- ``tick()`` … ``interval`` 秒ごとにスレッドで実行する。例外はログに残してループを
  継続する(1 回の失敗でタスクを恒久停止させない)。
- ``start()`` / ``stop()`` … どちらも冪等。``stop()`` は停止の指示(``stopping``)を
  出してタスクをキャンセルし、実行中の setup / tick の終了を最大 ``stop_timeout`` 秒
  待つ。実行中の処理は次の SQL で打ち切られ、書きかけのトランザクションは取り消される
  (:mod:`monitor_app.cancel`)。SQL を使わない長いループは
  :func:`~monitor_app.cancel.stop_requested` で指示を確かめられる。
- setup / tick はタスクごとのデーモンスレッドで動かす(asyncio の既定のスレッドプール
  は使わない)。待ちきれなかった処理があっても、プロセスの終了(コンテナで PID 1 の
  ときの ``docker stop`` など)を引き延ばさない。
- ``last_tick`` / ``last_error`` … 直近の実行結果(``/api/health`` が表示する)。
  1 ファイル・1 ビューなどの失敗を tick の中で個別に処理するタスクは
  :meth:`PeriodicTask.note_error` で記録する(tick は続け、失敗として表示する)。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime

from ..cancel import TaskStopped, stop_requested, stop_scope

__all__ = ["STOP_TIMEOUT", "PeriodicTask", "TaskStopped", "stop_requested"]

#: 停止のとき、実行中の setup / tick の終了を待つ既定の最大秒数。uvicorn の停止の猶予
#: (5 秒)と合わせても、``docker stop`` の既定の猶予(10 秒)に収まるようにする。
STOP_TIMEOUT = 3.0
#: 実行中の処理の終了を確かめる間隔(秒)。
_JOIN_POLL = 0.02


class PeriodicTask:
    """``tick()`` を ``interval`` 秒ごとにスレッドで実行するバックグラウンドタスク。"""

    #: ログに表示するタスク名(サブクラスで上書きする)。
    name = "periodic task"
    #: tick 失敗時のログに使う処理名("<name> <tick_label> failed; continuing")。
    tick_label = "tick"
    #: 開始・停止・失敗を記録するロガー(サブクラスは自分のロガーに差し替える)。
    _log = logging.getLogger("monitor_app.periodic")
    #: 最後に tick が成功して終わった時刻(UTC)。まだ一度も成功していなければ None。
    last_tick: datetime | None = None
    #: 直近の setup / tick が失敗したときの例外の型名。次の tick が成功すると None。
    #: メッセージやトレースバックはログにだけ残す(認証なしで読めるヘルスチェックの
    #: 応答に、ファイルパスや接続先などの内部情報を出さないため)。
    last_error: str | None = None
    #: 実行中の tick で :meth:`note_error` が記録した失敗(tick の終わりに反映する)。
    _noted_error: str | None = None
    #: 停止のとき、実行中の setup / tick の終了を待つ最大秒数。
    stop_timeout: float = STOP_TIMEOUT

    def __init__(self, interval: float) -> None:
        self.interval = interval
        self._task: asyncio.Task | None = None
        self.last_tick = None
        self.last_error = None
        self._noted_error = None
        #: 停止の指示。:meth:`stop` が立て、:meth:`start` のたびに新しくする(待ちきれずに
        #: 残った前回の処理には、前回の指示が立ったまま届く)。
        self.stopping = threading.Event()
        #: setup / tick を実行中(または最後に実行した)スレッド。
        self._thread: threading.Thread | None = None

    def setup(self) -> None:
        """ループ開始前に 1 回だけスレッドで呼ばれる(既定は何もしない)。"""

    def tick(self) -> None:
        """``interval`` 秒ごとにスレッドで呼ばれる 1 回分の処理。"""
        raise NotImplementedError

    def note_error(self, exc: BaseException) -> None:
        """tick(または setup)の中で個別に処理した失敗を記録する。

        1 ファイル・1 ビューの失敗でほかを止めないタスク用。処理は続けるが、その回は
        成功として数えず、``last_error`` に例外の型名を残す(``/api/health`` に出る)。
        """
        self._noted_error = type(exc).__name__

    def record_result(self, exc: BaseException | None = None) -> None:
        """1 回分の実行結果を ``last_tick`` / ``last_error`` に記録する。

        ``exc`` も :meth:`note_error` の記録も無ければ成功(時刻を記録し、失敗の記録を
        消す)。setup の中で tick と同じ処理を行うタスクも、これで結果を残す。
        """
        noted, self._noted_error = self._noted_error, None
        if exc is not None:
            self.last_error = type(exc).__name__
        elif noted is not None:
            self.last_error = noted
        else:
            self.last_tick = datetime.now(UTC)
            self.last_error = None

    def _log_started(self) -> None:
        self._log.info("%s started (interval=%.1fs)", self.name, self.interval)

    async def _call(self, fn: Callable[[], None]) -> None:
        """``fn`` をデーモンスレッドで実行し、終わるまで待つ。

        スレッドには停止の指示を結び付ける(:func:`~monitor_app.cancel.stop_scope`)。
        停止の指示で打ち切られた(:class:`TaskStopped`)なら、キャンセルとして扱う。
        """
        loop = asyncio.get_running_loop()
        done: asyncio.Future[None] = loop.create_future()
        stopping = self.stopping

        def deliver(exc: BaseException | None) -> None:
            if done.done():
                return  # 停止(キャンセル)の後に終わった
            if isinstance(exc, TaskStopped):
                done.cancel()
            elif exc is not None:
                done.set_exception(exc)
            else:
                done.set_result(None)

        def run() -> None:
            exc: BaseException | None = None
            try:
                with stop_scope(stopping):
                    fn()
            except TaskStopped as stopped:
                exc = stopped
                self._log.info("%s: 停止の指示を受けて処理を打ち切りました", self.name)
            except BaseException as error:  # noqa: BLE001 - ループ側へ渡す
                exc = error
            # イベントループが既に終わっていれば(待ちきれずに残った処理)渡さない。
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(deliver, exc)

        thread = threading.Thread(
            target=run, name=f"monitor-app {self.name}", daemon=True
        )
        self._thread = thread
        thread.start()
        await done

    async def _run(self) -> None:
        try:
            try:
                await self._call(self.setup)
            except Exception as exc:  # noqa: BLE001
                # 初期化の失敗はログに残して終了する。
                self.last_error = type(exc).__name__
                self._log.exception("%s setup failed; not started", self.name)
                return
            self._log_started()
            while True:
                await asyncio.sleep(self.interval)
                self._noted_error = None
                try:
                    await self._call(self.tick)
                except Exception as exc:  # noqa: BLE001 - ループを恒久停止させない
                    self.record_result(exc)
                    self._log.exception(
                        "%s %s failed; continuing", self.name, self.tick_label
                    )
                else:
                    self.record_result()
        except asyncio.CancelledError:
            self._log.info("%s stopped", self.name)
            raise

    @property
    def running(self) -> bool:
        """タスクが起動済みで、まだ終了していないか。"""
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """タスクを起動する(実行中なら何もしない)。イベントループ上で呼ぶ。"""
        if self.running:
            return
        self.stopping = threading.Event()
        self._task = asyncio.create_task(self._run(), name=self.name)

    async def stop(self, timeout: float | None = None) -> bool:
        """停止を指示してタスクをキャンセルし、実行中の setup / tick の終了を待つ。

        待つのは最大 ``timeout`` 秒(既定は :attr:`stop_timeout`)。実行中の処理は次の
        SQL で打ち切られる。時間内に終わらなければ警告を残して待たずに戻る(処理は
        デーモンスレッドなので、プロセスの終了は妨げない)。未起動・停止済みなら何も
        しない。時間内に止まれば(何もしなかったときも)True。
        """
        task, self._task = self._task, None
        if task is None:
            return True
        self.stopping.set()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        return await self._join(self.stop_timeout if timeout is None else timeout)

    async def _join(self, timeout: float) -> bool:
        """実行中の setup / tick のスレッドの終了を最大 ``timeout`` 秒待つ。"""
        thread = self._thread
        if thread is None or not thread.is_alive():
            return True
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(timeout, 0.0)
        while thread.is_alive():
            if loop.time() >= deadline:
                self._log.warning(
                    "%s: 実行中の処理が %g 秒以内に終わらないため、待たずに停止します",
                    self.name,
                    timeout,
                )
                return False
            await asyncio.sleep(_JOIN_POLL)
        return True
