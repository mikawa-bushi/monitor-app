"""サーバー DB(PostgreSQL / MySQL)の列が受け付ける値の範囲(D3-06)。

SQLite は宣言した文字列の長さや整数の幅を強制しないため、SQLite で取り込めた CSV が
PostgreSQL / MySQL では 1 つの値(255 文字を超える文字列・32 ビットを超える整数)で
取り込み全体の失敗になる。取り込みはこの範囲を超える値の行を型変換の失敗と同じく
行番号つきの警告を残して読み飛ばし、点検(``check --data``)は同じ範囲で警告する。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from sqlalchemy import Table, inspect
from sqlalchemy import types as sqltypes
from sqlalchemy.engine import Connection

from .coercion import CoercionError

logger = logging.getLogger("monitor_app.importer")

#: 整数の型 → ビット数(サブクラスを先に調べる)。
_INT_BITS: tuple[tuple[type, int], ...] = (
    (sqltypes.BigInteger, 64),
    (sqltypes.SmallInteger, 16),
    (sqltypes.Integer, 32),
)
_DB_LABELS = {"postgresql": "PostgreSQL", "mysql": "MySQL", "mariadb": "MariaDB"}
#: メッセージに載せる値の最大の長さ。
_SHOWN = 40


def _shown(value: str) -> str:
    return value if len(value) <= _SHOWN else value[:_SHOWN] + "…"


class ValueLimits:
    """1 テーブル分の列の制限(文字列の最大長・整数の範囲)。

    呼び出すと、変換済みの行(``{列名: 値}``)が制限に収まればそのまま返し、収まらなければ
    :class:`CoercionError` を送出する。
    """

    def __init__(
        self,
        dialect: str,
        lengths: Mapping[str, int],
        ranges: Mapping[str, tuple[int, int]],
    ) -> None:
        self.dialect = dialect
        self.label = _DB_LABELS.get(dialect, dialect)
        self.lengths = dict(lengths)
        self.ranges = dict(ranges)
        self._lengths = list(self.lengths.items())
        self._ranges = [(col, lo, hi) for col, (lo, hi) in self.ranges.items()]

    def __bool__(self) -> bool:
        return bool(self._lengths or self._ranges)

    def violation(self, column: str, value: Any) -> str | None:
        """``value``(変換済み)が列の制限を超える理由(収まれば None)。"""
        limit = self.lengths.get(column)
        if limit is not None and type(value) is str and len(value) > limit:
            return (
                f"列 {column} の値 '{_shown(value)}' が長すぎます({len(value)} 文字。"
                f"{self.label} のこの列は {limit} 文字まで)"
            )
        bounds = self.ranges.get(column)
        if bounds is not None and type(value) is int:
            lo, hi = bounds
            if not lo <= value <= hi:
                return (
                    f"列 {column} の値 {value} は {self.label} のこの列の整数の範囲"
                    f"({lo}〜{hi})を超えています"
                )
        return None

    def __call__(self, record: dict[str, Any]) -> dict[str, Any]:
        for col, limit in self._lengths:
            value = record.get(col)
            if type(value) is str and len(value) > limit:
                raise CoercionError(self.violation(col, value) or "")
        for col, lo, hi in self._ranges:
            value = record.get(col)
            if type(value) is int and not lo <= value <= hi:
                raise CoercionError(self.violation(col, value) or "")
        return record


def value_limits(
    column_types: Mapping[str, sqltypes.TypeEngine], dialect: str
) -> ValueLimits | None:
    """列の SQL 型から制限を作る。SQLite(制限を強制しない)や制限の無い表は None。"""
    if dialect == "sqlite":
        return None
    lengths: dict[str, int] = {}
    ranges: dict[str, tuple[int, int]] = {}
    for name, col_type in column_types.items():
        if isinstance(col_type, sqltypes.Integer):
            for cls, bits in _INT_BITS:
                if isinstance(col_type, cls):
                    ranges[name] = (-(2 ** (bits - 1)), 2 ** (bits - 1) - 1)
                    break
        elif isinstance(col_type, sqltypes.String) and not isinstance(
            col_type, sqltypes.Text
        ):
            length = getattr(col_type, "length", None)
            if isinstance(length, int) and length > 0:
                lengths[name] = length
    limits = ValueLimits(dialect, lengths, ranges)
    return limits if limits else None


def reflected_limits(conn: Connection, table: Table) -> ValueLimits | None:
    """DB にある表の実際の列の型から制限を作る(読めなければ config.py の宣言から)。

    作成済みの表は config.py の型を変えても変わらない(実際の列の型で制限される)。
    PostgreSQL は整数を宣言の型に変換して送る(``%s::INTEGER``)ため、列が BIGINT でも
    宣言が int なら 32 ビットに制限される(文字列は長さなしの ``::VARCHAR`` のため
    列の長さだけ)。
    """
    dialect = conn.dialect.name
    if dialect == "sqlite":
        return None
    declared = value_limits(
        {c.name: c.type.dialect_impl(conn.dialect) for c in table.columns}, dialect
    )
    try:
        types = {c["name"]: c["type"] for c in inspect(conn).get_columns(table.name)}
    except Exception as exc:  # noqa: BLE001 - 読めなければ宣言の型で判断する
        logger.debug("列の型を読めませんでした(%s): %s", table.name, exc)
        return declared
    actual = value_limits(types, dialect)
    if dialect != "postgresql" or declared is None:
        return actual
    ranges = dict(actual.ranges) if actual is not None else {}
    for col, (lo, hi) in declared.ranges.items():
        old_lo, old_hi = ranges.get(col, (lo, hi))
        ranges[col] = (max(lo, old_lo), min(hi, old_hi))
    lengths = actual.lengths if actual is not None else {}
    limits = ValueLimits(dialect, lengths, ranges)
    return limits if limits else None


def declared_limits(table: Table, database_url: str) -> ValueLimits | None:
    """接続しないで、config.py の宣言の型と接続先の DB の種類から制限を作る(点検用)。"""
    from sqlalchemy.engine import make_url

    try:
        url = make_url(database_url)
        if url.get_backend_name() == "sqlite":
            return None
        dialect = url.get_dialect()()
    except Exception:  # noqa: BLE001 - 解釈できない URL は別の点検が報告する
        return None
    types = {c.name: c.type.dialect_impl(dialect) for c in table.columns}
    return value_limits(types, dialect.name)
