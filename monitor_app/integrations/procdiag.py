"""procdiag(ホストリソース診断ツール)のプリセット。

``attach()`` で procdiag の可視化一式(テーブル・ソース・ビュー・KPI・アラート)を
既存の MonitorConfig に合成して返す。
"""

from __future__ import annotations

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
    db_path: str,  # procdiag.db のパス
    findings_json: str | None = None,  # findings JSON のパス(None なら所見系を省略)
    prefix: str = "procdiag",
    with_alerts: bool = True,
) -> MonitorConfig:
    """procdiag の可視化一式(テーブル・ソース・ビュー・KPI・アラート)を合成する。"""

    # ---- テーブル定義 --------------------------------------------------------
    sys_table = TableDef(
        columns={
            "ts_min": "float",
            "cpu_avg": "float",
            "cpu_max": "float",
            "cpu_p95": "float",
            "cpu_max_core_max": "float",
            "mem_used_avg": "int",
            "mem_used_max": "int",
            "psi_cpu_max": "float",
            "psi_mem_max": "float",
            "psi_io_max": "float",
            "swap_out_sum": "int",
        },
        primary_key="ts_min",
    )

    tables: dict = {f"{prefix}_sys": sys_table}

    if findings_json is not None:
        findings_table = TableDef(
            columns={
                "id": "int",
                "rule_id": "str",
                "severity": "str",
                "subject": "str",
                "summary": "str",
                "confidence": "str",
                "proposal": "str",
                "start_ts": "float",
                "end_ts": "float",
            },
            primary_key="id",
        )
        tables[f"{prefix}_findings"] = findings_table

    # ---- ソース定義 ----------------------------------------------------------
    sources: dict = {
        f"{prefix}_sys": SourceDef(
            kind="sqlite",
            path=db_path,
            table=f"{prefix}_sys",
            source_table="sys_rollup_1m",
            watermark_column="ts_min",
        )
    }

    if findings_json is not None:
        sources[f"{prefix}_findings"] = SourceDef(
            kind="json",
            path=findings_json,
            table=f"{prefix}_findings",
            record_path="findings",
            mode="replace",
            mapping={
                "rule_id": "rule_id",
                "severity": "severity",
                "subject": "subject",
                "summary": "summary",
                "confidence": "confidence",
                "proposal": "proposal",
                "detected_at.0": "start_ts",
                "detected_at.1": "end_ts",
            },
        )

    # ---- ビュー定義 ----------------------------------------------------------
    _where_24h = f"WHERE ts_min > strftime('%s','now') - 86400"

    views: dict = {
        f"{prefix}_cpu": ViewDef(
            query=(
                f"SELECT datetime(ts_min, 'unixepoch', 'localtime') AS ts,"
                f" cpu_avg, cpu_max"
                f" FROM {prefix}_sys"
                f" {_where_24h}"
                f" ORDER BY ts_min"
            ),
            title="CPU 使用率(直近 24h)",
            labels={
                "ts": "時刻",
                "cpu_avg": "CPU 平均 (%)",
                "cpu_max": "CPU 最大 (%)",
            },
            chart=ChartDef(
                type="line",
                x="ts",
                y=["cpu_avg", "cpu_max"],
                x_label="時刻",
                y_label="CPU 使用率 (%)",
            ),
            group="ホストリソース",
        ),
        f"{prefix}_memory": ViewDef(
            query=(
                f"SELECT datetime(ts_min, 'unixepoch', 'localtime') AS ts,"
                f" ROUND(mem_used_avg / 1048576.0, 2) AS mem_used_avg,"
                f" ROUND(mem_used_max / 1048576.0, 2) AS mem_used_max"
                f" FROM {prefix}_sys"
                f" {_where_24h}"
                f" ORDER BY ts_min"
            ),
            title="メモリ使用量 MB(直近 24h)",
            labels={
                "ts": "時刻",
                "mem_used_avg": "メモリ平均 (MB)",
                "mem_used_max": "メモリ最大 (MB)",
            },
            chart=ChartDef(
                type="line",
                x="ts",
                y=["mem_used_avg", "mem_used_max"],
                x_label="時刻",
                y_label="メモリ使用量 (MB)",
            ),
            group="ホストリソース",
        ),
        f"{prefix}_psi": ViewDef(
            query=(
                f"SELECT datetime(ts_min, 'unixepoch', 'localtime') AS ts,"
                f" psi_cpu_max, psi_mem_max, psi_io_max"
                f" FROM {prefix}_sys"
                f" {_where_24h}"
                f" ORDER BY ts_min"
            ),
            title="PSI(直近 24h)",
            labels={
                "ts": "時刻",
                "psi_cpu_max": "CPU ストール (%)",
                "psi_mem_max": "メモリ ストール (%)",
                "psi_io_max": "I/O ストール (%)",
            },
            chart=ChartDef(
                type="line",
                x="ts",
                y=["psi_cpu_max", "psi_mem_max", "psi_io_max"],
                x_label="時刻",
                y_label="ストール率 (%)",
            ),
            group="ホストリソース",
        ),
    }

    if findings_json is not None:
        views[f"{prefix}_findings"] = ViewDef(
            query=(
                f"SELECT id, rule_id, severity, subject, summary,"
                f" confidence, proposal, start_ts, end_ts,"
                f" CASE severity"
                f" WHEN 'critical' THEN 2"
                f" WHEN 'warning' THEN 1"
                f" ELSE 0"
                f" END AS sev_rank"
                f" FROM {prefix}_findings"
                f" ORDER BY sev_rank DESC, start_ts DESC"
            ),
            title="診断所見",
            labels={
                "id": "ID",
                "rule_id": "ルール",
                "severity": "深刻度",
                "subject": "対象",
                "summary": "概要",
                "confidence": "確度",
                "proposal": "対処提案",
                "start_ts": "検知開始",
                "end_ts": "検知終了",
                "sev_rank": "深刻度ランク",
            },
            styles={
                "sev_rank": CellStyle(
                    # sev_rank > 1 (つまり >= 2: critical) で赤系
                    greater_than=Threshold(
                        **{"value": 1, "class": "bg-danger text-white"}
                    ),
                    # sev_rank == 1 (warning) で黄系
                    equal_to=Threshold(**{"value": 1, "class": "bg-warning"}),
                )
            },
            group="ホストリソース",
        )

    # ---- KPI 定義 -----------------------------------------------------------
    kpis: dict = {
        f"{prefix}_cpu_avg": KpiCard(
            title="直近 CPU 使用率(%)",
            query=(
                f"SELECT cpu_avg FROM {prefix}_sys" f" ORDER BY ts_min DESC LIMIT 1"
            ),
            unit="%",
            higher_is_better=False,
            format="{:.1f}",
            link_view=f"{prefix}_cpu",
        ),
        f"{prefix}_mem_used_avg": KpiCard(
            title="直近メモリ使用量(MB)",
            query=(
                f"SELECT ROUND(mem_used_avg / 1048576.0, 1) FROM {prefix}_sys"
                f" ORDER BY ts_min DESC LIMIT 1"
            ),
            unit="MB",
            higher_is_better=False,
            format="{:.1f}",
            link_view=f"{prefix}_memory",
        ),
    }

    if findings_json is not None:
        kpis[f"{prefix}_critical_count"] = KpiCard(
            title="critical 所見件数",
            query=(
                f"SELECT COUNT(*) FROM {prefix}_findings"
                f" WHERE severity = 'critical'"
            ),
            unit="件",
            higher_is_better=False,
            format="{:.0f}",
            link_view=f"{prefix}_findings",
        )

    # ---- アラート定義 -------------------------------------------------------
    alerts: list = []

    if with_alerts and findings_json is not None:
        alerts.append(
            AlertRule(
                view=f"{prefix}_findings",
                column="sev_rank",
                op=">=",
                value=2,
                level="critical",
                message="critical 診断所見があります",
            )
        )

    return merge(
        config,
        tables=tables,
        views=views,
        kpis=kpis,
        alerts=alerts if alerts else None,
        sources=sources,
    )
