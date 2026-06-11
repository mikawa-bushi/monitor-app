"""論理型に基づく値変換。CSV 取り込みと API 入力の双方で使う。"""

from __future__ import annotations

import datetime as _dt
from typing import Any

from ..settings.declarative import ColumnType

_TRUTHY = {"1", "true", "yes", "y", "t", "on"}
_FALSY = {"0", "false", "no", "n", "f", "off", ""}


class CoercionError(ValueError):
    """値を目的の型に変換できなかったことを表す。"""


def coerce(value: Any, logical: ColumnType) -> Any:
    """``value`` を論理型 ``logical`` に変換する。空文字・None は None を返す。"""
    if value is None:
        return None
    if isinstance(value, str) and value.strip() == "" and logical != "str":
        return None

    try:
        if logical == "int":
            return int(str(value).strip())
        if logical == "float":
            return float(str(value).strip())
        if logical == "bool":
            s = str(value).strip().lower()
            if s in _TRUTHY:
                return True
            if s in _FALSY:
                return False
            raise CoercionError(f"'{value}' を bool に変換できません")
        if logical == "date":
            if isinstance(value, (_dt.date, _dt.datetime)):
                return value
            return _dt.date.fromisoformat(str(value).strip())
        return str(value)
    except CoercionError:
        raise
    except (ValueError, TypeError) as exc:
        raise CoercionError(f"'{value}' を {logical} に変換できません") from exc
