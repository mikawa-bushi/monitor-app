"""monitor-app: CSV を置くだけで Web UI と REST API が生えるデータ監視ツール。

公開 API として、ユーザーが ``config.py`` で使う宣言モデルを再エクスポートする。
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _dist_version

from .settings.declarative import (
    AlertRule,
    CellStyle,
    ChartDef,
    DashboardConfig,
    FormDef,
    KioskConfig,
    KpiCard,
    MonitorConfig,
    Retention,
    SourceDef,
    TableDef,
    Threshold,
    ViewDef,
)

try:
    # バージョンの真実は pyproject.toml の 1 か所だけに置く(配布メタデータから読む)。
    __version__ = _dist_version("monitor-app")
except PackageNotFoundError:  # pragma: no cover - ソースツリーを直接 import した場合
    __version__ = "0.0.0+unknown"

__all__ = [
    "AlertRule",
    "CellStyle",
    "ChartDef",
    "DashboardConfig",
    "FormDef",
    "KioskConfig",
    "KpiCard",
    "MonitorConfig",
    "Retention",
    "SourceDef",
    "TableDef",
    "Threshold",
    "ViewDef",
    "__version__",
]
