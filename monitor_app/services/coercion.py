"""論理型に基づく値変換。CSV 取り込みと API 入力の双方で使う。"""

from __future__ import annotations

import datetime as _dt
import logging
import math
import re
from typing import Any

from ..settings.declarative import ColumnType

_TRUTHY = {"1", "true", "yes", "y", "t", "on"}
_FALSY = {"0", "false", "no", "n", "f", "off", ""}

# 日付・日時の表記ゆれ(CRIT-12)。Excel の日本語 CSV は「2026/10/21 8:00」のように
# スラッシュ区切り・ゼロ埋めなしで書き出す。区切り(- か /)は年月日で揃える。
_DATE_RE = re.compile(r"(\d{4})([-/])(\d{1,2})\2(\d{1,2})")
_DATETIME_RE = re.compile(
    r"(\d{4})([-/])(\d{1,2})\2(\d{1,2})"  # 年月日
    r"(?:(?:[Tt]|\s+)(\d{1,2}):(\d{1,2})"  # 時:分(T か空白の後)
    r"(?::(\d{1,2})(?:[.,](\d+))?)?"  # :秒.小数(任意)
    r"\s*([Zz]|[+-]\d{2}(?::?\d{2})?)?)?"  # タイムゾーン(任意)
)


logger = logging.getLogger("monitor_app.coercion")

_nonfinite_noted = False


class CoercionError(ValueError):
    """値を目的の型に変換できなかったことを表す。"""


def finite_or_none(value: float) -> float | None:
    """NaN・±inf を欠損値(None)にする(有限の値はそのまま返す)。

    センサーは欠測を ``NaN`` / ``inf`` と書くことが多い。そのまま保存すると DB ごとに
    扱いが違い(SQLite は NaN を NULL・inf はそのまま、PostgreSQL は AVG・MAX が NaN に
    なり、MySQL は取り込み全体が失敗する)、JSON にも書き出せないため、どの DB でも
    空(NULL)として保存する(D3-14)。最初の 1 回だけログに残す。
    """
    if math.isfinite(value):
        return value
    global _nonfinite_noted
    if not _nonfinite_noted:
        _nonfinite_noted = True
        logger.info(
            "float の列の値 %r を欠損値(空)として保存します"
            "(NaN・inf はどのデータベースでも空として扱います。以後は表示しません)",
            value,
        )
    return None


def _parse_offset(text: str) -> _dt.timezone:
    if text in ("Z", "z"):
        return _dt.UTC
    sign = -1 if text[0] == "-" else 1
    digits = text[1:].replace(":", "")
    hours, minutes = int(digits[:2]), int(digits[2:] or 0)
    return _dt.timezone(sign * _dt.timedelta(hours=hours, minutes=minutes))


def to_local_naive(value: _dt.datetime) -> _dt.datetime:
    """タイムゾーン付きの日時をこのコンピュータの現地時刻へ直し、tzinfo を外す。

    DB の日時列はタイムゾーンを持たない(SQLite / MySQL の DATETIME、PostgreSQL の
    timestamp without time zone)。``2026-10-21T08:00:00Z`` などは保存前に現地時刻へ
    揃え、ほかの行(現地時刻で書かれた値)と並べ替え・比較できるようにする。
    """
    if value.tzinfo is None or value.utcoffset() is None:
        return value
    try:
        local = value.astimezone()
    except (OverflowError, OSError, ValueError):
        # 1970 年より前などで OS の現地時刻変換が使えないとき(Windows)は、現在の
        # UTC からのずれで換算する。
        offset = _dt.datetime.now().astimezone().utcoffset() or _dt.timedelta(0)
        local = value.astimezone(_dt.timezone(offset))
    return local.replace(tzinfo=None)


def parse_date(text: str) -> _dt.date:
    """``YYYY-MM-DD`` に加え ``YYYY/M/D`` などのゼロ埋めなしも受け付ける。

    それ以外は :meth:`datetime.date.fromisoformat` に任せる(Python が ISO 8601 として
    読める表記は従来どおり受け付ける)。読めなければ ValueError。
    """
    m = _DATE_RE.fullmatch(text)
    if m:
        return _dt.date(int(m[1]), int(m[3]), int(m[4]))
    return _dt.date.fromisoformat(text)


def parse_datetime(text: str) -> _dt.datetime:
    """日時の文字列を読む(タイムゾーン付きなら現地時刻へ直して tzinfo を外す)。

    受け付ける表記: ISO 8601(日付と時刻の間は ``T`` か空白、秒・小数秒は任意、末尾に
    ``Z`` や ``+09:00`` / ``+0900``)、``YYYY/M/D H:MM[:SS]``(ゼロ埋めなし可)、
    日付だけ(0 時 0 分)。小数秒は 6 桁(マイクロ秒)まで使う。それ以外は
    :meth:`datetime.datetime.fromisoformat` に任せる。読めなければ ValueError。
    """
    m = _DATETIME_RE.fullmatch(text)
    if m:
        fraction = (m[8] or "")[:6].ljust(6, "0")
        tz: _dt.timezone | None = _parse_offset(m[9]) if m[9] else None
        value = _dt.datetime(
            int(m[1]),
            int(m[3]),
            int(m[4]),
            int(m[5] or 0),
            int(m[6] or 0),
            int(m[7] or 0),
            int(fraction),
            tzinfo=tz,
        )
    else:
        value = _dt.datetime.fromisoformat(text)
    return to_local_naive(value)


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
            return finite_or_none(float(str(value).strip()))
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
            return parse_date(str(value).strip())
        if logical == "datetime":
            if isinstance(value, _dt.datetime):
                return to_local_naive(value)
            if isinstance(value, _dt.date):
                return _dt.datetime.combine(value, _dt.time())
            return parse_datetime(str(value).strip())
        return str(value)
    except CoercionError:
        raise
    except (ValueError, TypeError, OverflowError) as exc:
        raise CoercionError(f"'{value}' を {logical} に変換できません") from exc
