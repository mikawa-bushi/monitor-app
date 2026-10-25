"""行の変換: 外部の値(CSV の文字列・JSON・SQLite の値)を列の論理型へ変換する。

テーブルごとに 1 度だけ各列の論理型と変換関数を決める(:class:`RowConverter`)。
値ごとに ``coerce()`` と ``TableDef.column_type()`` を引き直さないため、大きな CSV の
取り込みが速い(PERF-04)。変換結果とエラーメッセージは
:func:`~monitor_app.services.coercion.coerce` と同じ。

CSV はヘッダの並びから「どの列を何番目のセルから読むか」を 1 度だけ決める
(:class:`HeaderPlan`)。``csv.DictReader`` と同じ結果になる: 同じ列名が 2 回あれば
後ろのセル、セルが足りない行は None、余分なセルは無視。
"""

from __future__ import annotations

import datetime as _dt
import json
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from math import isfinite
from typing import Any

from ..settings.declarative import ColumnType, TableDef
from .coercion import (
    _FALSY,
    _TRUTHY,
    CoercionError,
    coerce,
    finite_or_none,
    parse_date,
    parse_datetime,
    to_local_naive,
)

#: 1 つの値を変換する関数(失敗すれば CoercionError)。
Converter = Callable[[Any], Any]


# ---------------------------------------------------------------------------
# 列ごとの変換関数(文字列は速い経路、それ以外の型は coerce() に任せる)
#
# 文字列はまずそのまま int() / float() / fromisoformat() に渡し、失敗したときだけ
# coerce() と同じ手順(前後の空白を除いてから変換)でやり直す。成功したときの結果は
# coerce() と同じ(int() などが読み飛ばす空白は str.strip() でも除かれる)。
# ---------------------------------------------------------------------------
_BOOL_WORDS: dict[str, bool] = {
    **{word: True for word in _TRUTHY},
    **{word: False for word in _FALSY if word},
}


def _to_str(value: Any) -> Any:
    if value is None or type(value) is str:
        return value
    return str(value)


def _to_int(value: Any) -> Any:
    if type(value) is str:
        if value:
            try:
                return int(value)
            except ValueError:
                pass
        text = value.strip()
        if not text:
            return None
        try:
            return int(text)
        except ValueError:
            raise CoercionError(f"'{value}' を int に変換できません") from None
    return coerce(value, "int")


def _to_float(value: Any) -> Any:
    # NaN・inf は欠損値(None)にする(coerce() と同じ — D3-14)。
    if type(value) is str:
        if value:
            try:
                result = float(value)
            except ValueError:
                pass
            else:
                return result if isfinite(result) else finite_or_none(result)
        text = value.strip()
        if not text:
            return None
        try:
            return finite_or_none(float(text))
        except ValueError:
            raise CoercionError(f"'{value}' を float に変換できません") from None
    return coerce(value, "float")


def _to_bool(value: Any) -> Any:
    if type(value) is str:
        known = _BOOL_WORDS.get(value)
        if known is not None:
            return known
        text = value.strip().lower()
        if not text:
            return None
        if text in _TRUTHY:
            return True
        if text in _FALSY:
            return False
        raise CoercionError(f"'{value}' を bool に変換できません")
    return coerce(value, "bool")


def _to_date(value: Any) -> Any:
    if type(value) is str:
        if value and "/" not in value:
            try:
                return _dt.date.fromisoformat(value)
            except ValueError:
                pass
        text = value.strip()
        if not text:
            return None
        try:
            return parse_date(text)
        except (ValueError, TypeError, OverflowError):
            raise CoercionError(f"'{value}' を date に変換できません") from None
    return coerce(value, "date")


def _to_datetime(value: Any) -> Any:
    if type(value) is str:
        if value and "/" not in value:
            try:
                return to_local_naive(_dt.datetime.fromisoformat(value))
            except ValueError:
                pass
        text = value.strip()
        if not text:
            return None
        try:
            return parse_datetime(text)
        except (ValueError, TypeError, OverflowError):
            raise CoercionError(f"'{value}' を datetime に変換できません") from None
    return coerce(value, "datetime")


_CONVERTERS: dict[str, Converter] = {
    "str": _to_str,
    "int": _to_int,
    "float": _to_float,
    "bool": _to_bool,
    "date": _to_date,
    "datetime": _to_datetime,
}


def converter_for(logical: ColumnType) -> Converter:
    """論理型の変換関数(``coerce(value, logical)`` と同じ結果を返す)。"""
    return _CONVERTERS[logical]


# ---------------------------------------------------------------------------
# 外部ソースのレコード(dict)の走査
# ---------------------------------------------------------------------------
def dot_get(obj: Any, key: str) -> Any:
    """ドット記法キーで obj を走査する。数値トークンはリストインデックス。"""
    cur = obj
    for part in key.split("."):
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                raise KeyError(key) from None
        elif isinstance(cur, dict):
            if part not in cur:
                raise KeyError(key)
            cur = cur[part]
        else:
            raise KeyError(key)
    return cur


def flatten(value: Any) -> Any:
    """list/dict の平坦化: list → 空白区切り文字列、dict → JSON 文字列。"""
    if isinstance(value, list):
        return " ".join(str(v) for v in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return value


class ColumnError(CoercionError):
    """どの列の変換に失敗したかを持つ :class:`CoercionError`(メッセージは同じ)。"""

    def __init__(self, column: str, error: CoercionError) -> None:
        super().__init__(str(error))
        self.column = column


# ---------------------------------------------------------------------------
# CSV のヘッダ
# ---------------------------------------------------------------------------
def normalize_name(name: str) -> str:
    """列名の表記ゆれをそろえた形(前後の空白・BOM・全角/半角・大文字小文字)。"""
    return unicodedata.normalize("NFKC", name).replace("\ufeff", "").strip().casefold()


@dataclass
class HeaderPlan:
    """CSV のヘッダから決めた「どの列を何番目のセルから読むか」。

    ``cells`` は (セル番号, 列名, 変換関数) で、ヘッダに最初に現れた順
    (``csv.DictReader`` の dict と同じ順。複数の列が変換に失敗した行では、最初の
    列のエラーを報告する)。
    """

    header: list[str]
    cells: list[tuple[int, str, Converter]]
    #: ヘッダにある config.py の列(宣言順)。
    matched: list[str] = field(default_factory=list)
    #: ヘッダに無い config.py の列(宣言順)。
    missing: list[str] = field(default_factory=list)
    #: config.py に無いヘッダのセル(ヘッダの順)。
    unknown: list[str] = field(default_factory=list)
    #: 表記ゆれから対応付けたセル → 列名(例: ``"Name" → "name"``)。
    renamed: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # str の列は変換しない(セルの文字列をそのまま使う)。
        self._fast = [
            (idx, column, None if conv is _to_str else conv)
            for idx, column, conv in self.cells
        ]
        self._width = max((idx for idx, _col, _conv in self.cells), default=-1) + 1

    def convert(self, raw: Sequence[str]) -> dict[str, Any]:
        """CSV の 1 行(セルの並び)を ``{列名: 値}`` にする(失敗すれば CoercionError)。"""
        if len(raw) >= self._width:
            return {
                column: raw[idx] if conv is None else conv(raw[idx])
                for idx, column, conv in self._fast
            }
        # セルが足りない行: 足りない列は None(csv.DictReader の restval と同じ)。
        n = len(raw)
        return {
            column: (
                None if idx >= n else (raw[idx] if conv is None else conv(raw[idx]))
            )
            for idx, column, conv in self._fast
        }


class RowConverter:
    """1 テーブル分の変換規則(列ごとの論理型と変換関数を 1 度だけ決める)。

    ``mapping`` は外部ソースの ``{列名: ドット記法のソースキー}``
    (:attr:`~monitor_app.settings.declarative.SourceDef.mapping`)。
    """

    def __init__(
        self, tdef: TableDef, *, mapping: Mapping[str, str] | None = None
    ) -> None:
        self.tdef = tdef
        self.columns: list[str] = tdef.column_names
        self.types: dict[str, ColumnType] = {
            col: tdef.column_type(col) for col in self.columns
        }
        self.converters: dict[str, Converter] = {
            col: converter_for(logical) for col, logical in self.types.items()
        }
        self._mapping: list[tuple[str, str, Converter]] = [
            (dest, src, self.converters[dest])
            for dest, src in (mapping or {}).items()
            if dest in self.converters
        ]
        self._use_mapping = bool(mapping)

    def convert(self, column: str, value: Any) -> Any:
        """``coerce(value, column_type(column))`` と同じ。"""
        return self.converters[column](value)

    def inferred_str_columns(self) -> list[str]:
        """列名からの推論(``columns`` がリスト)で ``str`` になった列。"""
        if isinstance(self.tdef.columns, dict):
            return []
        return [col for col in self.columns if self.types[col] == "str"]

    # ------------------------------------------------------------------
    # 外部ソースのレコード
    # ------------------------------------------------------------------
    def from_record(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        """外部ソースの 1 レコードを ``{列名: 値}`` にする(コネクタ用)。

        ``mapping`` があれば各列をドット記法のソースキーから読み(キーが無ければ
        None)、無ければソースのキーをそのまま列名として使う(config.py に無いキーは
        捨てる)。list / dict の値は平坦化する。失敗した列は :class:`ColumnError`。
        """
        record: dict[str, Any] = {}
        if self._use_mapping:
            for dest, src, conv in self._mapping:
                try:
                    value = dot_get(raw, src)
                except KeyError:
                    value = None
                try:
                    record[dest] = conv(flatten(value))
                except CoercionError as exc:
                    raise ColumnError(dest, exc) from exc
            return record
        converters = self.converters
        for key, value in raw.items():
            conv = converters.get(key)
            if conv is None:
                continue
            try:
                record[key] = conv(flatten(value))
            except CoercionError as exc:
                raise ColumnError(key, exc) from exc
        return record

    # ------------------------------------------------------------------
    # CSV のヘッダ
    # ------------------------------------------------------------------
    def header_plan(self, header: Sequence[str]) -> HeaderPlan:
        """CSV のヘッダを config.py の列と突き合わせる。

        列名がそのまま一致するセルを優先し、残りは表記ゆれ(前後の空白・BOM・全角/
        半角・大文字小文字)をそろえて 1 つの列にだけ当てはまるものを対応付ける。
        同じ列に当たるセルが複数あれば後ろのセルを使う(``csv.DictReader`` と同じ)。
        """
        header = list(header)
        declared = set(self.columns)
        by_norm: dict[str, list[str]] = {}
        for col in self.columns:
            by_norm.setdefault(normalize_name(col), []).append(col)
        exact = {cell for cell in header if cell in declared}
        last: dict[str, int] = {}
        first: dict[str, int] = {}
        unknown: list[str] = []
        renamed: dict[str, str] = {}
        for idx, cell in enumerate(header):
            if cell in declared:
                column = cell
            else:
                candidates = [
                    c for c in by_norm.get(normalize_name(cell), ()) if c not in exact
                ]
                if len(candidates) != 1:
                    unknown.append(cell)
                    continue
                column = candidates[0]
                renamed[cell] = column
            last[column] = idx
            first.setdefault(column, idx)
        cells = sorted(
            ((last[col], col, self.converters[col]) for col in last),
            key=lambda item: first[item[1]],
        )
        return HeaderPlan(
            header=header,
            cells=cells,
            matched=[col for col in self.columns if col in last],
            missing=[col for col in self.columns if col not in last],
            unknown=unknown,
            renamed=renamed,
        )
