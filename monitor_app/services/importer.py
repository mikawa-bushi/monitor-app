"""CSV → DB インポート。標準ライブラリ ``csv`` を使い、スキーマの真実を
``config.py`` 側(:class:`TableDef`)に一本化する。

pandas の ``to_sql(if_exists="replace")`` のようにデータ側の推論でスキーマを
作り直さないため、宣言した型・外部キーが取り込みで失われない。

- 取り込みは 1 行ずつ変換して ``INSERT_CHUNK`` 行ごとに書き込む(ファイル全体を
  メモリに載せない — PERF-04)。全テーブルを 1 トランザクションで取り込み、途中で
  失敗すれば何も変わらない。
- モード: ``replace``(置換)/ ``append``(追記。主キーが既にある行は読み飛ばす)/
  ``if-empty``(行が 1 件も無いテーブルにだけ取り込む)。
- ヘッダを config.py の列と突き合わせ、宣言した列が丸ごと無い CSV では置換しない
  (CRIT-03)。
- 文字コード ``auto`` は UTF-8(BOM の有無は問わない)で読み、読めなければ
  ``encoding_fallback`` の文字コード(既定: cp932)で読み直す(DX-04)。UTF-8 として
  読めている CSV に壊れたバイトが混じっただけなら、その行だけを行番号つきの警告を
  残して読み飛ばす(D3-01, D3-02)。
- 行の中の NUL(電源断で末尾が 0 埋めになった SD カードなど)は取り除き、CSV として
  読めない行は読み飛ばす(D3-09)。PostgreSQL / MySQL の列に入らない値(長すぎる文字列・
  範囲外の整数)の行も同じく読み飛ばす(D3-06)。
- 追記監視は前回の続き(バイト位置)から完全な行だけを読む(:meth:`CsvImporter.tail_append`
  — PERF-05, PERF-06)。読み取り位置は取り込んだ行と同じトランザクションで DB に保存し、
  再起動後もその続きから読む(停止中に追記された行も取りこぼさない — D3-03)。
"""

from __future__ import annotations

import codecs
import csv
import difflib
import logging
import os
import re
import threading
from array import array
from bisect import bisect_left
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from itertools import islice
from operator import methodcaller
from pathlib import Path
from typing import Any

from sqlalchemy import (
    Column,
    MetaData,
    Table,
    bindparam,
    delete,
    insert,
    inspect,
    or_,
    select,
    update,
)
from sqlalchemy.exc import DBAPIError

from ..cancel import stop_scope
from ..db import errors as db_errors
from ..db.bulk import insert_rows
from ..db.engine import Database
from ..db.errors import CONFLICT, classify
from ..db.registry import TableRegistry
from ..exceptions import (
    ConflictError,
    CsvEncodingError,
    CsvFormatError,
    InvalidPayloadError,
    MonitorAppError,
)
from ..settings.declarative import MonitorConfig
from ..settings.infer import numeric_kind
from . import csv_encoding
from .coercion import CoercionError
from .csv_tail import (  # noqa: F401 - 従来の名前で import できるように
    _EOL,
    _SENTINEL,
    _TAIL_BLOCK,
    _WINDOW,
    TailState,
    _BadRow,
    _complete_prefix,
    _count_lines,
    _first_record_end,
    _parse_complete,
    _parse_complete_safe,
    _read_at,
    _scan_complete,
    _unparsable_message,
    _whole_lines,
    parse_tail_cursor,
    restore_windows,
    tail_cursor,
)
from .rows import HeaderPlan, RowConverter, normalize_name
from .value_limits import ValueLimits, declared_limits, reflected_limits

logger = logging.getLogger("monitor_app.importer")

#: 一括 INSERT を分割する行数(#V5)。1 文へ全行を束ねるとパラメータ上限
#: (SQLite は約 999/32766 など)に達したり、巨大な文でメモリを圧迫するため。
INSERT_CHUNK = 1000

#: 取り込みモード(:meth:`CsvImporter.import_all` の ``mode``)。
IMPORT_MODES = ("replace", "append", "if-empty")

#: 文字コードの判別に読むファイル先頭のバイト数。
_SNIFF_BYTES = 64 * 1024
#: 追記監視でバイト位置から読める文字コード(従来の名前。:mod:`.csv_encoding` を参照)。
_TAILABLE_CODECS = csv_encoding.TAILABLE_CODECS
#: 列名から str と推論した列が数値ばかりか(DX-08)を確かめる値の数(列ごと)。
_TYPE_HINT_SAMPLE = 10_000
#: 取り込み警告に並べる行番号の最大数。
_MAX_LISTED_LINES = 10
#: 警告に載せる型変換のエラー文の最大の長さ(長い値をそのまま並べない — D3-09)。
_MAX_MESSAGE = 200
#: 行から NUL を取り除く(電源断で 0 埋めになった部分。行の区切りは変わらない)。
_STRIP_NUL = methodcaller("replace", "\x00", "")
#: 追記監視の読み取り位置を保存する ``_ingest_state`` のキーの接頭辞。
TAIL_KEY_PREFIX = "csv:"
#: 先頭が 0 の数値(int / float として取り込むと 0 が落ちる)。
_LEADING_ZERO = re.compile(r"[+-]?0[0-9]")
#: 取り込み結果(TableImportResult・ImportReport)にも同じ内容が入るログの印。
#: ``import-csv`` は結果の一覧に表示するため、このログを出さない(D5-06)。
IN_RESULT = {"monitor_in_result": True}


# 従来の名前(:mod:`.csv_encoding` に移した)。
_split_encodings = csv_encoding.split_encodings
_codec_name = csv_encoding.codec_name


def insert_in_chunks(conn, table, rows, *, on_conflict: str = "error") -> int:
    """``rows`` を ``INSERT_CHUNK`` 件ずつに分けて挿入し、新しく入った行数を返す(#V5)。

    列の組が行ごとに違っても、列を落とさずに入れる(:func:`insert_rows`)。``rows`` は
    反復子でもよい(``INSERT_CHUNK`` 行ずつしか保持しない)。``on_conflict="ignore"`` は
    主キーが既にある行を読み飛ばす(追記)。
    """
    return insert_rows(conn, table, rows, on_conflict=on_conflict, chunk=INSERT_CHUNK)


def duplicates_message(name: str, count: int) -> str:
    """追記で主キーが既にあった行の知らせ(取り込み結果の警告に入る文)。"""
    return f"{name}: 主キーが既にある {count} 行を読み飛ばしました(追記)"


@dataclass
class TableImportResult:
    table: str
    inserted: int = 0
    skipped: int = 0
    warnings: list[str] = field(default_factory=list)
    #: 追記で主キーが既にあったため読み飛ばした行数。
    duplicates: int = 0
    #: ヘッダに宣言済みの列が無いため置換しなかった(既存の行はそのまま — CRIT-03)。
    refused: bool = False
    #: 実際に読んだ文字コード(CSV が空のときなどは None)。
    encoding: str | None = None
    #: ヘッダにある config.py の列 / 無い列 / config.py に無いヘッダの列。
    matched: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    #: 表記ゆれから対応付けたヘッダの列 → config.py の列(例: ``"Name" → "name"``)。
    renamed: dict[str, str] = field(default_factory=dict)


@dataclass
class UnmatchedFile:
    """csv/ フォルダにあるが、どのテーブルにも対応しない CSV ファイル。"""

    name: str
    #: 大文字小文字・全角半角の違いだけで名前が一致するテーブル(あれば)。
    suggestion: str | None = None

    @property
    def message(self) -> str:
        if self.suggestion:
            return (
                f"{self.name}: 対応するテーブルがありません(テーブル "
                f"'{self.suggestion}' のつもりならファイル名を "
                f"{self.suggestion}.csv にしてください)"
            )
        return (
            f"{self.name}: 対応するテーブルがありません(ファイル名から拡張子を除いた"
            "部分が config.py の tables の名前と一致するファイルを取り込みます)"
        )


@dataclass
class ImportReport:
    results: list[TableImportResult] = field(default_factory=list)
    #: どのテーブルにも対応しない CSV ファイル(CRIT-03)。
    unmatched_files: list[UnmatchedFile] = field(default_factory=list)
    #: ``if-empty`` で、行が既にあるため取り込まなかったテーブル。
    skipped_tables: list[str] = field(default_factory=list)

    @property
    def total_inserted(self) -> int:
        return sum(r.inserted for r in self.results)

    @property
    def total_skipped(self) -> int:
        return sum(r.skipped for r in self.results)

    @property
    def total_duplicates(self) -> int:
        return sum(r.duplicates for r in self.results)


@dataclass
class CsvFileCheck:
    """取り込みと同じ読み方で CSV を 1 つ調べた結果(``monitor-app check --data`` 用)。

    行番号は取り込みの警告と同じ数え方(見出しが 1 行目。空行は数えない)。値の例は
    (行番号, CSV のセルの値)。
    """

    table: str
    path: Path
    #: 取り込みが使う文字コード(見出しが無いときなどは None)。
    encoding: str | None = None
    #: ``auto`` で UTF-8 以外を使う。
    fallback: bool = False
    plan: HeaderPlan | None = None
    #: 取り込まれる行の数。
    rows: int = 0
    #: True なら先頭の ``limit`` 行だけを調べた。
    sampled: bool = False
    #: 列 → 宣言した型に変換できない最初の値。
    failures: dict[str, tuple[int, str]] = field(default_factory=dict)
    #: int / float の列 → 先頭の 0 が落ちる最初の値(``007`` など)。
    leading_zeros: dict[str, tuple[int, str]] = field(default_factory=dict)
    #: 列 → DB の列に入らない最初の値と理由(PostgreSQL / MySQL — D3-06)。
    too_large: dict[str, tuple[int, str]] = field(default_factory=dict)
    #: 列名から str と推論した列のうち、値がすべて数値の列 → 勧める型(DX-08)。
    numeric: dict[str, str] = field(default_factory=dict)
    #: CSV の中で重複した主キー(値, 最初の行, 重複した行)。
    duplicate_key: tuple[Any, int, int] | None = None
    #: 文字コードとして読めないバイトを含む行 / CSV として読めない行の行番号。
    undecodable: list[int] = field(default_factory=list)
    unparsable: list[int] = field(default_factory=list)
    #: 値がすべて空のため取り込まれない行の数。
    blank: int = 0


class _NoValidRows(Exception):
    """置換で有効行が 0 件だった(既存の行を残すためトランザクションを取り消す)。"""

    def __init__(self, source: _Source) -> None:
        super().__init__(source.table)
        self.source = source


class _PlanMismatch(Exception):
    """別の文字コードで読み直したらヘッダの対応が変わった(その文字コードは使わない)。"""


class _MostlyUndecodable(Exception):
    """読めない行を飛ばして読んだが、読めた ASCII 以外の行の方が多くなかった
    (その文字コードのファイルではない)。"""


class _SourceDbError(Exception):
    """1 つの CSV を書き込む途中で DB がエラーを返した(どの CSV の何行目付近かを持つ)。"""

    def __init__(
        self,
        source: _Source,
        error: DBAPIError,
        encoding: str,
        lossy: bool,
        produced: int | None,
    ) -> None:
        super().__init__(str(error))
        self.source = source
        self.error = error
        self.encoding = encoding
        self.lossy = lossy
        #: エラーまでに渡した行の数(書き込みの途中でなければ None)。
        self.produced = produced


class _CheckedPlan:
    """読めなかった行を、型変換の失敗(行番号つきの警告を残して読み飛ばす)にする変換。

    ``stats`` を渡すと、セルの中の読めないバイト(``surrogateescape`` の代用文字)も
    調べ、読めた ASCII 以外の行・読めなかった行を数える(読めない行だけを飛ばして読む
    ときだけ使う。ふだんの取り込みには挟まない)。
    """

    def __init__(self, plan: Any, encoding: str, stats: _Stats | None = None) -> None:
        self._convert = plan.convert
        self._stats = stats
        self._message = _undecodable_message(encoding)

    def convert(self, raw: Sequence[str]) -> dict[str, Any]:
        if type(raw) is _BadRow:
            raise CoercionError(raw.message)
        stats = self._stats
        if stats is not None:
            non_ascii = False
            for cell in raw:
                if not cell.isascii():
                    if csv_encoding.has_escaped(cell):
                        stats.bad += 1
                        raise CoercionError(self._message)
                    non_ascii = True
            if non_ascii:
                stats.good += 1
        return self._convert(raw)


class _GuardedPlan:
    """変換した行が DB の列の制限に収まるかを確かめる変換(D3-06)。"""

    def __init__(self, plan: Any, limits: ValueLimits) -> None:
        self._convert = plan.convert
        self._limits = limits

    def convert(self, raw: Sequence[str]) -> dict[str, Any]:
        return self._limits(self._convert(raw))


@dataclass
class _Stats:
    """1 回の読み取りの集計(スキップ数・警告・行番号)。"""

    skipped: int = 0
    rows: int = 0
    warnings: list[str] = field(default_factory=list)
    blank_lines: list[int] = field(default_factory=list)
    #: 直前に読んだ行の行番号(ヘッダが 1 行目。空行は数えない)。
    line: int = 1
    hint: _NumericHint | None = None
    #: 読めない行を飛ばして読んだときの、読めた ASCII 以外の行 / 読めなかった行の数。
    good: int = 0
    bad: int = 0
    #: すべての行を書き込み終えた(DB のエラーが行の書き込み中か後かを見分ける)。
    written: bool = False


@dataclass
class _Source:
    """取り込む 1 つの CSV(テーブル)の状態。"""

    table: str
    path: Path
    result: TableImportResult
    #: 試す文字コード(判別できたものから順に)。
    encodings: list[str] = field(default_factory=list)
    plan: HeaderPlan | None = None
    header_warnings: list[str] = field(default_factory=list)
    #: 型の推奨(DX-08)。やり直しても残す(同じ警告は 1 度しか作らないため)。
    notes: list[str] = field(default_factory=list)
    #: 置換で有効行が 0 件のため、既存の行を残す(#16)。
    kept: bool = False
    #: ヘッダの検査で置換を拒否した(CRIT-03)。
    refused: bool = False
    #: ``if-empty`` で、行が既にあるため取り込まない。
    not_empty: bool = False
    #: 行を書き込んだ(置換の削除を含む)。
    wrote: bool = False

    @property
    def name(self) -> str:
        return self.path.name


class _KeyTracker:
    """CSV の中で重複した主キーを探す(重複したら最初の行番号を返す)。

    昇順に並んだ整数の主キー(よくある CSV)は 8 バイト整数の配列で覚え、並びが崩れた
    分だけを辞書で覚える(大きな CSV でもメモリを使いすぎない)。
    """

    _LIMIT = 2**63

    def __init__(self) -> None:
        self.keys = array("q")
        self.lines = array("q")
        self.others: dict[Any, int] = {}

    def first_line(self, key: Any, line: int) -> int | None:
        """``key`` が既に出ていればその行番号、初めてなら覚えて None。"""
        keys = self.keys
        if type(key) is int and -self._LIMIT <= key < self._LIMIT:
            if not keys or key > keys[-1]:
                # 覚えたどの主キーより大きい(並びが崩れた分もそれより小さい)
                keys.append(key)
                self.lines.append(line)
                return None
            i = bisect_left(keys, key)
            if i < len(keys) and keys[i] == key:
                return self.lines[i]
        first = self.others.setdefault(key, line)
        return first if first != line else None


class _NumericHint:
    """列名から str と推論した列の値が数値ばかりかを見る(DX-08)。

    列ごとに空でない値を最大 ``limit`` 個まで調べ、数値でない値が 1 つでもあれば
    その列は対象から外す(先頭の数行で外れるため、通常は負荷にならない)。数値かどうかは
    ``serve`` の推論・``check`` と同じ判定(:func:`~monitor_app.settings.infer.numeric_kind`。
    先頭が 0 の ``007`` や 2**53 を超える長い番号は数値にしない — D7-03)。
    """

    def __init__(self, columns: Sequence[str], limit: int = _TYPE_HINT_SAMPLE) -> None:
        #: 列 → [数値の個数, すべて整数か]
        self.pending: dict[str, list[Any]] = {col: [0, True] for col in columns}
        self.numeric: dict[str, str] = {}
        self.limit = limit
        self.active = bool(self.pending)

    def observe(self, record: dict[str, Any]) -> None:
        decided: list[tuple[str, bool]] = []
        for col, state in self.pending.items():
            value = record.get(col)
            if value is None or type(value) is not str:
                continue
            text = value.strip()
            if not text:
                continue
            kind = numeric_kind(text)
            if kind is None:
                decided.append((col, False))
                continue
            state[0] += 1
            if state[1] and kind != "int":
                state[1] = False
            if state[0] >= self.limit:
                decided.append((col, True))
        for col, numeric in decided:
            _count, integral = self.pending.pop(col)
            if numeric:
                self.numeric[col] = "int" if integral else "float"
        self.active = bool(self.pending)

    def finish(self) -> dict[str, str]:
        """空でない値がすべて数値だった列 → 勧める型(int / float)。"""
        for col, (count, integral) in self.pending.items():
            if count:
                self.numeric[col] = "int" if integral else "float"
        self.pending = {}
        self.active = False
        return self.numeric


def _resolve_mode(keep: bool, mode: str | None) -> str:
    if mode is None:
        return "append" if keep else "replace"
    if mode not in IMPORT_MODES:
        raise ValueError(f"mode は {', '.join(IMPORT_MODES)} のいずれかです: {mode!r}")
    if keep and mode != "append":
        raise ValueError(
            "keep=True は mode='append' と同じです(ほかの mode と併用できません)"
        )
    return mode


def _same_file(a: Path, b: Path) -> bool:
    try:
        return b.exists() and os.path.samefile(a, b)
    except OSError:
        return False


def _line_list(lines: Sequence[int]) -> str:
    """``3, 4, 6 行目`` / ``3, 4, …, 12 行目ほか 15 行`` の形の行番号の並び。"""
    shown = ", ".join(str(n) for n in lines[:_MAX_LISTED_LINES]) + " 行目"
    if len(lines) > _MAX_LISTED_LINES:
        shown += f"ほか {len(lines) - _MAX_LISTED_LINES} 行"
    return shown


def _clip(message: str, limit: int = _MAX_MESSAGE) -> str:
    """長いエラー文の真ん中を省く(長い値を警告にそのまま並べない — D3-09)。"""
    if len(message) <= limit:
        return message
    half = limit // 2
    return message[:half] + " … " + message[-half:]


def _undecodable_message(encoding: str) -> str:
    return (
        f"文字コード {csv_encoding.label(encoding)} として読めないバイトを含むため、"
        "この行は取り込みません"
    )


class CsvImporter:
    """csv/ フォルダの CSV をテーブルへ取り込む。

    ``encoding`` は ``"auto"``(既定。UTF-8 で読み、読めなければ ``encoding_fallback``
    の文字コードを順に試す)か、``"utf-8-sig"`` / ``"cp932"`` などの固定の文字コード。
    ``on_change`` はデータが変わる書き込みを確定した後に呼ぶ(ビューのキャッシュを
    捨てるなど)。
    """

    def __init__(
        self,
        config: MonitorConfig,
        registry: TableRegistry,
        db: Database,
        csv_dir: Path,
        encoding: str = "auto",
        *,
        encoding_fallback: str | Sequence[str] | None = "cp932",
        on_change: Callable[[], None] | None = None,
    ) -> None:
        self.config = config
        self.registry = registry
        self.db = db
        self.csv_dir = Path(csv_dir)
        self.encoding = encoding
        self.encoding_fallback: list[str] = _split_encodings(encoding_fallback)
        self.on_change = on_change
        self._converters: dict[str, RowConverter] = {}
        #: 一度だけ出す警告の記録(文字コードの判別、DX-08 の型の推奨)。
        self._warned: set = set()
        #: テーブル → DB の列の制限(サーバー DB だけ。DB の列の型から 1 度だけ作る)。
        self._limits: dict[str, ValueLimits | None] = {}
        self._tail_store = None

    # ------------------------------------------------------------------
    # 一括取り込み
    # ------------------------------------------------------------------
    def import_all(
        self, *, keep: bool = False, mode: str | None = None, force: bool = False
    ) -> ImportReport:
        """``csv_dir`` 内の各 CSV を、対応するテーブルへ取り込む。

        CSV のファイル名(拡張子なし)がテーブル名。``mode`` は ``"replace"``(既定。
        取り込み前に既存行を全削除)/ ``"append"``(追記。主キーが既にある行は読み
        飛ばし、テーブルごとの ``duplicates`` に数える)/ ``"if-empty"``(行が 1 件も
        無いテーブルにだけ取り込む)。``keep=True`` は ``mode="append"`` と同じ。

        全テーブルを 1 トランザクションで取り込む(途中で失敗すれば何も変わらない)。
        外部キーを強制する DB(PostgreSQL / MySQL)でも置換できるよう、削除は子の
        テーブルから、挿入は親のテーブルから行う(CRIT-01)。置換では、有効行が 0 件の
        CSV(#16)と、宣言した主キー以外の列がヘッダに丸ごと無い CSV(CRIT-03、
        ``force=True`` で強制)のテーブルは既存の行を残す。
        """
        mode = _resolve_mode(keep, mode)
        self.registry.create_all(self.db)
        report = ImportReport(unmatched_files=self.unmatched_files())
        for unmatched in report.unmatched_files:
            logger.warning("%s", unmatched.message, extra=IN_RESULT)
        sources = []
        for table_name in self.config.tables:
            csv_path = self.csv_dir / f"{table_name}.csv"
            if csv_path.exists():
                sources.append(
                    _Source(table_name, csv_path, TableImportResult(table=table_name))
                )
        changed = self._run(sources, mode, force)
        for src in sources:
            if src.not_empty:
                report.skipped_tables.append(src.table)
                continue
            report.results.append(src.result)
            if src.wrote:
                self._log_imported(src.result)
        if changed:
            self._notify_change()
        return report

    def import_table(
        self,
        table_name: str,
        csv_path: Path,
        *,
        keep: bool = False,
        mode: str | None = None,
        force: bool = False,
    ) -> TableImportResult:
        """1 テーブル分の CSV を取り込む(置換 or 追記)。

        ``keep=False``(置換)でも、有効行が 0 件のときは既存行を削除しない
        (空・破損 CSV を誤って置いたときの全消去を防ぐ — #16)。``mode`` と ``force`` は
        :meth:`import_all` と同じ。
        """
        mode = _resolve_mode(keep, mode)
        src = _Source(table_name, Path(csv_path), TableImportResult(table=table_name))
        changed = self._run([src], mode, force)
        if src.wrote:
            self._log_imported(src.result)
        if changed:
            self._notify_change()
        return src.result

    def import_tables(
        self,
        table_names: Iterable[str],
        *,
        keep: bool = False,
        mode: str | None = None,
        force: bool = False,
    ) -> list[TableImportResult]:
        """指定したテーブルの CSV(``csv_dir/<テーブル名>.csv``)だけを 1 トランザクションで
        取り込む。CSV の無いテーブルは飛ばす。``keep``・``mode``・``force`` は
        :meth:`import_all` と同じ。

        フォルダ監視(置換)で同時に変わった CSV をまとめて置き換えるのに使う。外部キーで
        参照し合うテーブルの CSV を揃えて差し替えたとき、1 つずつ置き換えると
        PostgreSQL / MySQL では途中の状態が外部キーに違反して失敗するため。
        """
        mode = _resolve_mode(keep, mode)
        sources = []
        for name in dict.fromkeys(table_names):
            if name not in self.config.tables:
                raise KeyError(name)
            path = self.csv_dir / f"{name}.csv"
            if path.exists():
                sources.append(_Source(name, path, TableImportResult(table=name)))
        changed = self._run(sources, mode, force)
        results = []
        for src in sources:
            if src.not_empty:
                continue
            results.append(src.result)
            if src.wrote:
                self._log_imported(src.result)
        if changed:
            self._notify_change()
        return results

    def dependency_order(self) -> list[str]:
        """外部キーの参照先(親)が先に来るテーブル名の並び(設定の順を保つ)。

        外部キーを強制する DB では、この順に挿入し、逆順に削除する。自己参照は依存に
        数えず、循環参照は設定の順で 1 つずつほどく。
        """
        deps = {
            name: {
                fk.column.table.name
                for fk in self.registry.get(name).foreign_keys
                if fk.column.table.name != name
                and fk.column.table.name in self.config.tables
            }
            for name in self.config.tables
        }
        ordered: list[str] = []
        remaining = list(self.config.tables)
        while remaining:
            ready = [n for n in remaining if deps[n] <= set(ordered)]
            if not ready:  # 循環参照: 残りは設定の順のまま
                ready = remaining[:1]
            for name in ready:
                ordered.append(name)
                remaining.remove(name)
        return ordered

    def unmatched_files(self) -> list[UnmatchedFile]:
        """``csv_dir`` の CSV のうち、どのテーブルにも対応しないもの(CRIT-03)。"""
        try:
            entries = sorted(
                p
                for p in self.csv_dir.iterdir()
                if p.suffix.lower() == ".csv" and p.is_file()
            )
        except OSError:
            return []
        tables = list(self.config.tables)
        by_norm = {normalize_name(t): t for t in tables}
        unmatched = []
        for path in entries:
            if path.suffix == ".csv" and path.stem in self.config.tables:
                continue
            # 大文字小文字を区別しないファイルシステム(Windows・macOS)では
            # users.csv として開けるため、取り込まれている。
            if any(_same_file(path, self.csv_dir / f"{t}.csv") for t in tables):
                continue
            unmatched.append(
                UnmatchedFile(path.name, by_norm.get(normalize_name(path.stem)))
            )
        return unmatched

    # ------------------------------------------------------------------
    # 読み取り(従来の API。取り込みと同じ規則で、行をリストで返す)
    # ------------------------------------------------------------------
    def read_rows(
        self, table_name: str, csv_path: Path
    ) -> tuple[list[dict[str, object]], int, list[str]]:
        """CSV を読み、設定列に絞って論理型へ変換した行リストを返す。

        戻り値は ``(rows, skipped, warnings)``。未知列は破棄。型変換に失敗した
        行と、値がすべて空の行(Excel が書き出すカンマだけの行)はスキップして
        警告に積む。
        """
        path = Path(csv_path)
        src = _Source(table_name, path, TableImportResult(table=table_name))
        src.encodings, _header = self._sniff(path)

        def attempt(encoding: str, _expect: HeaderPlan | None, lossy: bool):
            stats = _Stats()
            records = self._records(table_name, path, encoding, stats, lossy=lossy)
            try:
                rows = list(records)
            finally:
                records.close()
            if lossy and stats.good <= stats.bad:
                raise _MostlyUndecodable()
            return rows, stats

        rows, stats = self._read_with_fallback(src, attempt)
        return rows, stats.skipped, self._row_warnings(path, stats)

    def append_since(
        self, table_name: str, csv_path: Path, *, start_row: int
    ) -> tuple[int, int]:
        """ファイル全体を読み、既読 ``start_row`` 行を超えた分のみ挿入する(#9)。

        戻り値は ``(inserted, total_valid_rows)``。ファイル縮小・差し替え時
        (``start_row > total``)は先頭から取り込み直す。主キーが既にある行は読み
        飛ばす。追記監視は通常 :meth:`tail_append` を使い、これは追記の読み取りに
        向かない文字コード(UTF-16 など)のときだけ使う。
        """
        rows, _skipped, warnings = self.read_rows(table_name, csv_path)
        for w in warnings:
            logger.warning("%s", w)
        total = len(rows)
        if start_row > total:  # 縮小/差し替え → 先頭から
            start_row = 0
        new_rows = rows[start_row:]
        inserted = 0
        if new_rows:
            with self.db.connect() as conn:
                inserted = insert_in_chunks(
                    conn, self.registry.get(table_name), new_rows, on_conflict="ignore"
                )
            logger.info(
                "appended %s: %d new rows (total %d)", table_name, len(new_rows), total
            )
            if inserted:
                self._notify_change()
        return inserted, total

    def count_rows(self, table_name: str, csv_path: Path) -> int:
        """有効行数のみ返す。"""
        rows, _skipped, _warnings = self.read_rows(table_name, csv_path)
        return len(rows)

    # ------------------------------------------------------------------
    # 点検(取り込みと同じ読み方で、書き込まずに調べる — D7-01, D7-02)
    # ------------------------------------------------------------------
    def inspect_file(
        self,
        table_name: str,
        csv_path: Path,
        *,
        limit: int | None = None,
        database_url: str | None = None,
    ) -> CsvFileCheck:
        """CSV を取り込みと同じ規則(文字コードの判別・見出しの対応・値の変換・読めない
        行の扱い)で読み、取り込んだらどうなるかを調べる(DB には書き込まない)。

        ``limit`` は調べるデータ行の上限(大きなファイル用)。``database_url`` が
        PostgreSQL / MySQL なら、その列に入らない値(D3-06)も調べる。読めない CSV は
        :class:`CsvEncodingError`。
        """
        path = Path(csv_path)
        src = _Source(table_name, path, TableImportResult(table=table_name))
        src.encodings, header = self._sniff(path)
        check = CsvFileCheck(table=table_name, path=path)
        if header is None:
            return check
        src.plan = self._converter(table_name).header_plan(header)
        limits = (
            declared_limits(self.registry.get(table_name), database_url)
            if database_url
            else None
        )

        def attempt(encoding: str, expect: HeaderPlan | None, lossy: bool):
            result = CsvFileCheck(table=table_name, path=path, encoding=encoding)
            stats = _Stats()
            self._inspect_rows(result, stats, expect, lossy, limit, limits)
            if lossy and stats.good <= stats.bad:
                raise _MostlyUndecodable()
            return result

        check = self._read_with_fallback(src, attempt)
        check.fallback = self._is_fallback(check.encoding or "utf-8")
        return check

    def _inspect_rows(
        self,
        check: CsvFileCheck,
        stats: _Stats,
        expect: HeaderPlan | None,
        lossy: bool,
        limit: int | None,
        limits: ValueLimits | None,
    ) -> None:
        """:meth:`inspect_file` の 1 回分の読み取り(``check`` に結果を書く)。"""
        encoding = check.encoding or "utf-8"
        table = check.table
        tdef = self.config.tables[table]
        errors = "surrogateescape" if lossy else "strict"
        with check.path.open(newline="", encoding=encoding, errors=errors) as fh:
            reader = csv.reader(map(_STRIP_NUL, fh))
            header = self._read_header(reader, check.path)
            if header is None:
                return
            plan = self._converter(table).header_plan(header)
            if expect is not None and _plan_key(plan) != _plan_key(expect):
                raise _PlanMismatch()
            check.plan = plan
            convert = _CheckedPlan(plan, encoding, stats if lossy else None).convert
            hint = self._new_hint(table, quiet=True)
            numeric_cells = [
                (idx, col)
                for idx, col, _conv in plan.cells
                if tdef.column_type(col) in ("int", "float")
            ]
            pk = tdef.primary_key
            keys = _KeyTracker()
            for line, raw in _numbered_rows(reader):
                if limit is not None and check.rows >= limit:
                    check.sampled = True
                    break
                try:
                    record = convert(raw)
                except CoercionError:
                    self._note_failure(check, plan, line, raw, encoding)
                    continue
                if not record or _all_blank(record):
                    check.blank += 1
                    continue
                if limits is not None:
                    for col, value in record.items():
                        reason = limits.violation(col, value)
                        if reason is not None and col not in check.too_large:
                            check.too_large[col] = (line, reason)
                for idx, col in numeric_cells:
                    if col in check.leading_zeros or idx >= len(raw):
                        continue
                    value = raw[idx].strip()
                    if value[:1] in ("0", "+", "-") and _LEADING_ZERO.match(value):
                        check.leading_zeros[col] = (line, value)
                key = record.get(pk)
                if key is not None and check.duplicate_key is None:
                    first = keys.first_line(key, line)
                    if first is not None:
                        check.duplicate_key = (key, first, line)
                if hint is not None and hint.active:
                    hint.observe(record)
                check.rows += 1
        if hint is not None:
            check.numeric = dict(hint.finish())

    def _note_failure(
        self,
        check: CsvFileCheck,
        plan: HeaderPlan,
        line: int,
        raw: Sequence[str],
        encoding: str,
    ) -> None:
        """変換できなかった行の、どの列のどの値が原因かを記録する。"""
        if type(raw) is _BadRow:
            check.unparsable.append(line)
            return
        if any(csv_encoding.has_escaped(cell) for cell in raw):
            check.undecodable.append(line)
            return
        for idx, col, conv in plan.cells:
            if col in check.failures or idx >= len(raw):
                continue
            try:
                conv(raw[idx])
            except CoercionError:
                check.failures[col] = (line, raw[idx].strip())

    # ------------------------------------------------------------------
    # 追記監視(バイト位置から完全な行だけを読む — PERF-05, PERF-06)
    # ------------------------------------------------------------------
    def tail_key(self, table_name: str) -> str:
        """追記監視の読み取り位置を保存する ``_ingest_state`` のキー。"""
        return TAIL_KEY_PREFIX + table_name

    def init_tail(self, table_name: str, csv_path: Path) -> TailState:
        """追記監視の開始時の読み取り位置(取り込み済みとみなす位置)を作る。

        ファイル末尾の完全な行の直後を位置にする(起動前からある行は取り込み直さない。
        書きかけの最後の行は、改行が届いてから取り込む)。``records`` は既存の行数
        (参考値)。文字コードはファイルの先頭と末尾の部分から判別する。
        """
        path = Path(csv_path)
        state = TailState()
        if not self._tailable():
            state.full_read = True
            state.records = self.count_rows(table_name, path)
            return state
        st = os.stat(path)
        state.identity = (st.st_dev, st.st_ino)
        with path.open("rb") as fh:
            end, lines = 0, 0
            carry = b""
            while True:
                block = fh.read(_TAIL_BLOCK)
                data = carry + block if carry else block
                consumed, count = _complete_prefix(data) if data else (0, 0)
                end += consumed
                lines += count
                carry = data[consumed:]
                if not block:
                    break
            if end:
                first = _read_at(fh, 0, min(end, _TAIL_BLOCK))
                header = first[: _first_record_end(first)]
                rows, _consumed, _checked = self._tail_rows(path, header, state)
                if rows:
                    self._set_tail_header(table_name, path, state, rows[0])
                state.head = _read_at(fh, 0, min(end, _WINDOW))
                state.tail = _read_at(fh, max(end - _WINDOW, 0), min(end, _WINDOW))
                self._judge_encoding(state, fh, end)
            state.offset = end
            state.records = max(lines - 1, 0)
        return state

    def _judge_encoding(self, state: TailState, fh, end: int) -> None:
        """ファイルの先頭と末尾の部分から、追記を読む文字コードとその確かさを決める。

        見出しが ASCII だけでも、データが cp932 なら cp932 で読み始め、UTF-8 の
        データがあれば壊れた 1 行で cp932 に切り替えない(D3-01)。
        """
        head = _whole_lines(
            _read_at(fh, 0, min(end, _SNIFF_BYTES)),
            from_start=True,
            to_end=end <= _SNIFF_BYTES,
        )
        start = max(end - _WINDOW, 0)
        tail = _whole_lines(state.tail, from_start=start == 0, to_end=True)
        sample = head + tail if start >= len(head) else head
        if sample.isascii():
            return
        current = state.encoding or csv_encoding.UTF8_FIRST
        chain = {_codec_name(current): current}
        if self._auto:
            for encoding in self.encoding_candidates():
                chain.setdefault(_codec_name(encoding), encoding)
        for encoding in chain.values():
            evidence = csv_encoding.line_evidence(sample, encoding)
            if evidence > 0:
                name = _codec_name(encoding)
                state.encoding = "utf-8" if name == "utf-8-sig" else encoding
                state.evidence = evidence
                return

    def start_tail(self, table_name: str, csv_path: Path) -> tuple[TailState, bool]:
        """追記監視を始める読み取り位置と、前回の続きから読むか(D3-03)。

        前回保存した読み取り位置があり、ファイルがその位置まで変わっていなければその
        続きから読む(停止中に追記された行も取り込む)。停止中にファイルが差し替えられて
        いれば先頭から読み直す(動いている間に差し替えられたときと同じ。主キーが既に
        ある行は読み飛ばす)。保存した位置が無ければ、今のファイル末尾から読む
        (:meth:`init_tail`)。
        """
        path = Path(csv_path)
        key = self.tail_key(table_name)
        state = self.resume_tail(table_name, path)
        if state is not None:
            return state, True
        state = self.init_tail(table_name, path)
        state.key = key
        self.save_tail(state)
        return state, False

    def resume_tail(self, table_name: str, csv_path: Path) -> TailState | None:
        """保存した読み取り位置を読み込む(無ければ None)。

        ファイルがその位置までに変わっていれば、先頭から読み直す状態を返す。
        """
        path = Path(csv_path)
        key = self.tail_key(table_name)
        try:
            raw = self._state_store().get(key)
        except DBAPIError as exc:
            logger.warning("%s: 保存した読み取り位置を読めません: %s", path.name, exc)
            return None
        saved = parse_tail_cursor(raw, key)
        if saved is None:
            if raw:
                logger.warning(
                    "%s: 保存した読み取り位置の形式が違うため使いません", path.name
                )
            return None
        state, windows = saved
        if state.full_read == self._tailable():
            return None  # 文字コードの設定が変わった(読み方が違う)
        if state.full_read:
            return state
        with path.open("rb") as fh:
            st = os.fstat(fh.fileno())
            reason = restore_windows(fh, st.st_size, state, windows)
            if reason:
                logger.warning(
                    "%s: 前回の停止の後に%sため、先頭から取り込み直します"
                    "(主キーが既にある行は読み飛ばします)",
                    path.name,
                    reason,
                )
                return TailState(key=key)
            state.identity = (st.st_dev, st.st_ino)
            if state.offset:
                first = _read_at(fh, 0, min(state.offset, _TAIL_BLOCK))
                header = first[: _first_record_end(first)]
                rows, _consumed, _checked = self._tail_rows(path, header, TailState())
                if rows:
                    self._set_tail_header(table_name, path, state, rows[0])
        logger.info(
            "%s: 前回の読み取り位置(%d 行目まで取り込み済み)の続きから読みます",
            path.name,
            state.records + 1,
        )
        return state

    def save_tail(self, state: TailState, *, conn=None) -> None:
        """読み取り位置を ``_ingest_state`` に保存する(``state.key`` が無ければ何もしない)。"""
        if state.key is not None:
            self._state_store().set(state.key, tail_cursor(state), conn=conn)

    def _state_store(self):
        if self._tail_store is None:
            from ..db.ingest_state import IngestStateStore

            store = IngestStateStore(self.db)
            store.create()
            self._tail_store = store
        return self._tail_store

    def _forget_tails(self, conn, tables: Sequence[str]) -> None:
        """全体の取り込みで書き込んだテーブルの、保存した読み取り位置を消す。

        追記監視が古い位置の続きから読むと、取り込んだばかりの行をもう一度入れてしまう
        (主キーの無い CSV では重複する)。消すと監視は今のファイル末尾から読む。
        """
        if not tables:
            return
        from ..db.ingest_state import IngestStateStore

        if not inspect(conn).has_table("_ingest_state"):
            return
        store = self._tail_store or IngestStateStore(self.db)
        for name in tables:
            store.delete(self.tail_key(name), conn=conn)

    def tail_append(  # noqa: C901 - 追記・差し替え・文字コードの分岐を 1 か所で追うため
        self, table_name: str, csv_path: Path, state: TailState
    ) -> int:
        """``state.offset`` 以降に追記された完全な行だけを取り込み、挿入した行数を返す。

        - 書きかけの最後の行(改行が無い・引用符が閉じていない)は取り込まず、次回に
          持ち越す(PERF-06)。
        - ファイルが小さくなった・先頭や読み取り位置の直前が変わった(ローテーション・
          差し替え)ときは先頭から読み直し、ヘッダも読み直す。主キーが既にある行は
          読み飛ばす。エディタの「保存」で別ファイルに置き換わっても、内容が続いて
          いれば続きから読む。
        - ``state`` は取り込みを確定してから進める(失敗したら次回やり直す)。
          ``state.key`` があれば、読み取り位置を取り込んだ行と同じトランザクションで
          保存する(D3-03)。
        """
        path = Path(csv_path)
        if state.key is not None:
            self._state_store()  # 保存先の表は取り込みのトランザクションの前に作る
        if state.full_read or not self._tailable():
            state.full_read = True
            inserted, total = self.append_since(
                table_name, path, start_row=state.records
            )
            changed = total != state.records
            state.records = total
            if changed:
                self.save_tail(state)
            return inserted
        try:
            st = os.stat(path)
        except FileNotFoundError:
            return 0
        with path.open("rb") as fh:
            reason = self._tail_reset_reason(fh, st, state)
            if reason:
                logger.warning(
                    "%s: %sため先頭から取り込み直します(主キーが既にある行は読み飛ばします)",
                    path.name,
                    reason,
                )
                state.reset()
            elif state.identity not in (None, (st.st_dev, st.st_ino)):
                # 別のファイルに置き換わった(エディタの保存・コピーで上書き)が、読み取り
                # 位置までの内容は同じ → 続きから読む(先頭から読むと主キーの無い CSV で
                # 行が重複する)。
                logger.info(
                    "%s: ファイルが置き換わりましたが内容が続いているため、続きから読みます",
                    path.name,
                )
            state.identity = (st.st_dev, st.st_ino)
            if st.st_size <= state.offset:
                return 0
            work = replace(state)
            stats = _Stats(line=1 + work.records, hint=self._new_hint(table_name))
            inserted = 0
            table = self.registry.get(table_name)
            with self.db.connect() as conn:
                limits = self._value_limits(conn, table_name)
                fh.seek(work.offset)
                carry = b""
                while True:
                    block = fh.read(_TAIL_BLOCK)
                    data = carry + block if carry else block
                    if not data:
                        break
                    cut = max(data.rfind(b"\n"), data.rfind(b"\r")) + 1
                    rows, consumed, checked = (
                        self._tail_rows(path, data[:cut], work)
                        if cut
                        else ([], 0, False)
                    )
                    if consumed:
                        if work.offset == 0 and work.header is None:
                            self._set_tail_header(table_name, path, work, rows[0])
                            rows = rows[1:]
                        if work.plan is not None and rows:
                            plan: Any = work.plan
                            if limits is not None:
                                plan = _GuardedPlan(plan, limits)
                            if checked:
                                plan = _CheckedPlan(plan, work.encoding or "utf-8")
                            records = self._convert_rows(rows, plan, path.name, stats)
                            inserted += (
                                insert_in_chunks(
                                    conn, table, records, on_conflict="ignore"
                                )
                                or 0
                            )
                        self._advance(work, data[:consumed])
                    carry = data[consumed:]
                    if not block:
                        break
                work.records = stats.line - 1
                if work.key is not None and work.offset != state.offset:
                    self.save_tail(work, conn=conn)
        state.__dict__.update(work.__dict__)
        for w in self._row_warnings(path, stats):
            logger.warning("%s", w)
        if stats.hint is not None:
            self._warn_numeric(table_name, stats.hint.finish())
        if stats.rows:
            logger.info(
                "appended %s: %d new rows (total %d)",
                table_name,
                inserted,
                state.records,
            )
            duplicates = stats.rows - inserted
            if duplicates:
                logger.info(
                    "%s: 主キーが既にある %d 行を読み飛ばしました",
                    path.name,
                    duplicates,
                )
        if inserted:
            self._notify_change()
        return inserted

    # ------------------------------------------------------------------
    # 内部: 取り込みの本体
    # ------------------------------------------------------------------
    def _run(self, sources: list[_Source], mode: str, force: bool) -> bool:
        """ヘッダを確かめてから 1 トランザクションで書き込む。データが変わったら True。"""
        for src in sources:
            self._inspect(src, mode, force)
        order = {name: i for i, name in enumerate(self.dependency_order())}
        ordered = sorted(sources, key=lambda s: order[s.table])
        while True:
            try:
                return self._write(ordered, mode)
            except _NoValidRows as exc:
                # 置換で有効行が 0 件だったテーブルは既存の行を残し、取り消して
                # そのテーブル抜きでやり直す(削除済みの行を戻すため)。
                exc.source.kept = True
            except _SourceDbError as exc:
                # トランザクションを取り消してから、どの CSV の何行目かを調べて知らせる
                explained = self._explain_db_error(exc, mode)
                if explained is None:
                    raise exc.error from None
                raise explained from exc.error

    def _write(self, ordered: list[_Source], mode: str) -> bool:
        """有効な CSV を 1 トランザクションで書き込む。データが変わったら True。"""
        active = [s for s in ordered if not (s.refused or s.kept)]
        for src in active:
            src.wrote = False
            src.not_empty = False
        if mode == "replace" and active and self.db.backend == "sqlite":
            staged = self._write_staged(active, ordered)
            if staged is not None:
                return staged
        changed = False
        with self.db.write_transaction() as conn:
            if mode == "if-empty":
                for src in active:
                    table = self.registry.get(src.table)
                    if conn.execute(select(1).select_from(table).limit(1)).first():
                        src.not_empty = True
                        logger.info(
                            "skip importing %s: table already has rows", src.table
                        )
                active = [s for s in active if not s.not_empty]
            if mode == "replace":
                for src in reversed(active):
                    self._delete_all(conn, src, active, ordered)
            for src in active:
                inserted = self._write_source(conn, src, mode)
                if mode == "replace" and src.kept:
                    raise _NoValidRows(src)
                src.wrote = mode == "replace" or src.result.inserted > 0
                changed = changed or inserted > 0 or mode == "replace"
            self._forget_tails(conn, [s.table for s in active if s.wrote])
        return changed

    def _write_staged(
        self, active: list[_Source], ordered: list[_Source]
    ) -> bool | None:
        """SQLite の置換: CSV をまず一時テーブル(TEMP)へ読み込み、本体のテーブルの
        書き込みロックは入れ替え(削除と ``INSERT … SELECT``)の間だけ取る(D4-06)。

        CSV の解析・値の変換の間(大きな CSV では数秒)もロックを持つと、その間の画面・
        API・インジェストの書き込みが busy_timeout を過ぎて失敗するため。一時テーブルは
        この接続だけのもので、本体の DB をロックしない。一時テーブルを作れない環境
        (一時ファイルの場所に書けないなど)では None を返し、従来どおり 1 つの
        書き込みトランザクションで取り込む。
        """
        with self.db.engine.connect() as conn:
            stages: dict[str, Table] = {}
            try:
                try:
                    with conn.begin():
                        for src in active:
                            stages[src.table] = _stage_table(
                                conn, self.registry.get(src.table)
                            )
                except DBAPIError as exc:
                    logger.info("一時テーブルを使わずに取り込みます: %s", exc)
                    return None
                # 1) 一時テーブルへ読み込む(本体の DB はロックしない)
                with conn.begin():
                    for src in active:
                        self._write_source(
                            conn, src, "replace", target=stages[src.table]
                        )
                replaced = [s for s in active if not s.kept]
                # 2) 入れ替える(ここだけ書き込みロックを取る)
                with conn.begin():
                    raw = conn.connection.dbapi_connection
                    if raw is not None and not getattr(raw, "in_transaction", True):
                        conn.exec_driver_sql("BEGIN IMMEDIATE")
                    targets = {
                        s.table: _main_table(self.registry.get(s.table))
                        for s in replaced
                    }
                    for src in reversed(replaced):
                        self._delete_all(
                            conn, src, replaced, ordered, table=targets[src.table]
                        )
                    for src in replaced:
                        target = targets[src.table]
                        stage = stages[src.table]
                        names = [c.name for c in target.columns]
                        conn.execute(
                            insert(target).from_select(
                                names, select(*(stage.c[n] for n in names))
                            )
                        )
                        src.wrote = True
                    self._forget_tails(conn, [s.table for s in replaced])
            finally:
                _drop_stages(conn, stages.values())
        return bool(replaced)

    def _delete_all(
        self,
        conn,
        src: _Source,
        active: Sequence[_Source],
        ordered: Sequence[_Source],
        *,
        table: Table | None = None,
    ) -> None:
        """置換の前に ``src`` のテーブルを空にする。

        外部キーを強制する DB(PostgreSQL / MySQL)では、置き換えない子のテーブルの行が
        参照していると削除できない。ドライバの文言(SQL 入り)の代わりに、どのテーブルが
        参照していて、なぜ置き換えなかったかと対処を示す(取り込みは全て取り消す)。
        自分自身を参照する列(親子のツリー)は、MySQL が 1 行ずつ外部キーを確かめるため
        先に空にしてから削除する(D3-07)。``table`` は削除する表(既定は登録済みの表)。
        """
        if table is None:
            table = self.registry.get(src.table)
        try:
            refs = _self_references(table)
            if refs and conn.dialect.name in ("mysql", "mariadb"):
                conn.execute(
                    update(table)
                    .where(or_(*(table.c[c].is_not(None) for c in refs)))
                    .values({c: None for c in refs})
                )
            conn.execute(delete(table))
        except DBAPIError as exc:
            if classify(exc) != CONFLICT:
                raise
            raise ConflictError(self._referenced_message(src, active, ordered)) from exc

    def _referenced_message(
        self, src: _Source, active: Sequence[_Source], ordered: Sequence[_Source]
    ) -> str:
        replaced = {s.table for s in active}
        by_table = {s.table: s for s in ordered}
        children = sorted(
            {
                child.name
                for child in self.registry.tables.values()
                for fk in child.foreign_keys
                if fk.column.table.name == src.table
                and child.name not in (src.table, *replaced)
            }
        )
        reasons = []
        for name in children:
            other = by_table.get(name)
            if other is None:
                reason = "今回は置き換えていません"
            elif other.refused:
                reason = "ヘッダに列が足りないため置き換えませんでした"
            elif other.kept:
                reason = "有効行が 0 件のため置き換えませんでした"
            else:
                reason = "今回は置き換えていません"
            reasons.append(f"{name}({reason})")
        referencing = (
            "テーブル " + "、".join(reasons) + " の行"
            if reasons
            else "ほかの行(config.py に無いテーブルや同じテーブルの行を含む)"
        )
        return (
            f"{src.name}: テーブル '{src.table}' を置き換えられません。{referencing}が"
            "外部キーで参照しています。参照しているテーブルの CSV も揃えて一緒に置き換えるか"
            "(monitor-app import-csv)、追記で取り込んでください"
            "(import-csv --keep / MONITOR_INGEST_MODE=append)。データは変更していません"
        )

    def _write_source(
        self, conn, src: _Source, mode: str, *, target: Table | None = None
    ) -> int:
        """1 つの CSV を流し込む。``auto`` で読めなければ次の文字コードで読み直す。

        1 回の読み取りごとに SAVEPOINT で包み、読み直すときはこのテーブルに入れた分だけ
        取り消す。``target`` は書き込む表(既定は登録済みの表。SQLite の置換では一時
        テーブル)。
        """
        table = target if target is not None else self.registry.get(src.table)
        limits = self._value_limits(conn, src.table)

        def attempt(encoding: str, expect: HeaderPlan | None, lossy: bool):
            stats = _Stats(hint=self._new_hint(src.table))
            records = self._records(
                src.table,
                src.path,
                encoding,
                stats,
                expect=expect,
                lossy=lossy,
                limits=limits,
            )
            try:
                with conn.begin_nested():
                    result = self._insert(conn, table, records, mode, stats)
                    if lossy and stats.good <= stats.bad:
                        raise _MostlyUndecodable()
            except DBAPIError as exc:
                records.close()
                produced = None if stats.written else stats.rows
                raise _SourceDbError(src, exc, encoding, lossy, produced) from exc
            finally:
                records.close()
            return result, stats, encoding

        result, stats, encoding = self._read_with_fallback(src, attempt)
        inserted = stats.rows if result is None else result
        self._finish_source(src, stats, encoding, inserted, mode)
        return inserted

    def _read_with_fallback(self, src: _Source, attempt: Callable[..., Any]) -> Any:
        """``src.encodings`` を順に試して ``attempt(文字コード, 期待する見出しの対応,
        読めない行を飛ばすか)`` を実行し、その結果を返す。

        途中で読めなくなったら(UnicodeDecodeError)、同じ文字コードで読めない行だけを
        飛ばして読み直す。読めた ASCII 以外の行が読めなかった行より多ければ(UTF-8 の
        CSV に壊れたバイトが混じっただけ — D3-02)その結果を使い、そうでなければ次の
        文字コードで最初から読み直す(先頭がたまたま UTF-8 として読めた cp932 の CSV)。
        どれでも読めなければ :class:`CsvEncodingError`。
        """
        candidates = src.encodings
        for i, encoding in enumerate(candidates):
            last = i == len(candidates) - 1
            expect = None if i == 0 else src.plan
            error: UnicodeDecodeError | None = None
            for lossy in (False, True):
                try:
                    return attempt(encoding, expect, lossy)
                except _PlanMismatch:
                    if last:
                        raise self._encoding_error(src.path, None) from None
                    break
                except UnicodeDecodeError as exc:
                    error = exc  # 読めない行だけを飛ばして読み直す
                except _MostlyUndecodable:
                    if last:
                        raise self._encoding_error(src.path, error) from error
                    break
        raise AssertionError("unreachable")  # pragma: no cover - 候補は 1 つ以上

    def _insert(
        self, conn, table, rows: Iterator[dict[str, Any]], mode: str, stats: _Stats
    ) -> int:
        refs = _self_references(table) if mode != "append" else []
        if not refs or conn.dialect.name == "sqlite":
            if mode == "append":
                inserted = insert_in_chunks(conn, table, rows, on_conflict="ignore")
            else:
                inserted = insert_in_chunks(conn, table, rows)
            stats.written = True
            return inserted
        # 自分自身を参照する列(親子のツリー)は、親の行より前に子の行があっても入るよう、
        # 空にして入れてから後で埋める(外部キーを強制する DB — D3-07)。
        pk = self.registry.primary_key(table.name)
        pending: list[tuple[Any, dict[str, Any]]] = []
        inserted = insert_in_chunks(conn, table, _defer_refs(rows, pk, refs, pending))
        stats.written = True
        if pending:
            names = {c: f"_ref_{i}" for i, c in enumerate(refs)}
            stmt = (
                update(table)
                .where(table.c[pk] == bindparam("_pk"))
                .values({c: bindparam(name) for c, name in names.items()})
            )
            it = iter(pending)
            while chunk := list(islice(it, INSERT_CHUNK)):
                conn.execute(
                    stmt,
                    [
                        {"_pk": key, **{names[c]: refs_[c] for c in refs}}
                        for key, refs_ in chunk
                    ],
                )
        return inserted

    def _finish_source(
        self, src: _Source, stats: _Stats, encoding: str, inserted: int, mode: str
    ) -> None:
        result = src.result
        result.inserted = inserted
        result.skipped = stats.skipped
        result.duplicates = stats.rows - inserted if mode == "append" else 0
        result.encoding = encoding
        result.warnings = list(src.header_warnings)
        if self._is_fallback(encoding):
            message = self._fallback_message(src.path, encoding)
            result.warnings.append(message)
            self._warn_once(("encoding", str(src.path)), message)
        # 型変換の失敗は従来どおり結果の警告に積む(ログには出さない)。
        result.warnings.extend(stats.warnings)
        blank = self._blank_message(src.path, stats)
        if blank:
            result.warnings.append(blank)
            logger.info("%s", blank, extra=IN_RESULT)
        if result.duplicates:
            message = duplicates_message(src.name, result.duplicates)
            result.warnings.append(message)
            logger.info("%s", message, extra=IN_RESULT)
        if stats.hint is not None:
            src.notes.extend(self._warn_numeric(src.table, stats.hint.finish()))
        result.warnings.extend(src.notes)
        if stats.rows == 0 and mode != "append":
            message = self._kept_message(src.path)
            logger.warning("%s", message, extra=IN_RESULT)
            result.warnings.append(message)
            result.inserted = 0
            if mode == "replace":
                src.kept = True

    def _inspect(self, src: _Source, mode: str, force: bool) -> None:
        """文字コードを判別してヘッダを読み、config.py の列と突き合わせる。"""
        src.encodings, header = self._sniff(src.path)
        src.plan = None
        src.header_warnings = []
        result = src.result
        if header is None:
            return
        plan = self._converter(src.table).header_plan(header)
        src.plan = plan
        result.matched = list(plan.matched)
        result.missing = list(plan.missing)
        result.unknown = list(plan.unknown)
        result.renamed = dict(plan.renamed)
        for cell, column in plan.renamed.items():
            logger.info(
                "%s: 列名 '%s' を '%s' として取り込みます",
                src.name,
                cell,
                column,
                extra=IN_RESULT,
            )
        if plan.unknown:
            logger.info(
                "%s: config.py に無い列は取り込みません: %s",
                src.name,
                ", ".join(plan.unknown),
                extra=IN_RESULT,
            )
        pk = self.config.tables[src.table].primary_key
        missing = [col for col in plan.missing if col != pk]
        if not missing:
            return
        columns = ", ".join(missing)
        detail = self._did_you_mean(plan, missing)
        if mode == "replace" and not force:
            src.refused = True
            result.refused = True
            message = (
                f"{src.name}: config.py の列 {columns} が CSV のヘッダに無いため、"
                f"置換を中止しました(既存データを保持){detail}。"
                "ヘッダの列名を config.py の columns に合わせてください"
            )
        else:
            message = (
                f"{src.name}: config.py の列 {columns} が CSV のヘッダにありません"
                f"(空のまま取り込みます){detail}"
            )
        logger.warning("%s", message, extra=IN_RESULT)
        src.header_warnings.append(message)
        result.warnings = list(src.header_warnings)

    @staticmethod
    def _did_you_mean(plan: HeaderPlan, missing: Sequence[str]) -> str:
        if not plan.unknown:
            return ""
        by_norm = {normalize_name(cell): cell for cell in plan.unknown}
        hints = []
        for col in missing:
            close = difflib.get_close_matches(
                normalize_name(col), list(by_norm), n=1, cutoff=0.6
            )
            if close:
                hints.append(f"'{by_norm[close[0]]}' → {col}?")
        if hints:
            return "(CSV の列: " + ", ".join(hints) + ")"
        return "(CSV にある列: " + ", ".join(plan.unknown) + ")"

    # ------------------------------------------------------------------
    # 内部: DB のエラーの説明(D3-06, D5-08)
    # ------------------------------------------------------------------
    def _explain_db_error(
        self, exc: _SourceDbError, mode: str
    ) -> MonitorAppError | None:
        """CSV の書き込み中の DB のエラーを、ファイル名と行番号つきの説明にする。

        主キーの重複は CSV の中の重複した行を探して示す。制約違反・値の不正のほかは
        (接続断など CSV と関係ないもの)None を返し、元の例外のまま扱う。
        """
        kind = classify(exc.error)
        if kind not in (CONFLICT, db_errors.INVALID):
            return None
        src = exc.source
        undone = "取り込みは取り消しました(データは変わっていません)"
        if kind == CONFLICT and mode != "append":
            found = self._duplicate_key(src, exc.encoding, exc.lossy)
            if found is not None:
                value, first, second = found
                pk = self.config.tables[src.table].primary_key
                return ConflictError(
                    f"{src.name} {second}行目: 主キー {pk}={value!r} が {first}行目と"
                    f"重複しています。{undone}"
                )
        where = ""
        if exc.produced:
            lines = self._chunk_lines(src, exc.encoding, exc.lossy, exc.produced)
            if lines is not None and lines[0] == lines[1]:
                where = f" {lines[0]}行目"
            elif lines is not None:
                where = f" {lines[0]}〜{lines[1]}行目のどれか"
        if kind == CONFLICT:
            base = db_errors.conflict_message(exc.error)
            return ConflictError(f"{src.name}{where}: {base}。{undone}")
        base = db_errors.invalid_message(exc.error)
        return InvalidPayloadError(f"{src.name}{where}: {base}。{undone}")

    def _numbered_records(
        self, src: _Source, encoding: str, lossy: bool
    ) -> Iterator[tuple[int, dict[str, Any]]]:
        """取り込みが書き込む行を (行番号, 変換した行) で返す(エラーの説明用)。"""
        limits = self._limits.get(src.table)
        errors = "surrogateescape" if lossy else "strict"
        with src.path.open(newline="", encoding=encoding, errors=errors) as fh:
            reader = csv.reader(map(_STRIP_NUL, fh))
            header = self._read_header(reader, src.path)
            if header is None:
                return
            plan: Any = self._converter(src.table).header_plan(header)
            if limits is not None:
                plan = _GuardedPlan(plan, limits)
            convert = _CheckedPlan(plan, encoding, _Stats() if lossy else None).convert
            for line, raw in _numbered_rows(reader):
                try:
                    record = convert(raw)
                except CoercionError:
                    continue
                if record and not _all_blank(record):
                    yield line, record

    def _duplicate_key(
        self, src: _Source, encoding: str, lossy: bool
    ) -> tuple[Any, int, int] | None:
        pk = self.config.tables[src.table].primary_key
        keys = _KeyTracker()
        for line, record in self._numbered_records(src, encoding, lossy):
            key = record.get(pk)
            if key is None:
                continue
            first = keys.first_line(key, line)
            if first is not None:
                return key, first, line
        return None

    def _chunk_lines(
        self, src: _Source, encoding: str, lossy: bool, produced: int
    ) -> tuple[int, int] | None:
        """エラーになった書き込み(``INSERT_CHUNK`` 行ずつ)の最初と最後の行番号。"""
        start = (produced - 1) // INSERT_CHUNK * INSERT_CHUNK + 1
        lines: list[int] = []
        for index, (line, _record) in enumerate(
            self._numbered_records(src, encoding, lossy), start=1
        ):
            if index == start:
                lines.append(line)
            if index == produced:
                lines.append(line)
                break
        return (lines[0], lines[-1]) if lines else None

    # ------------------------------------------------------------------
    # 内部: 読み取りと変換
    # ------------------------------------------------------------------
    def _converter(self, table_name: str) -> RowConverter:
        conv = self._converters.get(table_name)
        if conv is None:
            conv = self._converters[table_name] = RowConverter(
                self.config.tables[table_name]
            )
        return conv

    def _new_hint(self, table_name: str, *, quiet: bool = False) -> _NumericHint | None:
        """DX-08 の型の推奨を調べる準備(警告済みの列は調べない。``quiet`` は全列)。"""
        columns = [
            col
            for col in self._converter(table_name).inferred_str_columns()
            if quiet or ("numeric", table_name, col) not in self._warned
        ]
        return _NumericHint(columns) if columns else None

    def _value_limits(self, conn, table_name: str) -> ValueLimits | None:
        """サーバー DB の列の制限(SQLite は None)。DB の列の型から 1 度だけ作る。"""
        if conn.dialect.name == "sqlite":
            return None
        if table_name not in self._limits:
            self._limits[table_name] = reflected_limits(
                conn, self.registry.get(table_name)
            )
        return self._limits[table_name]

    @staticmethod
    def _read_header(reader, path: Path) -> list[str] | None:
        try:
            return next(reader, None)
        except csv.Error as exc:
            raise CsvFormatError(
                f"{path.name} の見出し(1 行目)を CSV として読めません({exc})",
                "CSV ファイルが壊れていないか、カンマ区切りの CSV かを確認してください",
            ) from exc

    def _records(
        self,
        table_name: str,
        path: Path,
        encoding: str,
        stats: _Stats,
        *,
        expect: HeaderPlan | None = None,
        lossy: bool = False,
        limits: ValueLimits | None = None,
    ) -> Iterator[dict[str, Any]]:
        """CSV を 1 行ずつ変換して返す(``csv.DictReader`` と同じ解釈)。

        ``lossy=True`` は読めないバイトを含む行を読み飛ばす(警告に残す)。``limits`` は
        DB の列の制限(超える値の行を読み飛ばす)。
        """
        errors = "surrogateescape" if lossy else "strict"
        with path.open(newline="", encoding=encoding, errors=errors) as fh:
            reader = csv.reader(map(_STRIP_NUL, fh))
            header = self._read_header(reader, path)
            if header is None:
                return
            plan = self._converter(table_name).header_plan(header)
            if expect is not None and _plan_key(plan) != _plan_key(expect):
                raise _PlanMismatch()
            converter: Any = plan
            if limits is not None:
                converter = _GuardedPlan(converter, limits)
            if lossy:
                converter = _CheckedPlan(converter, encoding, stats)
            yield from self._convert_rows(reader, converter, path.name, stats)

    @staticmethod
    def _convert_rows(
        rows: Iterable[list[str]], plan: Any, name: str, stats: _Stats
    ) -> Iterator[dict[str, Any]]:
        convert = plan.convert
        hint = stats.hint
        line = stats.line
        produced = 0
        it = iter(rows)
        try:
            while True:
                try:
                    for raw in it:
                        if not raw:  # 空行(csv.DictReader と同じく行番号にも数えない)
                            continue
                        line += 1
                        try:
                            record = convert(raw)
                        except CoercionError as exc:
                            stats.skipped += 1
                            stats.warnings.append(
                                f"{name} {line}行目: {_clip(str(exc))}"
                            )
                            continue
                        if not record:
                            continue
                        for value in record.values():
                            if value is not None and (
                                type(value) is not str or value.strip()
                            ):
                                break
                        else:  # 値がすべて空(Excel のカンマだけの行)
                            stats.skipped += 1
                            stats.blank_lines.append(line)
                            continue
                        if hint is not None and hint.active:
                            hint.observe(record)
                        produced += 1
                        yield record
                except csv.Error as exc:
                    # CSV として読めない行(長すぎるセルなど): その行だけを読み飛ばす(D3-09)
                    line += 1
                    stats.skipped += 1
                    stats.warnings.append(
                        f"{name} {line}行目: {_unparsable_message(exc)}"
                    )
                    continue
                break
        finally:
            stats.line = line
            stats.rows += produced

    @staticmethod
    def _blank_message(path: Path, stats: _Stats) -> str | None:
        if not stats.blank_lines:
            return None
        return (
            f"{path.name}: 値がすべて空の行を {len(stats.blank_lines)} 行スキップ"
            f"しました({_line_list(stats.blank_lines)})"
        )

    def _row_warnings(self, path: Path, stats: _Stats) -> list[str]:
        """行ごとの警告(型変換の失敗)と、値がすべて空の行のまとめ。"""
        blank = self._blank_message(path, stats)
        return stats.warnings + ([blank] if blank else [])

    def _warn_numeric(self, table_name: str, numeric: dict[str, str]) -> list[str]:
        """数値ばかりの str 列を 1 度だけ警告し、出した警告文を返す(DX-08)。"""
        messages = []
        for col, kind in numeric.items():
            key = ("numeric", table_name, col)
            if key in self._warned:
                continue
            self._warned.add(key)
            message = (
                f"{table_name}.{col}: 列名から str(文字列)と推論しましたが、CSV の値は"
                "すべて数値です。数値として集計・並べ替え・比較するには config.py の"
                f' columns で型を明示してください(例: {{"{col}": "{kind}"}}。作成済みの'
                "テーブルの列の型は変わりません)"
            )
            logger.warning("%s", message, extra=IN_RESULT)
            messages.append(message)
        return messages

    # ------------------------------------------------------------------
    # 内部: 文字コード
    # ------------------------------------------------------------------
    @property
    def _auto(self) -> bool:
        return csv_encoding.is_auto(self.encoding)

    def encoding_candidates(self) -> list[str]:
        """全体を読むときに試す文字コード(``auto`` なら UTF-8 → 予備の順)。"""
        return csv_encoding.candidates(self.encoding, self.encoding_fallback)

    def _is_fallback(self, encoding: str) -> bool:
        return self._auto and not csv_encoding.is_utf8(encoding)

    def _fallback_message(self, path: Path, encoding: str) -> str:
        return (
            f"{path.name}: UTF-8 として読めないため {encoding} で読み込みました"
            "(文字化けする場合は .env の MONITOR_CSV_ENCODING で CSV の文字コードを"
            f"指定してください。例: MONITOR_CSV_ENCODING={encoding})"
        )

    def _warn_once(self, key: tuple[str, ...], message: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            logger.warning("%s", message, extra=IN_RESULT)

    def _sniff(self, path: Path) -> tuple[list[str], list[str] | None]:
        """先頭を読んで文字コードを選び、(試す文字コード, ヘッダ) を返す。

        先頭が読めなくても、読めた ASCII 以外の行の方が多ければその文字コードを選ぶ
        (UTF-8 の CSV に壊れたバイトが混じっただけ。読めない行は取り込みで飛ばす)。
        """
        candidates = self.encoding_candidates()
        with path.open("rb") as fh:
            head = fh.read(_SNIFF_BYTES)
            at_eof = not fh.read(1)
        lines = _whole_lines(head, from_start=True, to_end=at_eof)
        error: UnicodeDecodeError | None = None
        for i, encoding in enumerate(candidates):
            errors = "strict"
            try:
                _decode_head(encoding, head, at_eof)
            except UnicodeDecodeError as exc:
                error = exc
                if csv_encoding.line_evidence(lines, encoding) <= 0:
                    continue
                errors = "surrogateescape"
            with path.open(newline="", encoding=encoding, errors=errors) as fh:
                header = self._read_header(csv.reader(map(_STRIP_NUL, fh)), path)
            if header is not None and any(map(csv_encoding.has_escaped, header)):
                continue
            return candidates[i:], header
        raise self._encoding_error(path, error)

    def _encoding_error(
        self, path: Path, exc: UnicodeDecodeError | None
    ) -> CsvEncodingError:
        reason = f"({exc.reason})" if exc is not None else ""
        if self._auto:
            tried = ", ".join(self.encoding_candidates())
            return CsvEncodingError(
                f"{path.name} を文字コード {tried} のどれでも読めません{reason}",
                "CSV の文字コードを .env の MONITOR_CSV_ENCODING で指定してください"
                "(例: MONITOR_CSV_ENCODING=cp932)",
            )
        return CsvEncodingError(
            f"{path.name} を文字コード {self.encoding} として読めません{reason}",
            "Excel で保存した CSV なら .env に MONITOR_CSV_ENCODING=cp932 を設定するか、"
            "MONITOR_CSV_ENCODING=auto(自動判別)にしてください",
        )

    def _tailable(self) -> bool:
        try:
            names = self.encoding_candidates()
        except CsvEncodingError:
            return False
        return csv_encoding.is_tailable(names)

    def _tail_encodings(self, state: TailState) -> list[str]:
        """追記を読む文字コードの順(今使っている文字コードが先頭)。"""
        at_start = state.offset == 0
        if self._auto:
            chain = ["utf-8-sig" if at_start else "utf-8", *self.encoding_fallback]
        elif not at_start and _codec_name(self.encoding) == "utf-8-sig":
            chain = ["utf-8"]  # BOM はファイルの先頭にしかない
        else:
            chain = [self.encoding]
        if state.encoding and self._auto and not at_start:
            chain = [state.encoding] + [e for e in chain if e != state.encoding]
        unique: dict[str, str] = {}
        for encoding in chain:
            unique.setdefault(_codec_name(encoding), encoding)
        return list(unique.values())

    def _decode_block(
        self, path: Path, data: bytes, state: TailState
    ) -> tuple[str, str, int, bool]:
        """追記の ``data``(完全な行)を読み、(文字列, 文字コード, 確かさ, 読めない行を
        含むか) を返す。

        今の文字コードで読めなければ、読めない行だけを飛ばして読む。それで読めた ASCII
        以外の行が、これまでの分も合わせて読めなかった行より多ければ(UTF-8 の CSV に
        壊れた 1 行が混じっただけ)文字コードを切り替えない(D3-01)。そうでなければ
        ほかの文字コード(``auto`` の予備)を試し、どれでも読めなければ
        :class:`CsvEncodingError`(読み取り位置は進めない)。
        """
        chain = self._tail_encodings(state)
        preferred = chain[0]
        try:
            text = data.decode(preferred)
        except UnicodeDecodeError as exc:
            error = exc
        else:
            gained = 0 if data.isascii() else 1
            return text, preferred, state.evidence + gained, False
        evidence = state.evidence + csv_encoding.line_evidence(data, preferred)
        # 同数なら、取り込み済みの行と同じ文字コードのまま読む(途中から別の文字コードで
        # 読むとテーブルの中で文字化けした行と正しい行が混ざる)。
        if evidence > 0 or (evidence == 0 and state.evidence > 0):
            return data.decode(preferred, "surrogateescape"), preferred, evidence, True
        for encoding in chain[1:]:
            try:
                text = data.decode(encoding)
            except UnicodeDecodeError:
                continue
            return text, encoding, 0 if data.isascii() else 1, False
        raise self._encoding_error(path, error)

    def _tail_rows(
        self, path: Path, data: bytes, state: TailState
    ) -> tuple[list[list[str]], int, bool]:
        """完全な行で終わる ``data`` をデコードして行に分ける。

        戻り値は (行, 消費バイト数, 読めない行(:class:`_BadRow`)を含むか)。行の中の NUL
        は取り除く(電源断で 0 埋めになった部分 — D3-09)。
        """
        text, encoding, evidence, checked = self._decode_block(path, data, state)
        if "\x00" in text:
            text = text.replace("\x00", "")
        try:
            rows, consumed = _parse_complete(data, len(data), text)
        except csv.Error:
            rows, consumed = _parse_complete_safe(data, len(data), text)
            checked = True
        if checked:
            message = _undecodable_message(encoding)
            rows = [
                (
                    _BadRow(message)
                    if type(row) is not _BadRow
                    and any(map(csv_encoding.has_escaped, row))
                    else row
                )
                for row in rows
            ]
        if consumed:
            name = _codec_name(encoding)
            state.encoding = "utf-8" if name == "utf-8-sig" else encoding
            state.evidence = evidence
            if self._is_fallback(encoding):
                self._warn_once(
                    ("encoding", str(path)), self._fallback_message(path, encoding)
                )
        return rows, consumed, checked

    def _set_tail_header(
        self, table_name: str, path: Path, state: TailState, header: list[str]
    ) -> None:
        plan = self._converter(table_name).header_plan(header)
        state.header = list(header)
        state.plan = plan
        for cell, column in plan.renamed.items():
            logger.info(
                "%s: 列名 '%s' を '%s' として取り込みます", path.name, cell, column
            )
        pk = self.config.tables[table_name].primary_key
        missing = [col for col in plan.missing if col != pk]
        if missing:
            logger.warning(
                "%s: config.py の列 %s が CSV のヘッダにありません(空のまま取り込みます)%s",
                path.name,
                ", ".join(missing),
                self._did_you_mean(plan, missing),
            )

    @staticmethod
    def _advance(state: TailState, consumed: bytes) -> None:
        state.offset += len(consumed)
        if len(state.head) < _WINDOW:
            state.head = (state.head + consumed)[:_WINDOW]
        state.tail = (state.tail + consumed)[-_WINDOW:]

    @staticmethod
    def _tail_reset_reason(fh, st: os.stat_result, state: TailState) -> str | None:
        """読み取り位置が使えなくなった理由(使えるなら None)。"""
        if state.offset == 0:
            return None
        if st.st_size < state.offset:
            return "ファイルが小さくなった"
        if state.head and _read_at(fh, 0, len(state.head)) != state.head:
            return "ファイルが差し替えられた"
        if state.tail and (
            _read_at(fh, state.offset - len(state.tail), len(state.tail)) != state.tail
        ):
            return "ファイルが書き換えられた"
        return None

    # ------------------------------------------------------------------
    # 内部: その他
    # ------------------------------------------------------------------
    def _notify_change(self) -> None:
        if self.on_change is None:
            return
        try:
            self.on_change()
        except Exception:  # noqa: BLE001 - 通知の失敗で取り込みを失敗にしない
            logger.warning("取り込み後の通知(on_change)に失敗しました", exc_info=True)

    @staticmethod
    def _kept_message(csv_path: Path) -> str:
        return f"{csv_path.name}: 有効行 0 件のため置換を中止(既存データを保持)"

    @staticmethod
    def _log_imported(result: TableImportResult) -> None:
        logger.info(
            "imported %s: %d rows (skipped %d)",
            result.table,
            result.inserted,
            result.skipped,
        )


def _decode_head(encoding: str, head: bytes, at_eof: bool) -> str:
    """ファイルの先頭 ``head`` を ``encoding`` で読む(末尾で切れた多バイト文字は許す)。"""
    return codecs.getincrementaldecoder(encoding)().decode(head, final=at_eof)


def _numbered_rows(reader) -> Iterator[tuple[int, list[str]]]:
    """CSV の行を (行番号, セル) で返す(行番号は取り込みの警告と同じ数え方)。

    CSV として読めない行は :class:`_BadRow` で返す。
    """
    line = 1
    it = iter(reader)
    while True:
        try:
            for raw in it:
                if not raw:
                    continue
                line += 1
                yield line, raw
        except csv.Error as exc:
            line += 1
            yield line, _BadRow(_unparsable_message(exc))
            continue
        return


def _all_blank(record: dict[str, Any]) -> bool:
    return all(
        value is None or (type(value) is str and not value.strip())
        for value in record.values()
    )


def _self_references(table) -> list[str]:
    """自分自身のテーブルを参照する外部キーの列(主キーを除く)。"""
    return [
        fk.parent.name
        for fk in table.foreign_keys
        if fk.column.table is table and not fk.parent.primary_key
    ]


def _defer_refs(
    rows: Iterable[dict[str, Any]],
    pk: str,
    refs: Sequence[str],
    pending: list[tuple[Any, dict[str, Any]]],
) -> Iterator[dict[str, Any]]:
    """自己参照の列を空にした行を返し、後で埋める値を ``pending`` に積む(D3-07)。"""
    for row in rows:
        key = row.get(pk)
        if key is not None and any(row.get(c) is not None for c in refs):
            pending.append((key, {c: row.get(c) for c in refs}))
            row = {**row, **dict.fromkeys(refs)}
        yield row


def _stage_table(conn, table: Table) -> Table:
    """``table`` と同じ列・主キーの一時テーブル(SQLite の ``temp`` スキーマ)を作る。

    名前は本体と同じにする(``temp.<名前>``。本体は ``main.<名前>`` で区別する)。
    """
    stage = Table(
        table.name,
        MetaData(),
        *(Column(c.name, c.type, primary_key=c.primary_key) for c in table.columns),
        schema="temp",
    )
    _drop_stage(conn, stage)
    stage.create(conn)
    return stage


def _drop_stage(conn, stage: Table) -> None:
    # SQLAlchemy の has_table は temp スキーマの表を見つけないため、IF EXISTS で消す
    name = conn.dialect.identifier_preparer.quote(stage.name)
    conn.exec_driver_sql(f"DROP TABLE IF EXISTS temp.{name}")


def _main_table(table: Table) -> Table:
    """一時テーブルと区別するため ``main.<名前>`` で参照する、``table`` と同じ列の表。"""
    return Table(
        table.name,
        MetaData(),
        *(Column(c.name, c.type, primary_key=c.primary_key) for c in table.columns),
        schema="main",
    )


def _drop_stages(conn, stages: Iterable[Table]) -> None:
    """一時テーブルを消す(接続はプールに戻って使い回されるため)。

    一時テーブルは本体と同じ名前のため、残ると、プールに戻った接続では名前を修飾しない
    SQL(画面・API・次の取り込み)が本体の代わりに一時テーブルを読み書きしてしまう。
    停止の指示(:class:`~monitor_app.cancel.TaskStopped`)で取り込みを打ち切った後でも
    消し、消せなければ接続ごと捨てる(一時テーブルは接続を閉じると消える)。
    """
    stages = list(stages)
    if not stages:
        return
    try:
        # 打ち切りの後片付けは、停止の指示が出ていても実行する
        with stop_scope(threading.Event()):
            if conn.in_transaction():
                conn.rollback()
            with conn.begin():
                for stage in stages:
                    _drop_stage(conn, stage)
    except BaseException as exc:
        conn.invalidate()
        if not isinstance(exc, Exception):
            raise
        logger.debug("一時テーブルを消せないため、接続を閉じます: %s", exc)


def _plan_key(plan: HeaderPlan) -> list[tuple[int, str]]:
    return [(idx, col) for idx, col, _conv in plan.cells]
