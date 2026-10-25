"""アラートの通知が、状態の変化(発火 → 復帰)と同じ順に届くこと。

スケジューラと SSE のように 2 つのスレッドが同じビューを続けて評価したとき、先に
評価したスレッドの通知の投入が遅れても、後の評価の通知が追い越さない。
"""

from __future__ import annotations

import threading
import time

from monitor_app import AlertRule, MonitorConfig, TableDef, ViewDef
from monitor_app.services.alert_service import AlertEngine
from monitor_app.settings.runtime import AppSettings


def _engine() -> AlertEngine:
    config = MonitorConfig(
        tables={"t": TableDef(columns={"id": "int", "v": "float"}, primary_key="id")},
        views={"v": ViewDef(query="SELECT * FROM t")},
        alerts=[AlertRule(view="v", column="v", op=">", value=10, notify=["console"])],
    )
    return AlertEngine(config, AppSettings())


def _run_two_evaluations(engine: AlertEngine, evaluate_first, evaluate_second) -> list:
    sent: list[str] = []
    engine._enqueue = lambda channels, subject, body, level: sent.append(subject)
    original = engine._send_edges

    def slow_send(edges):
        if threading.current_thread().name == "first":
            time.sleep(0.2)  # 先に評価したスレッドが投入の直前で待たされる
        original(edges)

    engine._send_edges = slow_send
    first = threading.Thread(name="first", target=evaluate_first)
    first.start()
    time.sleep(0.05)
    second = threading.Thread(name="second", target=evaluate_second)
    second.start()
    first.join(5)
    second.join(5)
    return sent


def test_if_changed_evaluations_notify_in_state_order():
    engine = _engine()
    sent = _run_two_evaluations(
        engine,
        lambda: engine.evaluate_view_if_changed("v", "d1", [{"v": 99}], version=1),
        lambda: engine.evaluate_view_if_changed("v", "d2", [{"v": 1}], version=2),
    )
    assert engine.active_alerts() == []
    assert sent == ["異常検知: v", "復帰: v"]  # 最後の通知が今の状態(復帰)と一致


def test_direct_evaluations_notify_in_state_order():
    engine = _engine()
    sent = _run_two_evaluations(
        engine,
        lambda: engine.evaluate_view("v", [{"v": 99}]),
        lambda: engine.evaluate_view("v", [{"v": 1}]),
    )
    assert sent == ["異常検知: v", "復帰: v"]
