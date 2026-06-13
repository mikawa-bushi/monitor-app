"""network-checker の ping 結果可視化プリセット。

network-checker は SQLite(既定 ~/network_check_tool.db)の ping_results テーブルに
蓄積するツール。このモジュールは MonitorConfig にビュー・KPI・アラートを合成する。
"""

from __future__ import annotations

import re

from ..settings.declarative import (
    AlertRule,
    CellStyle,
    ChartDef,
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
    db_path: str = "~/network_check_tool.db",
    hosts: list[str] | None = None,  # ホスト別ビューを作る対象。None なら集約ビューのみ
    prefix: str = "netcheck",
    with_alerts: bool = True,
    loss_threshold_pct: float = 5.0,  # 直近 1h のロス率アラート閾値(%)
) -> MonitorConfig:
    """network-checker の ping 結果可視化一式を合成する。"""

    ping_table = f"{prefix}_ping"
    summary_view = f"{prefix}_summary"

    # --- テーブル定義 ---
    tables = {
        ping_table: TableDef(
            columns={
                "id": "int",
                "target_host": "str",
                "timestamp": "str",
                "response_time": "float",
                "packet_loss": "bool",
                "error_message": "str",
            },
            primary_key="id",
        )
    }

    # --- ソース定義 ---
    sources = {
        ping_table: SourceDef(
            kind="sqlite",
            path=db_path,
            table=ping_table,
            source_table="ping_results",
            watermark_column="id",
        )
    }

    # --- サマリービュー(ホスト別集計、直近 24h) ---
    summary_query = f"""
SELECT
    target_host,
    COUNT(*) AS total_count,
    AVG(CASE WHEN packet_loss THEN 0.0 ELSE 100.0 END) AS success_pct,
    AVG(response_time) AS avg_ms,
    (
        SELECT AVG(CASE WHEN i.packet_loss THEN 100.0 ELSE 0.0 END)
        FROM {ping_table} i
        WHERE i.target_host = o.target_host
          AND i.timestamp > datetime('now', '-1 hour')
    ) AS loss_pct_1h
FROM {ping_table} o
WHERE timestamp > datetime('now', '-1 day')
GROUP BY target_host
""".strip()

    views: dict = {
        summary_view: ViewDef(
            query=summary_query,
            title=f"{prefix} ホスト別サマリー(直近 24h)",
            labels={
                "target_host": "ホスト",
                "total_count": "計測数",
                "success_pct": "成功率 (%)",
                "avg_ms": "平均応答 (ms)",
                "loss_pct_1h": "直近1h ロス率 (%)",
            },
            styles={
                "success_pct": CellStyle(
                    less_than=Threshold(**{"value": 95.0, "class": "text-warning"}),
                ),
                "loss_pct_1h": CellStyle(
                    greater_than=Threshold(
                        **{"value": loss_threshold_pct, "class": "text-danger"}
                    ),
                ),
            },
            group="ping 監視",
        )
    }

    # --- ホスト別応答時間ビュー(line チャート) ---
    if hosts is not None:
        for host in hosts:
            sanitized = re.sub(r"[^0-9A-Za-z_]", "_", host)
            view_name = f"{prefix}_rt_{sanitized}"
            escaped_host = host.replace("'", "''")
            rt_query = f"""
SELECT timestamp, response_time
FROM {ping_table}
WHERE target_host = '{escaped_host}'
  AND timestamp > datetime('now', '-1 day')
ORDER BY timestamp
""".strip()
            views[view_name] = ViewDef(
                query=rt_query,
                title=f"{prefix} 応答時間: {host}(直近 24h)",
                labels={"timestamp": "時刻", "response_time": "応答時間 (ms)"},
                chart=ChartDef(
                    type="line",
                    x="timestamp",
                    y="response_time",
                    x_label="時刻",
                    y_label="応答時間 (ms)",
                ),
                group="ping 監視",
            )

    # --- KPI ---
    kpis = {
        f"{prefix}_success_pct_24h": KpiCard(
            title="全体成功率(24h)",
            query=f"SELECT AVG(CASE WHEN packet_loss THEN 0.0 ELSE 100.0 END) FROM {ping_table} WHERE timestamp > datetime('now', '-1 day')",
            unit="%",
            target=95.0,
            higher_is_better=True,
            format="{:.1f}",
            link_view=summary_view,
        ),
        f"{prefix}_avg_ms_24h": KpiCard(
            title="全体平均応答 ms(24h)",
            query=f"SELECT AVG(response_time) FROM {ping_table} WHERE timestamp > datetime('now', '-1 day')",
            unit="ms",
            higher_is_better=False,
            format="{:.1f}",
            link_view=summary_view,
        ),
    }

    # --- アラート ---
    alerts: list[AlertRule] = []
    if with_alerts:
        alerts.append(
            AlertRule(
                view=summary_view,
                column="loss_pct_1h",
                op=">",
                value=loss_threshold_pct,
                level="warning",
                message=f"直近 1h のロス率が {loss_threshold_pct}% を超えています",
            )
        )

    return merge(
        config,
        tables=tables,
        views=views,
        kpis=kpis,
        alerts=alerts,
        sources=sources,
    )
