"""CSV からテーブル定義・ビュー・グラフを推論する(``monitor-app serve`` のゼロ設定起動)。

列の型は列名ではなく **データ** から決める(列名による推論は ``temp`` のような数値の
列を文字列にしてしまうため — #DX-08)。推論した型は、調べたすべての値を取り込みで
変換できる型にする(日時の形式は、取り込みの値変換 ``coerce`` が受け付けるかを形式
ごとに確かめる)。そのため、先頭の行だけを調べる大きなファイルを除き、取り込みで
行が捨てられることはない。

- テーブル名 = ファイル名(拡張子を除く)。日本語も可。
- 型: int(先頭が 0 の ``007`` は str のまま)/ float / bool(true・false・yes・no)/
  date(``2026-10-21``・``2026/10/21``)/ datetime(``2026-10-21T08:00``・
  ``2026/10/21 8:00`` など)/ それ以外は str。空欄は判定に使わない。
- 主キー = 整数で重複も空欄もない ``id`` 列。無ければ自動採番の ``_row_id`` を足す。
- テーブルごとに ``SELECT *`` のビュー(大きな表は新しい順に一定行数)、日時(なければ
  主キー)を横軸にした折れ線グラフ、件数の KPI を作る。

:func:`render_config_py` は推論結果を読みやすい ``config.py`` として書き出す
(``monitor-app serve --write-config``)。
"""

from __future__ import annotations

import csv
import datetime as _dt
import json
import math
import os
import re
import unicodedata
from array import array
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .declarative import (
    ChartDef,
    ColumnType,
    DashboardConfig,
    KpiCard,
    MonitorConfig,
    TableDef,
    ViewDef,
)

#: すべての行から型を推論するファイルサイズの上限。超えると先頭の行だけで推論する。
FULL_SCAN_MAX_BYTES = 50 * 1024 * 1024
#: 大きなファイルで推論に使う先頭の行数。
SAMPLE_ROWS = 100_000
#: これより行数が多いテーブルは、ビューを「主キーの新しい順に TAIL_ROWS 行」に絞る。
TAIL_ROWS = 5_000
#: 推論で試す文字コードの順(Excel の「CSV UTF-8」は BOM 付き、「CSV」は cp932)。
DEFAULT_ENCODINGS = ("utf-8-sig", "cp932")
#: グラフの系列(縦軸)の最大数。
MAX_CHART_SERIES = 4
#: トップページ(ダッシュボード)に並べるグラフの最大数。画面の更新のたびに全グラフの
#: データをまとめて取得するため、CSV が多いフォルダでも重くならないようにする。
MAX_DASHBOARD_CHARTS = 6
#: 使える ``id`` 列が無いときに足す主キー列の名前(取り込み時に自動採番される)。
SYNTHETIC_PK = "_row_id"
#: ブラウザ(JavaScript の数値)で正確に表せる整数の上限。超える整数はコード扱い(str)。
_MAX_SAFE_INT = 2**53 - 1
#: monitor-app が内部で使うテーブル名(CSV のテーブル名にできない)。
_INTERNAL_TABLES = frozenset({"_audit", "_ingest_state"})
#: テーブル名(ファイル名)に使えない文字。引用符・スラッシュ・カンマは SQL・URL・
#: ビューの一括取得の区切りを壊す。``:`` は SQL の ``:名前`` がパラメータとして扱われ
#: (``x :y`` のビューが実行できない)、``# ? %`` は画面のリンク(URL)を壊す。
_UNSAFE_NAME_CHARS = frozenset("'\"/\\,:#?%")
#: 1 列ごとに記憶しておく値の種類の上限(同じ値の判定を省くためのキャッシュ)。
_CACHE_LIMIT = 4096

# 型の候補(ビット)。値ごとに「その値を変換できる型」を求め、列の候補と論理積をとる。
_INT = 1
_FLOAT = 2
_BOOL = 4  # 推論に使う bool(true / false / yes / no のみ。0/1 は int のまま)
_BOOLISH = 8  # 取り込みの bool 変換が受け付ける値(1/0・y/n・on/off なども含む)
_DATE = 16
_DATETIME = 32
_ALL = _INT | _FLOAT | _BOOL | _BOOLISH | _DATE | _DATETIME
#: 候補が複数残ったときに選ぶ順(より狭い型を優先する)。
_PRIORITY: tuple[tuple[int, ColumnType], ...] = (
    (_INT, "int"),
    (_FLOAT, "float"),
    (_BOOL, "bool"),
    (_DATE, "date"),
    (_DATETIME, "datetime"),
)
#: 宣言された型の値として取り込めるかを表すビット(``ColumnScan.accepts``)。
_ACCEPT_BITS: dict[str, int] = {
    "int": _INT,
    "float": _FLOAT,
    "bool": _BOOLISH,
    "date": _DATE,
    "datetime": _DATETIME,
}
#: 整数(先頭の 0 は "0" そのものだけ)と小数の 1 値分の形。
_INT_ONE = r"[+-]?(?:0|[1-9][0-9]*)"
_FLOAT_ONE = r"[+-]?(?:(?:0|[1-9][0-9]*)(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?"
_INT_RE = re.compile(_INT_ONE)
_FLOAT_RE = re.compile(_FLOAT_ONE)
_DATE_RE = re.compile(r"([0-9]{4})([-/])([0-9]{1,2})\2([0-9]{1,2})")
_DATETIME_RE = re.compile(
    r"([0-9]{4})([-/])([0-9]{1,2})\2([0-9]{1,2})[T ]([0-9]{1,2}):([0-9]{2})"
    r"(?::([0-9]{2})(?:\.[0-9]{1,9})?)?(?:Z|[+-][0-9]{2}(?::?[0-9]{2})?)?"
)
_BOOL_TOKENS = frozenset({"true", "false", "yes", "no"})
#: services.coercion の bool 変換が受け付ける値(空欄を除く)。
_BOOLISH_TOKENS = frozenset(
    {"1", "0", "true", "false", "yes", "no", "y", "n", "t", "f", "on", "off"}
)
_NUMERIC_START = frozenset("0123456789+-.")
_DIGITS_TO_NINE = str.maketrans("0123456789", "9999999999")

# まとめて判定するための正規表現(改行で連結した 1 列分の値に 1 回だけ適用する)。
# 1 値ずつ判定するより桁違いに速い。結果は 1 値ずつの判定(classify_value)と同じに
# なるようにする: 1 つの値の中での日付の区切り(- と /)の混在は _MIXED_SEPARATORS、
# 月日の実在は _DATE_PARTS で確かめ、該当するチャンクは 1 値ずつの判定に回す。
_DATE_ONE = r"[0-9]{4}[-/](?:0?[1-9]|1[0-2])[-/](?:0?[1-9]|[12][0-9]|3[01])"
_TIME_ONE = (
    r"[T ](?:[01]?[0-9]|2[0-3]):[0-5][0-9](?::[0-5][0-9](?:\.[0-9]{1,9})?)?"
    r"(?:Z|[+-][0-9]{2}(?::?[0-9]{2})?)?"
)


def _lines_re(one: str) -> re.Pattern:
    return re.compile(rf"(?:{one}\n)*{one}")


_INT_LINES = _lines_re(_INT_ONE)
_FLOAT_LINES = _lines_re(_FLOAT_ONE)
_DATE_LINES = _lines_re(_DATE_ONE)
_DATETIME_LINES = _lines_re(_DATE_ONE + _TIME_ONE)
#: 連結した値から、各行の日付部分(年・月・日)を取り出す。
_DATE_PARTS = re.compile(r"(?m)^([0-9]{4})[-/]([0-9]{1,2})[-/]([0-9]{1,2})")
#: 1 つの値の中で日付の区切りが混在している(2026-10/21)行があるか。
_MIXED_SEPARATORS = re.compile(r"(?m)^[0-9]{4}(?:-[0-9]{1,2}/|/[0-9]{1,2}-)")
#: まとめて判定する行数。
_CHUNK_ROWS = 4096


class InferError(Exception):
    """CSV から設定を推論できないこと(CSV が無い・別々のフォルダにある等)を表す。"""


class CsvReadError(Exception):
    """1 つの CSV を読めないこと(文字コード・見出し・CSV の形式)を表す。"""


# ---------------------------------------------------------------------------
# 値の判定
# ---------------------------------------------------------------------------
def _valid_date(m: re.Match) -> bool:
    try:
        _dt.date(int(m[1]), int(m[3]), int(m[4]))
    except ValueError:
        return False
    return True


def _valid_datetime(m: re.Match) -> bool:
    if not _valid_date(m) or int(m[5]) > 23 or int(m[6]) > 59:
        return False
    return m[7] is None or int(m[7]) <= 59


def classify_value(value: str) -> int:
    """空でない値(前後の空白は除去済み)を変換できる型のビットを返す。"""
    if value[0] in _NUMERIC_START:
        if _INT_RE.fullmatch(value):
            if len(value) > 15 and abs(int(value)) > _MAX_SAFE_INT:
                return 0  # バーコードなどの長い番号。数値にすると桁が落ちる
            bits = _INT | _FLOAT
            if value in ("0", "1"):
                bits |= _BOOLISH
            return bits
        if _FLOAT_RE.fullmatch(value):
            if "e" in value.lower() and not math.isfinite(float(value)):
                return 0
            return _FLOAT
        if len(value) >= 8 and value[0].isdigit():
            m = _DATE_RE.fullmatch(value)
            if m is not None:
                return _DATE | _DATETIME if _valid_date(m) else 0
            m = _DATETIME_RE.fullmatch(value)
            if m is not None:
                return _DATETIME if _valid_datetime(m) else 0
        return 0
    lowered = value.lower()
    if lowered in _BOOL_TOKENS:
        return _BOOL | _BOOLISH
    if lowered in _BOOLISH_TOKENS:
        return _BOOLISH
    return 0


def numeric_kind(value: str) -> ColumnType | None:
    """空でない値(前後の空白は除去済み)が数値なら ``"int"`` / ``"float"``、違えば None。

    推論(serve)・点検(check)・取り込みの型の推奨(DX-08)が共有する判定。先頭が 0 の
    ``007`` や、JavaScript の数値で桁が落ちる 2**53 を超える整数は数値にしない(D7-03)。
    """
    bits = classify_value(value)
    if bits & _INT:
        return "int"
    if bits & _FLOAT:
        return "float"
    return None


def _default_importable() -> Callable[[str, ColumnType], bool]:
    """取り込みの値変換(services.coercion.coerce)で変換できるかを返す関数。"""
    from ..services.coercion import CoercionError, coerce

    def importable(value: str, logical: ColumnType) -> bool:
        try:
            coerce(value, logical)
        except (CoercionError, ValueError, TypeError):
            return False
        return True

    return importable


# ---------------------------------------------------------------------------
# 1 つの CSV の走査
# ---------------------------------------------------------------------------
@dataclass
class ColumnScan:
    """CSV の 1 列をデータから調べた結果。"""

    name: str
    #: CSV の行内での位置(見出しが重複した列は、取り込みと同じく後ろの列)。
    index: int
    #: すべての値を変換できる型の候補(ビット)。
    mask: int = _ALL
    #: 空でない値の数。
    non_empty: int = 0
    #: 型の候補を外した最初の値: ビット → (行番号, 値)。
    examples: dict[int, tuple[int, str]] = field(default_factory=dict)

    @property
    def type(self) -> ColumnType:
        """データから推論した型(空でない値が無ければ str)。"""
        if self.non_empty:
            for bit, logical in _PRIORITY:
                if self.mask & bit:
                    return logical
        return "str"

    def accepts(self, logical: ColumnType) -> bool:
        """すべての値を ``logical`` 型として取り込めるか。"""
        if logical == "str" or not self.non_empty:
            return True
        return bool(self.mask & _ACCEPT_BITS[logical])

    def counterexample(self, logical: ColumnType) -> tuple[int, str] | None:
        """``logical`` 型に変換できない最初の値(行番号, 値)。"""
        if self.accepts(logical):
            return None
        return self.examples.get(_ACCEPT_BITS[logical])


@dataclass
class CsvScan:
    """CSV 1 ファイルを調べた結果(見出し・列ごとの型・行数)。"""

    path: Path
    #: 読めた文字コード(``utf-8-sig`` は BOM の有無を問わない UTF-8)。
    encoding: str
    #: 見出し行(読んだままの値)。
    header: list[str]
    #: 列名(前後の空白を除いた見出し)→ 列の調査結果。見出しの順。
    columns: dict[str, ColumnScan]
    #: データ行の数(空行と、すべて空欄の行は数えない)。
    rows: int
    #: True なら先頭の ``SAMPLE_ROWS`` 行だけを調べた。
    sampled: bool
    notes: list[str] = field(default_factory=list)
    #: ``id`` 列(大文字小文字を問わない)の名前。
    id_column: str | None = None
    #: ``id`` 列を主キーにできない理由(使えるなら None)。
    id_problem: str | None = None


def _has_control(text: str) -> bool:
    """改行・タブ・NUL などの制御文字を含むか(外字などの私用領域の文字は含めない)。"""
    return any(unicodedata.category(ch) == "Cc" for ch in text)


def _plan_columns(header: list[str], notes: list[str]) -> list[tuple[str, int]]:
    """見出しから取り込む列 (名前, 位置) を決める。空・重複・制御文字の見出しは除く。"""
    positions: dict[str, int] = {}
    folded: dict[str, str] = {}
    empty = 0
    for index, raw in enumerate(header):
        # 取り込みと同じく、見出しの先頭に残った BOM(U+FEFF)は名前に含めない
        name = raw.replace("\ufeff", "").strip()
        if not name:
            empty += 1
            continue
        if _has_control(name):
            notes.append(
                f"見出し {name!r} は制御文字を含むため、この列は取り込みません"
            )
            continue
        if name in positions:
            # csv.DictReader と同じく、同じ見出しが重複したら後ろの列の値を使う
            positions[name] = index
            notes.append(
                f"見出し '{name}' が重複しています(後ろの列の値を取り込みます)"
            )
            continue
        key = name.casefold()
        if key in folded:
            notes.append(
                f"見出し '{name}' は '{folded[key]}' と大文字・小文字だけが違うため、"
                "この列は取り込みません"
            )
            continue
        folded[key] = name
        positions[name] = index
    if empty:
        notes.append(f"見出しが空の列({empty} 列)は取り込みません")
    return list(positions.items())


#: 型の候補を外した値を記録するビット(例として表示する)。
_EXAMPLE_BITS = (_INT, _FLOAT, _BOOL, _BOOLISH, _DATE, _DATETIME)


class _ColumnState:
    """1 列分の判定の途中経過(まとめて判定・1 値ずつの判定・id の検査)。"""

    def __init__(
        self,
        col: ColumnScan,
        importable: Callable[[str, ColumnType], bool],
        *,
        is_id: bool,
    ) -> None:
        self.col = col
        self.importable = importable
        self.is_id = is_id
        self.cache: dict[str, int] = {}
        self.shapes: dict[str, int] = {}
        # id 列の値(8 バイト整数の配列で省メモリに持つ)。値が増え続けている間は
        # 重複が無いと分かるため、集合を作るのは順序が崩れたときだけにする。
        self.ids = array("q")
        self.ids_increasing = True
        self.id_problem: str | None = None

    def feed(self, raw: Sequence[str], lines: Sequence[int]) -> None:
        """1 チャンク分の値(行の順)を判定に加える。"""
        col = self.col
        stripped = [v.strip() for v in raw]
        values = [v for v in stripped if v]
        col.non_empty += len(values)
        if self.is_id and self.id_problem is None:
            self._check_ids(stripped, values)
        if not col.mask or not values:
            return
        bits = self._chunk_bits(values)
        if bits is None:  # 混在しているチャンクは 1 値ずつ判定する
            for line, value in zip(lines, stripped):
                if value:
                    self._observe(value, self._value_bits(value), line)
                    if not col.mask:
                        break
            return
        lost = col.mask & ~bits
        if lost:
            self._record_examples(lost, stripped, lines)
            col.mask &= bits

    # --- まとめて判定 -------------------------------------------------------
    def _chunk_bits(self, values: list[str]) -> int | None:
        """チャンクの全値を 1 回の正規表現で判定する。判定できなければ None。"""
        mask = self.col.mask
        joined = "\n".join(values)
        if mask & (_INT | _FLOAT):
            if max(map(len, values)) > 15:
                return None  # 長い番号は 1 値ずつ(桁数の上限を確かめる)
            if mask & _INT and _INT_LINES.fullmatch(joined):
                bits = _INT | _FLOAT
                if mask & _BOOLISH and set(values) <= {"0", "1"}:
                    bits |= _BOOLISH
                return bits
            if _FLOAT_LINES.fullmatch(joined) and "e" not in joined.lower():
                return _FLOAT
            return None
        if mask & (_DATE | _DATETIME):
            if mask & _DATE and _DATE_LINES.fullmatch(joined):
                bits = _DATE | _DATETIME
            elif _DATETIME_LINES.fullmatch(joined):
                bits = _DATETIME
            else:
                return None
            if _MIXED_SEPARATORS.search(joined) or not self._calendar_ok(joined):
                return None  # 1 値ずつの判定に任せる(該当する値を弾き、反例を記録する)
            # 形式(数字を 9 にした形)ごとに、取り込めるかを確かめる
            shapes = dict(zip(joined.translate(_DIGITS_TO_NINE).split("\n"), values))
            for value in shapes.values():
                bits &= _shape_bits(value, bits, self.shapes, self.importable)
            return bits
        if mask & (_BOOL | _BOOLISH):
            tokens = {v.lower() for v in values}
            if tokens <= _BOOL_TOKENS:
                return _BOOL | _BOOLISH
            if tokens <= _BOOLISH_TOKENS:
                return _BOOLISH
        return None

    @staticmethod
    def _calendar_ok(joined: str) -> bool:
        """各値の年月日が実在する日付か(2 月 30 日などを弾く)。"""
        for year, month, day in set(_DATE_PARTS.findall(joined)):
            try:
                _dt.date(int(year), int(month), int(day))
            except ValueError:
                return False
        return True

    # --- 1 値ずつの判定 -----------------------------------------------------
    def _value_bits(self, value: str) -> int:
        bits = self.cache.get(value)
        if bits is None:
            bits = classify_value(value)
            if bits & (_DATE | _DATETIME):
                bits &= _shape_bits(value, bits, self.shapes, self.importable)
            if len(self.cache) < _CACHE_LIMIT:
                self.cache[value] = bits
        return bits

    def _observe(self, value: str, bits: int, line: int) -> None:
        col = self.col
        lost = col.mask & ~bits
        if lost:
            for bit in _EXAMPLE_BITS:
                if lost & bit and bit not in col.examples:
                    col.examples[bit] = (line, value)
            col.mask &= bits

    def _record_examples(
        self, lost: int, stripped: list[str], lines: Sequence[int]
    ) -> None:
        """まとめて判定で外れた型について、最初の反例(行番号, 値)を記録する。"""
        examples = self.col.examples
        pending = [b for b in _EXAMPLE_BITS if lost & b and b not in examples]
        for line, value in zip(lines, stripped):
            if not pending:
                return
            if not value:
                continue
            bits = self._value_bits(value)
            for bit in [b for b in pending if not bits & b]:
                examples[bit] = (line, value)
                pending.remove(bit)

    # --- id 列 --------------------------------------------------------------
    def _check_ids(self, stripped: list[str], values: list[str]) -> None:
        if len(values) != len(stripped):
            self.id_problem = "id 列に空欄があるため"
            return
        if not _INT_LINES.fullmatch("\n".join(values)):
            return  # 整数でない(型の判定で主キーにしない)
        if max(map(len, values)) > 18:
            self.id_problem = "id 列の値が大きすぎるため"
            return
        numbers = [int(v) for v in values]
        if self.ids_increasing and not (
            (not self.ids or numbers[0] > self.ids[-1])
            and numbers == sorted(set(numbers))
        ):
            self.ids_increasing = False
        self.ids.extend(numbers)

    def duplicate_id(self) -> str | None:
        """id 列で最初に重複した値(重複が無ければ None)。走査の最後に呼ぶ。"""
        if self.ids_increasing:
            return None
        seen: set[int] = set()
        for number in self.ids:
            if number in seen:
                return str(number)
            seen.add(number)
        return None


def header_problem(name: str, header: Sequence[str] | None) -> str | None:
    """見出しの行(1 行目)が CSV の見出しとして使えない理由(使えるなら None)。

    推論(serve)と点検(check --data)が共有する。``name`` はファイル名。
    """
    if header is None or not any(cell.strip() for cell in header):
        return f"{name}: 1 行目(見出し)がありません"
    if len(header) == 1:
        # 区切りがカンマでない(タブ区切り・; 区切り)と全体が 1 列になり、取り込んでも
        # 役に立たない(タブは制御文字のため「見出しが空」とだけ表示されてしまう)
        for sep, label in (("\t", "タブ"), (";", "セミコロン(;)")):
            if sep in header[0]:
                return (
                    f"{name}: 区切り文字が{label}のようです"
                    "(カンマ区切りの CSV で保存してください)"
                )
    return None


def header_looks_like_data(names: Iterable[str]) -> bool:
    """見出し(前後の空白を除いた空でない名前)がすべて数値・日付などの値か。"""
    names = [name for name in names if name]
    return bool(names) and all(classify_value(name) for name in names)


def _scan_reader(
    reader,
    path: Path,
    encoding: str,
    limit: int | None,
    importable: Callable[[str, ColumnType], bool],
) -> CsvScan:
    header = next(reader, None)
    problem = header_problem(path.name, header)
    if problem is not None:
        raise CsvReadError(problem)
    notes: list[str] = []
    plan = _plan_columns(header, notes)
    if not plan:
        reason = "・".join(notes) or "見出しが空です"
        raise CsvReadError(f"{path.name}: 取り込める列がありません({reason})")
    if header_looks_like_data(name for name, _index in plan):
        notes.append(
            "1 行目が見出しではなくデータのようです(1 行目に列名を書いてください)"
        )

    columns = [ColumnScan(name, index) for name, index in plan]
    id_pos = next(
        (i for i, col in enumerate(columns) if col.name.casefold() == "id"), None
    )
    states = [
        _ColumnState(col, importable, is_id=(pos == id_pos))
        for pos, col in enumerate(columns)
    ]
    width = max(col.index for col in columns) + 1
    padding = [""] * width

    rows = 0
    sampled = False
    logical = 1  # 見出しを 1 行目とした行番号(空行は数えない。取り込みの警告と同じ)
    chunk: list[list[str]] = []
    lines: list[int] = []

    def flush() -> None:
        if not chunk:
            return
        transposed = list(zip(*chunk))
        for state in states:
            state.feed(transposed[state.col.index], lines)
        chunk.clear()
        lines.clear()

    for row in reader:
        if not row:
            continue  # 空行(csv.DictReader と同じく飛ばす)
        logical += 1
        if not any(row):
            continue  # すべて空欄の行(Excel の ,,, の行)は取り込まれない
        if limit is not None and rows >= limit:
            sampled = True
            break
        rows += 1
        if len(row) < width:
            row = row + padding[len(row) :]  # 値の足りない行は空欄として扱う
        chunk.append(row)
        lines.append(logical)
        if len(chunk) >= _CHUNK_ROWS:
            flush()
    flush()

    scan = CsvScan(
        path=path,
        encoding=encoding,
        header=list(header),
        columns={col.name: col for col in columns},
        rows=rows,
        sampled=sampled,
        notes=notes,
    )
    if id_pos is not None:
        id_col = columns[id_pos]
        scan.id_column = id_col.name
        if rows == 0:
            pass  # データ行が無ければ、反する値の無い id 列を(整数の)主キーにする
        elif id_col.type != "int":
            scan.id_problem = "id 列が整数ではないため"
        elif states[id_pos].id_problem is not None:
            scan.id_problem = states[id_pos].id_problem
        elif (duplicate := states[id_pos].duplicate_id()) is not None:
            scan.id_problem = f"id 列の値 {duplicate} が重複しているため"
        elif sampled:
            scan.id_problem = "ファイルが大きく、id の重複を全行では確かめていないため"
    return scan


def _shape_bits(
    value: str,
    bits: int,
    shapes: dict[str, int],
    importable: Callable[[str, ColumnType], bool],
) -> int:
    """日付・日時の形式(数字を 9 に置き換えた形)ごとに、取り込めるかを 1 回だけ確かめる。"""
    shape = value.translate(_DIGITS_TO_NINE)
    allowed = shapes.get(shape)
    if allowed is None:
        allowed = _ALL
        if bits & _DATE and not importable(value, "date"):
            allowed &= ~_DATE
        if bits & _DATETIME and not importable(value, "datetime"):
            allowed &= ~_DATETIME
        shapes[shape] = allowed
    return allowed


def scan_csv(
    path: str | os.PathLike,
    encodings: Sequence[str] = DEFAULT_ENCODINGS,
    *,
    row_limit: int | None = None,
    importable: Callable[[str, ColumnType], bool] | None = None,
) -> CsvScan:
    """CSV を読み、列ごとの型をデータから調べる。

    ``encodings`` を順に試し、最後まで読めた文字コードの結果を返す。ファイルが
    ``FULL_SCAN_MAX_BYTES`` を超えるときは先頭 ``SAMPLE_ROWS`` 行だけを調べる
    (``row_limit`` で上書きできる)。読めなければ :class:`CsvReadError`。
    """
    path = Path(path)
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            head = fh.read(4)
    except OSError as exc:
        raise CsvReadError(f"{path.name}: 読み込めません({exc})") from exc
    if head.startswith((b"\xff\xfe", b"\xfe\xff")):
        raise CsvReadError(
            f"{path.name}: UTF-16 のファイルは読み込めません"
            "(Excel では「CSV UTF-8(コンマ区切り)」で保存してください)"
        )
    if row_limit is None and size > FULL_SCAN_MAX_BYTES:
        row_limit = SAMPLE_ROWS
    importable = importable or _default_importable()
    for encoding in encodings:
        try:
            with path.open(encoding=encoding, newline="") as fh:
                scan = _scan_reader(
                    csv.reader(fh), path, encoding, row_limit, importable
                )
        except UnicodeDecodeError:
            continue
        except LookupError as exc:
            raise CsvReadError(f"文字コード '{encoding}' は使えません") from exc
        except csv.Error as exc:
            raise CsvReadError(f"{path.name}: CSV として読めません({exc})") from exc
        if scan.sampled:
            scan.notes.append(
                f"ファイルが大きいため、先頭 {row_limit:,} 行のデータで型を判定しました"
            )
        return scan
    raise CsvReadError(
        f"{path.name}: 文字コードを判別できません(試した文字コード: "
        f"{', '.join(encodings)})。UTF-8 か Shift_JIS(cp932)で保存してください"
    )


# ---------------------------------------------------------------------------
# プロジェクト(CSV のフォルダ)全体の推論
# ---------------------------------------------------------------------------
@dataclass
class InferredFile:
    """CSV 1 ファイルから推論したテーブル。"""

    path: Path
    table: str
    encoding: str
    #: 列名 → 型(自動採番の主キーを足した場合は先頭)。
    columns: dict[str, ColumnType]
    primary_key: str
    #: True なら主キーは CSV に無い自動採番の列。
    synthetic_pk: bool
    rows: int
    sampled: bool
    chart_x: str | None = None
    chart_y: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def view_name(self) -> str:
        return f"{self.table}_view"

    @property
    def rows_label(self) -> str:
        """行数の表示(大きなファイルで先頭の行だけを調べたときは「以上」)。"""
        return f"{self.rows:,} 行以上" if self.sampled else f"{self.rows:,} 行"


@dataclass
class InferredProject:
    """推論した設定と、その元になった CSV。"""

    config: MonitorConfig
    #: CSV のフォルダ(取り込みとフォルダ監視の対象)。
    csv_dir: Path
    files: list[InferredFile]
    #: 読み込まなかったファイルなど、全体への注意。
    notes: list[str] = field(default_factory=list)


def _unsafe_table_reason(name: str) -> str | None:
    if not name:
        return "ファイル名が空の"
    if name.startswith("~$"):
        return "Excel などがファイルを開いている間に作る一時ファイルの"
    if name.startswith("."):
        return "隠しファイル(名前が . で始まる)の"
    if name != name.strip():
        return "ファイル名の前後に空白がある"
    unsafe = sorted({ch for ch in name if ch in _UNSAFE_NAME_CHARS})
    if unsafe:
        return f"ファイル名にテーブル名として使えない文字({' '.join(unsafe)})を含む"
    if _has_control(name):
        return "ファイル名に制御文字を含む"
    folded = name.casefold()
    if folded.startswith("sqlite_"):
        return "ファイル名が SQLite の予約名(sqlite_ で始まる)の"
    if folded in _INTERNAL_TABLES:
        return "ファイル名が monitor-app の内部テーブル名と同じ"
    return None


def _is_csv_file(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() == ".csv"


def _collect_files(
    paths: Sequence[str | os.PathLike],
) -> tuple[Path, list[Path]]:
    """引数(CSV ファイルかフォルダ)から、CSV のフォルダとファイルの一覧を得る。

    パスはシンボリックリンクを解決しない(絶対パスにするだけ)。取り込みとフォルダ
    監視は「CSV のフォルダ/<テーブル名>.csv」を読むため、リンク先の名前ではなく
    フォルダにある名前をテーブル名にする。
    """
    if not paths:
        raise InferError("CSV ファイルかフォルダを指定してください")
    files: list[Path] = []
    #: 実体のフォルダ → 指定された書き方のフォルダ(同じフォルダの別の書き方をまとめる)
    dirs: dict[Path, Path] = {}
    for raw in paths:
        path = Path(os.path.abspath(Path(raw).expanduser()))
        if path.is_dir():
            found = sorted(p for p in path.iterdir() if _is_csv_file(p))
            if not found:
                raise InferError(f"CSV ファイルが見つかりません: {path}")
            files.extend(found)
            dirs.setdefault(path.resolve(), path)
        elif path.is_file():
            files.append(path)
            dirs.setdefault(path.parent.resolve(), path.parent)
        else:
            raise InferError(f"ファイルまたはフォルダが見つかりません: {path}")
    if len(dirs) > 1:
        raise InferError(
            "CSV は 1 つのフォルダにまとめて指定してください"
            f"(指定されたフォルダ: {', '.join(sorted(str(d) for d in dirs.values()))})"
        )
    csv_dir = next(iter(dirs.values()))
    # 同じファイルを 2 回指定した(フォルダとファイルの両方など)ときは 1 つにする
    unique: dict[tuple[str, Path], Path] = {}
    for path in files:
        unique.setdefault((path.name, path.resolve()), csv_dir / path.name)
    return csv_dir, list(unique.values())


def _importer_finds(path: Path, csv_dir: Path) -> bool:
    """取り込み・フォルダ監視が探す ``<CSV のフォルダ>/<テーブル名>.csv`` がこのファイルか。"""
    expected = csv_dir / (path.stem + ".csv")
    try:
        return expected.exists() and os.path.samefile(expected, path)
    except OSError:  # pragma: no cover - 権限などで比べられない
        return False


def _synthetic_pk(names: Iterable[str]) -> str:
    taken = {name.casefold() for name in names}
    candidate, n = SYNTHETIC_PK, 2
    while candidate.casefold() in taken:
        candidate, n = f"{SYNTHETIC_PK}_{n}", n + 1
    return candidate


def _infer_file(path: Path, table: str, scan: CsvScan) -> InferredFile:
    notes = list(scan.notes)
    columns: dict[str, ColumnType] = {}
    if scan.id_column is not None and scan.id_problem is None:
        pk, synthetic = scan.id_column, False
    else:
        pk, synthetic = _synthetic_pk(scan.columns), True
        columns[pk] = "int"
        if scan.id_column is not None:
            notes.append(
                f"{scan.id_problem}、主キーは自動採番の {pk} にしました"
                f"({scan.id_column} は通常の列として取り込みます)"
            )
    if not scan.rows:
        notes.append(
            "データ行が無いため列の型を推論できません(主キー以外は str にしました。"
            "データを入れてから起動し直すと推論します)"
        )
    for name, col in scan.columns.items():
        # 主キーにした id 列は整数(データ行が無くても)
        columns[name] = "int" if name == pk else col.type

    time_cols = [n for n, t in columns.items() if t in ("date", "datetime")]
    x = time_cols[0] if time_cols else pk
    ys = [
        name
        for name, logical in columns.items()
        if logical in ("int", "float")
        and name not in (pk, x)
        and name.casefold() != "id"
        and not name.casefold().endswith("_id")
    ][:MAX_CHART_SERIES]
    return InferredFile(
        path=path,
        table=table,
        encoding=scan.encoding,
        columns=columns,
        primary_key=pk,
        synthetic_pk=synthetic,
        rows=scan.rows,
        sampled=scan.sampled,
        chart_x=x if ys else None,
        chart_y=ys,
        notes=notes,
    )


def quote_identifier(name: str) -> str:
    """SQL の識別子を必要なときだけ二重引用符で囲む(SQLAlchemy の DDL と同じ規則)。"""
    from sqlalchemy.dialects import sqlite

    return sqlite.dialect().identifier_preparer.quote(name)


def _project_title(csv_dir: Path) -> str:
    folder = Path(os.path.abspath(csv_dir))  # リンク先ではなく見えているフォルダ名
    name = folder.name
    if name.casefold() == "csv" and folder.parent.name:
        name = folder.parent.name  # ./csv ならプロジェクトのフォルダ名を使う
    return name or "Monitor App"


def _view_query(item: InferredFile, *, bounded: bool, tail_rows: int) -> str:
    table = quote_identifier(item.table)
    if not bounded:
        return f"SELECT * FROM {table}"
    pk = quote_identifier(item.primary_key)
    return (
        f"SELECT * FROM (SELECT * FROM {table} ORDER BY {pk} DESC LIMIT {tail_rows})"
        f" AS _tail ORDER BY {pk}"
    )


def build_config(
    files: Sequence[InferredFile],
    title: str,
    *,
    tail_rows: int = TAIL_ROWS,
    bound_all_views: bool = False,
) -> MonitorConfig:
    """推論したテーブルから MonitorConfig(テーブル・ビュー・グラフ・KPI)を組み立てる。"""
    tables: dict[str, TableDef] = {}
    views: dict[str, ViewDef] = {}
    kpis: dict[str, KpiCard] = {}
    for item in files:
        tables[item.table] = TableDef(
            columns=dict(item.columns), primary_key=item.primary_key
        )
        chart = None
        if item.chart_y and item.chart_x:
            chart = ChartDef(type="line", x=item.chart_x, y=list(item.chart_y))
        bounded = bound_all_views or item.rows > tail_rows
        views[item.view_name] = ViewDef(
            title=item.table,
            description=f"{item.path.name} から自動生成",
            query=_view_query(item, bounded=bounded, tail_rows=tail_rows),
            chart=chart,
        )
        kpis[f"{item.table}_count"] = KpiCard(
            title=f"{item.table} の件数",
            query=f"SELECT COUNT(*) FROM {quote_identifier(item.table)}",
            unit="件",
            link_view=item.view_name,
        )
    charts = [name for name, view in views.items() if view.chart is not None]
    return MonitorConfig(
        app_title=title,
        header_text=f"📊 {title}",
        tables=tables,
        views=views,
        kpis=kpis,
        # 多ければ先頭の数件だけにする(空ならグラフ付きの全ビュー)
        dashboard=DashboardConfig(
            columns=2,
            views=(
                charts[:MAX_DASHBOARD_CHARTS]
                if len(charts) > MAX_DASHBOARD_CHARTS
                else []
            ),
        ),
    )


def infer_project(
    paths: Sequence[str | os.PathLike],
    *,
    encodings: Sequence[str] = DEFAULT_ENCODINGS,
    tail_rows: int = TAIL_ROWS,
    bound_all_views: bool = False,
    importable: Callable[[str, ColumnType], bool] | None = None,
) -> InferredProject:
    """CSV ファイル(複数可・同じフォルダ)かフォルダから設定を推論する。

    使えないファイル(テーブル名にできない名前・読めない CSV)は飛ばして ``notes`` に
    理由を残す。1 つも使えなければ :class:`InferError`。``bound_all_views=True`` なら
    行数に関係なくビューを新しい順 ``tail_rows`` 行に絞る(追記で増え続ける CSV 向け)。
    """
    csv_dir, candidates = _collect_files(paths)
    importable = importable or _default_importable()
    notes: list[str] = []
    files: list[InferredFile] = []
    tables: dict[str, str] = {}
    for path in candidates:
        table = path.stem
        reason = _unsafe_table_reason(table)
        if reason:
            notes.append(f"{path.name}: {reason}ため読み込みません")
            continue
        if not _importer_finds(path, csv_dir):
            notes.append(
                f"{path.name}: 拡張子が .csv(小文字)ではないため読み込みません"
                f"({table}.csv に名前を変えてください)"
            )
            continue
        if table.casefold() in tables:
            notes.append(
                f"{path.name}: {tables[table.casefold()]} と大文字・小文字だけが違う"
                "名前のため読み込みません"
            )
            continue
        try:
            scan = scan_csv(path, encodings, importable=importable)
        except CsvReadError as exc:
            notes.append(str(exc))
            continue
        tables[table.casefold()] = path.name
        files.append(_infer_file(path, table, scan))
    if not files:
        detail = "".join(f"\n  - {note}" for note in notes)
        raise InferError(f"取り込める CSV がありません: {csv_dir}{detail}")
    config = build_config(
        files,
        _project_title(csv_dir),
        tail_rows=tail_rows,
        bound_all_views=bound_all_views,
    )
    if config.dashboard.views:
        charts = sum(1 for view in config.views.values() if view.chart is not None)
        notes.append(
            f"グラフ付きのビューが {charts} 件あるため、トップページには最初の"
            f" {MAX_DASHBOARD_CHARTS} 件を並べます(ほかは一覧から開けます。"
            "config.py の dashboard.views で変えられます)"
        )
    return InferredProject(config=config, csv_dir=csv_dir, files=files, notes=notes)


# ---------------------------------------------------------------------------
# config.py への書き出し
# ---------------------------------------------------------------------------
def _lit(value: str) -> str:
    """Python の文字列リテラル(black と同じく、なるべく二重引用符)。"""
    if '"' in value and "'" not in value:
        return repr(value)
    return json.dumps(value, ensure_ascii=False)


def _comment_text(text: str) -> str:
    """コメントに書く文字列(改行などの制御文字は ? に置き換える)。"""
    return "".join("?" if _has_control(ch) else ch for ch in text)


def _encoding_label(encoding: str) -> str:
    from ..services.csv_encoding import label

    return label(encoding)


def render_config_py(project: InferredProject, *, in_project: bool = False) -> str:
    """推論結果を、そのまま使える読みやすい ``config.py`` の文字列にする。

    列は型を明示した辞書で書く(列名からの型推論に頼らない)。書き出した config.py は
    ``monitor-app check`` を通り、読み込むと ``project.config`` と同じ設定になる。
    ``in_project=True`` は、CSV をプロジェクトの ``csv/`` に置く場合(``startproject
    --from-csv``)の説明にする(MONITOR_CSV_DIR は不要)。
    """
    cfg = project.config
    by_table = {item.table: item for item in project.files}
    csv_dir = _comment_text(str(project.csv_dir))
    names = ["KpiCard", "MonitorConfig", "TableDef", "ViewDef", "DashboardConfig"]
    if any(view.chart for view in cfg.views.values()):
        names.append("ChartDef")
    generator = "startproject --from-csv" if in_project else "serve"
    out = [
        f'"""monitor-app の設定ファイル(monitor-app {generator} が CSV から自動生成)。',
        "",
        "列の型は CSV のデータから推論した。必要に応じて書き換えてよい",
        '("int" / "float" / "str" / "bool" / "date" / "datetime")。',
        '"""',
        "",
    ]
    if in_project:
        out += [
            "# CSV はこのフォルダの csv/ に置く(ファイル名がテーブル名: <テーブル名>.csv)。",
            "# 起動: monitor-app runserver --import-csv-if-empty",
        ]
    else:
        out += [
            f"# CSV のフォルダ: {csv_dir}",
            "# この設定で起動するには、.env に CSV のフォルダを書いてから runserver を使う:",
            f"#   MONITOR_CSV_DIR={csv_dir}",
            "#   monitor-app runserver --config <この config.py> --import-csv-if-empty",
        ]
    out += [
        "",
        f"from monitor_app import {', '.join(sorted(names))}",
        "",
        "config = MonitorConfig(",
        f"    app_title={_lit(cfg.app_title)},",
        f"    header_text={_lit(cfg.header_text)},",
        "    tables={",
    ]
    for name, tdef in cfg.tables.items():
        item = by_table.get(name)
        if item is not None:
            detail = f"{_encoding_label(item.encoding)}・{item.rows_label}"
            out.append(f"        # {_comment_text(item.path.name)}({detail})")
            if item.synthetic_pk:
                out.append(
                    f"        # 主キー {_comment_text(item.primary_key)} は自動採番"
                    "(CSV に使える id 列が無いため)"
                )
        out.append(f"        {_lit(name)}: TableDef(")
        out.append("            columns={")
        columns = tdef.columns if isinstance(tdef.columns, dict) else {}
        for col, logical in columns.items():
            out.append(f"                {_lit(col)}: {_lit(logical)},")
        out.append("            },")
        out.append(f"            primary_key={_lit(tdef.primary_key)},")
        out.append("        ),")
    out.append("    },")
    out.append("    views={")
    for name, view in cfg.views.items():
        out.append(f"        {_lit(name)}: ViewDef(")
        out.append(f"            title={_lit(view.title)},")
        if view.description:
            out.append(f"            description={_lit(view.description)},")
        out.append(f"            query={_lit(view.query)},")
        if view.chart is not None:
            ys = ", ".join(_lit(y) for y in view.chart.y_columns)
            out.append("            chart=ChartDef(")
            out.append(f"                type={_lit(view.chart.type)},")
            out.append(f"                x={_lit(view.chart.x)},")
            out.append(f"                y=[{ys}],")
            out.append("            ),")
        out.append("        ),")
    out.append("    },")
    out.append("    kpis={")
    for name, card in cfg.kpis.items():
        out.append(f"        {_lit(name)}: KpiCard(")
        out.append(f"            title={_lit(card.title)},")
        out.append(f"            query={_lit(card.query)},")
        out.append(f"            unit={_lit(card.unit)},")
        if card.link_view:
            out.append(f"            link_view={_lit(card.link_view)},")
        out.append("        ),")
    out.append("    },")
    if cfg.dashboard.views:
        shown = ", ".join(_lit(name) for name in cfg.dashboard.views)
        out.append(
            f"    dashboard=DashboardConfig(columns={cfg.dashboard.columns},"
            f" views=[{shown}]),"
        )
    else:
        out.append(f"    dashboard=DashboardConfig(columns={cfg.dashboard.columns}),")
    out.append(")")
    return "\n".join(out) + "\n"
