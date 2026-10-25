"""閾値アラートの評価とエッジ検出(フェーズ1・A)。

各ビューのデータに対しルールを評価し、違反を検出する。発火中アラートの集合を
保持し、新規発火(OFF→ON)と復帰(ON→OFF)のときだけ通知することで、
連続通知を抑制する。

評価の入力はビューの全データだけ(ページ取得の一部の行で評価すると、ページごとに
発火と復帰を繰り返して通知が乱発されるため)。:meth:`AlertEngine.evaluate_view_if_changed`
はデータのハッシュで評価済みかを覚え、同じデータを何度渡されても評価し直さない
(AlertScheduler・GET・一括取得・SSE が共有する)。

並行性(#12): ``_active`` は AlertScheduler(単一ループ)と複数リクエストスレッド
から触られるため、``threading.Lock`` で保護する。発火・復帰の通知もそのロック下で
キューに積み、状態が変わった順に届ける。
通知(#14): 外部送信(webhook/SMTP)はブロッキングなため、同期パスからは
キューに積むだけにし、ワーカースレッドで送る。失敗したチャネルは間隔を倍々に
伸ばして再試行し(``notify_retries``)、終了時は :meth:`AlertEngine.close` で
キューに残った通知を送り切る。
"""

from __future__ import annotations

import contextlib
import logging
import operator
import queue
import threading
from collections.abc import Callable, Hashable
from dataclasses import dataclass, field

from ..settings.declarative import AlertRule, MonitorConfig
from ..settings.runtime import AppSettings
from .notifiers import build_notifiers, channel_problem, dispatch

logger = logging.getLogger("monitor_app.alerts")

_OPS: dict[str, Callable[[float, float], bool]] = {
    ">": operator.gt,
    ">=": operator.ge,
    "<": operator.lt,
    "<=": operator.le,
    "==": operator.eq,
    "!=": operator.ne,
}

# 通知キューの 1 件: (channels, subject, body, level)。None は停止サインティネル。
_NotifyItem = tuple[list[str], str, str, str] | None

#: 終了時に未送信の通知を送り切るまで待つ既定の秒数。
DEFAULT_CLOSE_TIMEOUT = 5.0


@dataclass
class ActiveAlert:
    key: str
    view: str
    column: str
    level: str
    message: str
    count: int  # 違反している行数


@dataclass
class AlertEngine:
    config: MonitorConfig
    settings: AppSettings
    _active: dict[str, ActiveAlert] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._notifiers = build_notifiers(self.settings)
        # ルールを対象ビューごとに索引化。
        self._by_view: dict[str, list[AlertRule]] = {}
        for rule in self.config.alerts:
            self._by_view.setdefault(rule.view, []).append(rule)
        # _active と評価済みハッシュを保護するロック(#12)。
        self._lock = threading.Lock()
        #: ビュー名 → (評価したデータのハッシュ, スナップショットの通し番号)。
        self._evaluated: dict[str, tuple[Hashable, int | None]] = {}
        # 非同期通知のキューとワーカー(#14)。ワーカーは初回通知時に遅延起動する。
        self._notify_queue: queue.Queue[_NotifyItem] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._worker_lock = threading.Lock()
        self._closing = threading.Event()
        # webhook/SMTP などブロッキング I/O を伴う通知先がある場合のみ
        # 非同期ワーカーを使う。console だけならログ出力のみで即時送る(#14)。
        self._use_async_notify = any(name != "console" for name in self._notifiers)
        self._warn_unusable_channels()

    # ------------------------------------------------------------------
    # 起動時の検証
    # ------------------------------------------------------------------
    def _warn_unusable_channels(self) -> None:
        """未知のチャネルや接続情報が未設定のチャネルを 1 つにつき 1 回だけ警告する。

        どちらも従来どおり console へ出力される(通知が届かないことに気付けるように)。
        """
        warned: set[str] = set()
        for rule in self.config.alerts:
            for name in rule.notify:
                if name in warned:
                    continue
                warned.add(name)
                problem = channel_problem(name, self.settings, self._notifiers)
                if problem:
                    logger.warning(
                        "アラート(ビュー '%s')の通知先 '%s': %s",
                        rule.view,
                        name,
                        problem,
                    )

    # ------------------------------------------------------------------
    # 通知(非ブロッキング)
    # ------------------------------------------------------------------
    def _ensure_worker(self) -> None:
        with self._worker_lock:
            self._ensure_worker_locked()

    def _ensure_worker_locked(self) -> None:
        worker = self._worker
        if worker is not None and worker.is_alive():
            return
        t = threading.Thread(
            target=self._drain_notifications,
            args=(self._notify_queue, self._closing),
            name="alert-notifier",
            daemon=True,
        )
        t.start()
        self._worker = t

    def _drain_notifications(
        self,
        items: queue.Queue[_NotifyItem] | None = None,
        closing: threading.Event | None = None,
    ) -> None:
        items = items if items is not None else self._notify_queue
        closing = closing if closing is not None else self._closing
        while True:
            item = items.get()
            if item is None:  # 停止サインティネル
                return
            channels, subject, body, level = item
            retries = getattr(self.settings, "notify_retries", 0)
            try:
                dispatch(
                    self._notifiers,
                    channels,
                    subject=subject,
                    body=body,
                    level=level,
                    retries=0 if closing.is_set() else retries,
                    wait=closing.wait,
                )
            except Exception:  # noqa: BLE001 - 通知失敗・終了時の logging 失敗で
                # ワーカースレッドを死なせない(ベストエフォートで記録)。
                with contextlib.suppress(Exception):
                    logger.warning("通知ディスパッチに失敗しました", exc_info=True)

    def _enqueue(
        self, channels: list[str], subject: str, body: str, level: str
    ) -> None:
        if not self._use_async_notify:
            # console のみ(ブロッキング I/O 無し)は同期送信で十分。
            dispatch(self._notifiers, channels, subject=subject, body=body, level=level)
            return
        # close() のキュー差し替えと競合しないよう、起動と投入をまとめてロック下で行う
        # (停止サインティネルより後ろに積まれて取り残されることがない)。
        with self._worker_lock:
            self._ensure_worker_locked()
            self._notify_queue.put((list(channels), subject, body, level))

    def close(self, timeout: float = DEFAULT_CLOSE_TIMEOUT) -> bool:
        """キューに残った通知を送り切ってからワーカーを止める(終了時に呼ぶ)。

        送信に失敗しているチャネルは再試行の待ちを打ち切り、もう 1 回だけ試す。
        ``timeout`` 秒以内に終われば True。終わらなければ警告を残して諦める
        (ワーカーはデーモンスレッドなのでプロセスの終了は妨げない)。呼んだ後も
        エンジンは使える(次の通知で新しいワーカーが起動する)。
        """
        with self._worker_lock:
            worker, items, closing = self._worker, self._notify_queue, self._closing
            self._worker = None
            self._notify_queue = queue.Queue()
            self._closing = threading.Event()
            if worker is not None and worker.is_alive():
                closing.set()
                items.put(None)  # これより前に積まれた通知を送ってから止まる
        if worker is None or not worker.is_alive():
            if not items.empty():  # ワーカーが止まった後に積まれた分
                self._send_now(items)
            return True
        worker.join(timeout)
        if worker.is_alive():
            logger.warning(
                "未送信のアラート通知を残したまま終了します(%g 秒以内に送り切れません"
                "でした)",
                timeout,
            )
            return False
        return True

    def _send_now(self, items: queue.Queue[_NotifyItem]) -> None:
        """停止済みのキューに残った通知を、この場で 1 回ずつ送る。"""
        while True:
            try:
                item = items.get_nowait()
            except queue.Empty:
                return
            if item is None:
                continue
            channels, subject, body, level = item
            dispatch(self._notifiers, channels, subject=subject, body=body, level=level)

    # ------------------------------------------------------------------
    # 評価
    # ------------------------------------------------------------------
    @staticmethod
    def _rule_key(rule: AlertRule) -> str:
        return f"{rule.view}:{rule.column}:{rule.op}:{rule.value}"

    def _evaluate_rule(self, rule: AlertRule, rows: list[dict]) -> int:
        """ルールに違反する行数を返す。"""
        compare = _OPS[rule.op]
        violations = 0
        for row in rows:
            raw = row.get(rule.column)
            if raw is None:
                continue
            try:
                if compare(float(raw), rule.value):
                    violations += 1
            except (TypeError, ValueError):
                continue
        return violations

    def evaluate_view(self, view_name: str, rows: list[dict]) -> list[ActiveAlert]:
        """1 つのビューのデータを評価し、発火/復帰時に通知する。

        ``rows`` はビューの全データであること(一部の行で評価すると誤って復帰する)。
        返り値はこのビューに紐づく現在アクティブなアラート。``_active`` の更新と
        通知の投入(キューに積むだけ・非ブロッキング)は同じロック下で行う(2 つの
        スレッドが続けて評価しても、通知が状態の変化と逆の順に届かないように)。
        """
        with self._lock:
            # 直接渡されたデータで状態が変わるため、評価済みの記録は無効にする。
            self._evaluated.pop(view_name, None)
            results, edges = self._evaluate_locked(view_name, rows)
            self._send_edges(edges)
        return results

    def evaluate_view_if_changed(
        self,
        view_name: str,
        digest: Hashable,
        rows: list[dict],
        *,
        version: int | None = None,
    ) -> bool:
        """データのハッシュ ``digest`` が前回の評価と違うときだけ評価する。

        同じデータを何度評価しても状態は変わらないため、ポーリング・SSE の
        クライアント数やスケジューラの周期に関係なく、評価はデータが変わったときの
        1 回で済む。``version``(スナップショットの通し番号)を渡すと、すでに評価した
        ものより古いデータ(遅れて届いた別スレッドの結果)では評価し直さない。
        評価したら True。
        """
        with self._lock:
            last = self._evaluated.get(view_name)
            if last is not None:
                last_digest, last_version = last
                if last_digest == digest:
                    return False
                if (
                    version is not None
                    and last_version is not None
                    and version < last_version
                ):
                    return False
            _results, edges = self._evaluate_locked(view_name, rows)
            self._evaluated[view_name] = (digest, version)
            # 状態の変化と同じ順に届くよう、ロック下で投入する(evaluate_view と同じ)。
            self._send_edges(edges)
        return True

    def _evaluate_locked(
        self, view_name: str, rows: list[dict]
    ) -> tuple[list[ActiveAlert], list[tuple[str, AlertRule, ActiveAlert | None]]]:
        results: list[ActiveAlert] = []
        edges: list[tuple[str, AlertRule, ActiveAlert | None]] = []
        for rule in self._by_view.get(view_name, []):
            key = self._rule_key(rule)
            count = self._evaluate_rule(rule, rows)
            was_active = key in self._active
            if count > 0:
                alert = ActiveAlert(
                    key=key,
                    view=rule.view,
                    column=rule.column,
                    level=rule.level,
                    message=rule.describe(),
                    count=count,
                )
                self._active[key] = alert
                results.append(alert)
                if not was_active:  # OFF → ON のエッジでのみ通知
                    edges.append(("fire", rule, alert))
            elif was_active:  # ON → OFF(復帰)
                del self._active[key]
                edges.append(("recover", rule, None))
        return results, edges

    def _send_edges(
        self, edges: list[tuple[str, AlertRule, ActiveAlert | None]]
    ) -> None:
        for kind, rule, alert in edges:
            if kind == "fire" and alert is not None:
                self._notify(rule, alert)
            else:
                self._notify_recovery(rule)

    def _notify(self, rule: AlertRule, alert: ActiveAlert) -> None:
        self._enqueue(
            rule.notify,
            f"異常検知: {rule.view}",
            f"{alert.message}({alert.count} 件)",
            rule.level,
        )

    def _notify_recovery(self, rule: AlertRule) -> None:
        self._enqueue(
            rule.notify,
            f"復帰: {rule.view}",
            f"{rule.describe()} は解消しました",
            "info",
        )

    def alert_views(self) -> list[str]:
        """アラートルールを持つビュー名の一覧(AlertScheduler 用)。"""
        return list(self._by_view.keys())

    def active_alerts(self) -> list[dict]:
        """現在アクティブな全アラートを辞書のリストで返す。"""
        with self._lock:
            return [
                {
                    "view": a.view,
                    "column": a.column,
                    "level": a.level,
                    "message": a.message,
                    "count": a.count,
                }
                for a in self._active.values()
            ]
