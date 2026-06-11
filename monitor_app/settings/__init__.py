"""設定層。

- ``declarative`` … ユーザーが ``config.py`` で書く宣言的定義(Pydantic モデル)
- ``runtime`` … 環境変数で与える実行時設定(pydantic-settings)
- ``loader`` … ユーザーの ``config.py`` を読み込み、検証する
"""

from .declarative import CellStyle, MonitorConfig, TableDef, Threshold, ViewDef
from .runtime import AppSettings

__all__ = [
    "AppSettings",
    "CellStyle",
    "MonitorConfig",
    "TableDef",
    "Threshold",
    "ViewDef",
]
