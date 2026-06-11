"""ユーザーの ``config.py`` を読み込み、:class:`MonitorConfig` を得る。

対応する書き方は 2 通り:

1. **v2 形式** … モジュール内に ``config = MonitorConfig(...)`` がある。
2. **v1 形式(後方互換)** … ``ALLOWED_TABLES`` / ``VIEW_TABLES`` /
   ``TABLE_CELL_STYLES`` などのモジュール変数がある。

``ValidationError`` は初心者にも分かるよう日本語に整形して送出する。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

from pydantic import ValidationError

from .declarative import CellStyle, MonitorConfig, TableDef, ViewDef


class ConfigError(Exception):
    """設定ファイルの読み込み・検証に失敗したことを表す。"""


def _import_module_from_path(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location("monitor_user_config", path)
    if spec is None or spec.loader is None:
        raise ConfigError(f"設定ファイルを読み込めません: {path}")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 - ユーザーコードの任意例外を包む
        raise ConfigError(f"config.py の実行中にエラーが発生しました: {exc}") from exc
    return module


def _from_v1_module(module: ModuleType) -> MonitorConfig:
    """v1 形式のモジュール変数から :class:`MonitorConfig` を組み立てる。"""

    allowed: dict[str, Any] = getattr(module, "ALLOWED_TABLES", {})
    view_tables: dict[str, Any] = getattr(module, "VIEW_TABLES", {})
    cell_styles: dict[str, Any] = getattr(module, "TABLE_CELL_STYLES", {})

    tables = {
        name: TableDef(
            columns=info["columns"],
            primary_key=info.get("primary_key", "id"),
            foreign_keys=info.get("foreign_keys", {}),
        )
        for name, info in allowed.items()
    }

    views = {}
    for name, info in view_tables.items():
        raw_styles = cell_styles.get(name, {})
        styles = {col: CellStyle(**rule) for col, rule in raw_styles.items()}
        views[name] = ViewDef(
            query=info["query"],
            title=info.get("title", name),
            description=info.get("description", ""),
            styles=styles,
        )

    extras: dict[str, Any] = {}
    for src, dst in (
        ("APP_TITLE", "app_title"),
        ("HEADER_TEXT", "header_text"),
        ("FOOTER_TEXT", "footer_text"),
        ("FAVICON_PATH", "favicon_path"),
        ("TABLE_REFRESH_INTERVAL", "refresh_interval_ms"),
    ):
        if hasattr(module, src):
            extras[dst] = getattr(module, src)

    return MonitorConfig(tables=tables, views=views, **extras)


def _format_validation_error(exc: ValidationError) -> str:
    lines = ["config.py の設定に問題があります:"]
    for err in exc.errors():
        loc = " → ".join(str(p) for p in err["loc"]) or "(トップレベル)"
        lines.append(f"  - {loc}: {err['msg']}")
    return "\n".join(lines)


def load_config(path: str | Path) -> MonitorConfig:
    """``config.py`` のパスを受け取り、検証済みの :class:`MonitorConfig` を返す。"""

    path = Path(path)
    if not path.exists():
        raise ConfigError(f"config.py が見つかりません: {path}")

    module = _import_module_from_path(path)

    try:
        config = getattr(module, "config", None)
        if isinstance(config, MonitorConfig):
            return config
        if hasattr(module, "ALLOWED_TABLES"):
            return _from_v1_module(module)
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(exc)) from exc

    raise ConfigError(
        "config.py に MonitorConfig インスタンス(変数名 config)も "
        "v1 形式の ALLOWED_TABLES も見つかりませんでした"
    )
