"""ツール別プリセット(procdiag / netdiag / network_checker)を提供するパッケージ。

各モジュールの attach() でフラグメントを config に合成する。
"""

from __future__ import annotations

from typing import Dict, List, Optional

from ..settings.declarative import (
    AlertRule,
    KpiCard,
    MonitorConfig,
    SourceDef,
    TableDef,
    ViewDef,
)


def merge(
    config: MonitorConfig,
    *,
    tables: Optional[Dict[str, TableDef]] = None,
    views: Optional[Dict[str, ViewDef]] = None,
    kpis: Optional[Dict[str, KpiCard]] = None,
    alerts: Optional[List[AlertRule]] = None,
    sources: Optional[Dict[str, SourceDef]] = None,
) -> MonitorConfig:
    """フラグメントを合成した新しい MonitorConfig を返す(元は変更しない)。

    tables/views/kpis/sources の名前衝突は ValueError。合成後に
    MonitorConfig.model_validate(...model_dump()...) で全バリデータを再実行する。
    """
    new = config.model_copy(deep=True)

    # 衝突チェック: tables/views/kpis/sources の既存キーと重複したら ValueError
    for key, mapping, label in (
        (tables, new.tables, "tables"),
        (views, new.views, "views"),
        (kpis, new.kpis, "kpis"),
        (sources, new.sources, "sources"),
    ):
        if key:
            for name in key:
                if name in mapping:
                    raise ValueError(f"merge: {label}['{name}'] はすでに存在します")

    new.tables.update(tables or {})
    new.views.update(views or {})
    new.kpis.update(kpis or {})
    new.alerts = list(new.alerts) + list(alerts or [])
    new.sources.update(sources or {})
    return MonitorConfig.model_validate(new.model_dump())
