"""netdiag watch アラートログ可視化プリセット。

``netdiag watch`` が ``~/.netdiag/alerts.jsonl`` に追記する 1 行 1 JSON 形式の
アラートログを monitor-app に取り込むための設定フラグメント生成器。
"""

from __future__ import annotations

from ..settings.declarative import (
    AlertRule,
    CellStyle,
    KpiCard,
    MonitorConfig,
    SourceDef,
    TableDef,
    Threshold,
    ViewDef,
)
from . import merge


def attach(
    config: MonitorConfig,
    *,
    alerts_jsonl: str = "~/.netdiag/alerts.jsonl",
    prefix: str = "netdiag",
    with_alerts: bool = True,
) -> MonitorConfig:
    """netdiag watch のアラートログ可視化一式を合成する。"""

    table_name = f"{prefix}_alerts"
    timeline_view = f"{prefix}_timeline"
    state_view = f"{prefix}_state"

    # --- テーブル定義 ---
    # id は自動採番主キー。JSONL に存在しないので mapping は空でよい。
    tables = {
        table_name: TableDef(
            columns={
                "id": "int",
                "ts": "float",
                "iso": "str",
                "event": "str",
                "from_state": "str",
                "to_state": "str",
                "cause": "str",
                "confidence": "str",
                "summary": "str",
                "evidence": "str",
                "consequences": "str",
                "runbook": "str",
                "duration_s": "float",
            },
            primary_key="id",
        )
    }

    # --- ソース定義 ---
    sources = {
        table_name: SourceDef(
            kind="jsonl",
            path=alerts_jsonl,
            table=table_name,
            mode="append",
        )
    }

    # --- CASE 式(共通) ---
    _is_degraded = "CASE WHEN event = 'degraded' THEN 1 ELSE 0 END AS is_degraded"

    # 2 ビューで列構成が同じなので見出しを共用する
    _labels = {
        "iso": "時刻",
        "event": "イベント",
        "cause": "原因",
        "summary": "概要",
        "duration_s": "継続時間 (秒)",
        "is_degraded": "障害中",
    }

    # --- ビュー定義 ---
    views = {
        # 直近 100 件を新しい順に表示
        timeline_view: ViewDef(
            query=(
                f"SELECT iso, event, cause, summary, duration_s, {_is_degraded} "
                f"FROM {table_name} "
                f"ORDER BY ts DESC "
                f"LIMIT 100"
            ),
            title=f"{prefix} タイムライン",
            labels=_labels,
            styles={
                "is_degraded": CellStyle(
                    greater_than=Threshold(value=0, css_class="text-danger fw-bold"),
                )
            },
            group="ネットワーク診断",
        ),
        # 最新 1 行から現在状態を返す
        state_view: ViewDef(
            query=(
                f"SELECT iso, event, cause, summary, duration_s, {_is_degraded} "
                f"FROM {table_name} "
                f"ORDER BY ts DESC "
                f"LIMIT 1"
            ),
            title=f"{prefix} 現在状態",
            labels=_labels,
            group="ネットワーク診断",
        ),
    }

    # --- KPI 定義 ---
    kpis = {
        f"{prefix}_degraded_24h": KpiCard(
            title="直近 24h 障害回数",
            query=(
                f"SELECT COUNT(*) FROM {table_name} "
                f"WHERE event = 'degraded' "
                f"AND ts > strftime('%s', 'now') - 86400"
            ),
            unit="回",
            higher_is_better=False,
            link_view=timeline_view,
        ),
        f"{prefix}_avg_recovery_min": KpiCard(
            title="平均復旧時間",
            query=(
                f"SELECT AVG(duration_s) / 60.0 FROM {table_name} "
                f"WHERE event = 'recovered'"
            ),
            unit="分",
            higher_is_better=False,
            format="{:.1f}",
            link_view=timeline_view,
        ),
    }

    # --- アラートルール ---
    alert_rules: list[AlertRule] = []
    if with_alerts:
        alert_rules.append(
            AlertRule(
                view=state_view,
                column="is_degraded",
                op="==",
                value=1,
                level="critical",
                message=f"{prefix}: ネットワーク障害を検出しました",
            )
        )

    return merge(
        config,
        tables=tables,
        views=views,
        kpis=kpis,
        alerts=alert_rules,
        sources=sources,
    )
