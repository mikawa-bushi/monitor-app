"""計算の失敗を相乗りした全員へ渡しても、例外のトレースバックが積み重ならないこと。

失敗した計算の例外は相乗りした呼び出しで同じオブジェクトを送出し直す。そのたびに
各スレッドのフレームが同じ ``__traceback__`` に足されると、N 件のクライアントの
ログがどんどん長く、ほかのリクエストのフレームが混ざったものになる。
"""

from __future__ import annotations

import threading
import traceback

from monitor_app.exceptions import QueryExecutionError
from monitor_app.services.view_cache import ViewCache


def test_followers_raise_the_error_with_the_leaders_traceback_only():
    cache = ViewCache(1000)
    started, release = threading.Event(), threading.Event()

    def failing():
        started.set()
        release.wait(5)
        raise QueryExecutionError("ビュー 'v' のクエリ実行に失敗しました")

    errors: list[BaseException] = []
    depths: list[int] = []
    lock = threading.Lock()

    def call() -> None:
        try:
            cache.get_or_compute("k", failing)
        except QueryExecutionError as exc:
            with lock:
                errors.append(exc)
                depths.append(len(traceback.extract_tb(exc.__traceback__)))

    leader = threading.Thread(target=call)
    leader.start()
    assert started.wait(5)
    followers = [threading.Thread(target=call) for _ in range(20)]
    for thread in followers:
        thread.start()
    deadline = threading.Event()
    while cache.stats["waits"] < 20 and not deadline.wait(0.005):
        pass
    release.set()
    for thread in [leader, *followers]:
        thread.join(5)
    assert len(errors) == 21
    assert len({id(e) for e in errors}) == 1  # 同じ例外オブジェクト(型・内容はそのまま)
    # 送出し直しのたびに伸びず、リーダーのフレーム + 自分のフレームの数で一定。
    assert max(depths) - min(depths) <= 2
    assert max(depths) < 15
