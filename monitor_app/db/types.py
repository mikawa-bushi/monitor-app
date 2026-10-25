"""論理型 ``datetime`` の SQL 型(DB ごとの保存形式をそろえる)。

- **SQLite** … 文字列 ``YYYY-MM-DD HH:MM:SS`` で保存し、マイクロ秒が 0 でないときだけ
  ``.ffffff`` を付ける(SQLAlchemy の既定は常に ``.000000`` を付け、ビュー・表・
  グラフの軸に ``2026-10-21 08:00:00.000000`` と出ていた)。この形は文字列の大小が
  時刻の前後と一致するため、生 SQL の ``ORDER BY`` / ``WHERE ts >= datetime('now',
  '-1 day')`` や保持期間の比較もそのまま正しく動く。読み出しは寛容にし、以前の版の
  文字列の列を datetime に変えたときに残る ``2026/10/21 8:00`` や ``T`` 区切りの値も
  日時として返す(読めない値は文字列のまま返し、一覧の API を 500 にしない — D3-04)。
- **MySQL / MariaDB** … ``DATETIME(6)``(既定の ``DATETIME`` は小数秒を丸め、
  23:59:59.7 が翌日の 00:00:00 になる — D3-05)。
- **PostgreSQL** … 標準の ``timestamp without time zone``(マイクロ秒まで保存する)。

型名はどの DB でも ``DATETIME`` 系のまま(作成済みの表の型の照合
:func:`~monitor_app.db.schema.type_family` は従来どおり datetime と分類する)。
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

from sqlalchemy import DateTime
from sqlalchemy.dialects import mysql, sqlite


def format_sqlite_datetime(value: _dt.datetime | _dt.date) -> str:
    """SQLite に保存する日時の文字列(マイクロ秒が 0 なら秒まで)。

    タイムゾーン付きの日時は、ほかの経路(:func:`~monitor_app.services.coercion.coerce`)
    と同じく現地時刻へ直してから保存する。
    """
    if isinstance(value, _dt.datetime):
        if value.tzinfo is not None and value.utcoffset() is not None:
            from ..services.coercion import to_local_naive

            value = to_local_naive(value)
        # 基底クラスの isoformat を呼ぶ(pandas.Timestamp などのサブクラスはナノ秒を
        # 付けることがある)。マイクロ秒が 0 なら秒までになる。
        return _dt.datetime.isoformat(value.replace(tzinfo=None), " ")
    return f"{value.isoformat()} 00:00:00"


def parse_stored_datetime(value: Any) -> Any:
    """SQLite の日時列から読んだ値を ``datetime`` にする(読めなければそのまま返す)。

    この型が書いた形・SQLAlchemy の既定の形(``.000000`` 付き)・ISO 8601 の
    ``T`` 区切り・Excel の ``2026/10/21 8:00`` などを読む。数値(ほかのツールが書いた
    UNIX 時刻など)や読めない文字列は、例外にせず元の値を返す。
    """
    if not isinstance(value, str):
        return value
    try:
        parsed = _dt.datetime.fromisoformat(value)
    except ValueError:
        from ..services.coercion import parse_datetime

        try:
            return parse_datetime(value.strip())
        except (ValueError, TypeError, OverflowError):
            return value
    if parsed.tzinfo is not None:
        from ..services.coercion import to_local_naive

        return to_local_naive(parsed)
    return parsed


class SQLiteDateTime(sqlite.DATETIME):
    """SQLite 用の日時型(保存形式はモジュールの説明を参照)。DDL は ``DATETIME``。"""

    def bind_processor(self, dialect):
        datetime_cls = _dt.datetime
        date_cls = _dt.date

        def process(value):
            if value is None:
                return None
            if isinstance(value, (datetime_cls, date_cls)):
                return format_sqlite_datetime(value)
            if isinstance(value, str):
                # 文字列で渡された日時(手書きのクエリなど)も同じ形にそろえる。
                parsed = parse_stored_datetime(value)
                if isinstance(parsed, datetime_cls):
                    return format_sqlite_datetime(parsed)
            raise TypeError(
                "SQLite DateTime type only accepts Python datetime and date objects "
                "as input."
            )

        return process

    def result_processor(self, dialect, coltype):
        return parse_stored_datetime


def datetime_type() -> DateTime:
    """論理型 ``datetime`` の列の型(DB ごとの違いはモジュールの説明を参照)。"""
    return (
        DateTime()
        .with_variant(SQLiteDateTime(), "sqlite")
        .with_variant(mysql.DATETIME(fsp=6), "mysql", "mariadb")
    )
