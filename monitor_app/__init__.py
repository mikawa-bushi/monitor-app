"""monitor-app: CSV を置くだけで Web UI と REST API が生えるデータ監視ツール。

公開 API として、ユーザーが ``config.py`` で使う宣言モデルを再エクスポートする。
"""

from .settings.declarative import (
    AlertRule,
    CellStyle,
    ChartDef,
    FormDef,
    KioskConfig,
    KpiCard,
    MonitorConfig,
    TableDef,
    Threshold,
    ViewDef,
)

__version__ = "2.1.0"

__all__ = [
    "AlertRule",
    "CellStyle",
    "ChartDef",
    "FormDef",
    "KioskConfig",
    "KpiCard",
    "MonitorConfig",
    "TableDef",
    "Threshold",
    "ViewDef",
    "__version__",
]
