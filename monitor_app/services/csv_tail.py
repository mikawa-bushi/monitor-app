"""追記監視(:meth:`~monitor_app.services.importer.CsvImporter.tail_append`)の
バイト位置の扱い: 読み取り位置(:class:`TailState`)と、完全な行だけを切り出す関数。

CSV の行の区切りは引用符の外の改行だけ(``csv`` モジュールと同じ解釈)。改行・引用符の
バイトは、追記監視で使える文字コード(:data:`~monitor_app.services.csv_encoding.TAILABLE_CODECS`)
の多バイト文字の途中に現れないため、文字コードを決める前のバイト列のまま区切りを探せる。
DB には触れない(取り込みは importer が行う)。
"""

from __future__ import annotations

import csv
import io
import json
import re
import zlib
from dataclasses import dataclass, field
from typing import Any

from .rows import HeaderPlan

#: 追記監視が 1 回に読むバイト数(大きなファイルでもメモリを一定に保つ)。
_TAIL_BLOCK = 1 << 20
#: ファイルの差し替え・書き換えの検出に覚えておく先頭/直前のバイト数。
_WINDOW = 4096
_EOL = re.compile(rb"\r\n|\r|\n")
#: 引用符の中で行が終わっているか(書きかけの行か)を確かめるための番兵の行。
_SENTINEL = "\uffff\n"


@dataclass
class TailState:
    """追記監視(:meth:`CsvImporter.tail_append`)のファイルごとの読み取り位置。"""

    #: 次に読むバイト位置(取り込み済みの最後の完全な行の直後)。
    offset: int = 0
    #: ヘッダ(ファイル先頭の行)。
    header: list[str] | None = None
    #: ファイルの識別子 (st_dev, st_ino)。
    identity: tuple[int, int] | None = None
    #: 読み取りに使っている文字コード(``auto`` で判別したもの)。
    encoding: str | None = None
    #: 読み取り済みのデータ行数(参考値)。
    records: int = 0
    #: ファイル先頭と読み取り位置の直前のバイト列(差し替え・書き換えの検出用)。
    head: bytes = b""
    tail: bytes = b""
    #: 追記の読み取りに向かない文字コード(UTF-16 など)なら毎回ファイル全体を読む。
    full_read: bool = False
    plan: HeaderPlan | None = field(default=None, repr=False)
    #: 読み取りに使っている文字コードで読めた ASCII 以外の行の数から、読めなかった行の
    #: 数を引いた値。正のあいだは、読めない行があってもその行だけを読み飛ばし、別の
    #: 文字コードに切り替えない(UTF-8 の CSV の壊れた 1 行で以後を cp932 として読まない
    #: — D3-01)。
    evidence: int = 0
    #: 読み取り位置を保存する ``_ingest_state`` のキー(None なら保存しない — D3-03)。
    key: str | None = None

    def reset(self) -> None:
        """ファイルの先頭から読み直す状態にする。"""
        self.offset = 0
        self.header = None
        self.encoding = None
        self.records = 0
        self.head = b""
        self.tail = b""
        self.plan = None
        self.evidence = 0


class _BadRow(list):
    """読めなかった行の代わりに置く行(``message`` は警告に載せる理由)。

    空行(取り込みは数えずに飛ばす)と区別するため、空のセルを 1 つ持つ。
    """

    def __init__(self, message: str) -> None:
        super().__init__(("",))
        self.message = message


def _unparsable_message(exc: csv.Error) -> str:
    return f"CSV として読めないため、この行は取り込みません({exc})"


def _count_lines(data: bytes, end: int) -> int:
    return (
        data.count(b"\n", 0, end)
        + data.count(b"\r", 0, end)
        - data.count(b"\r\n", 0, end)
    )


def _complete_prefix(data: bytes) -> tuple[int, int]:
    """``data``(行の先頭から始まるバイト列)のうち、完全な行だけの部分の長さと行数。

    改行は引用符の外にあるものだけを行の区切りとみなす(``csv`` モジュールと同じ
    解釈)。引用符を含む部分は latin-1(1 バイト = 1 文字)として ``csv`` で区切りを
    確かめる(改行・引用符のバイトは対応する文字コードの多バイト文字に現れない)。
    """
    cut = max(data.rfind(b"\n"), data.rfind(b"\r")) + 1
    if cut == 0:
        return 0, 0
    if data.find(b'"', 0, cut) < 0:
        return cut, _count_lines(data, cut)
    text = data[:cut].decode("latin-1").replace("\x00", "")
    try:
        count, consumed = _scan_complete(data, cut, text, None)
    except csv.Error:
        rows, consumed = _parse_complete_safe(data, cut, text)
        count = len(rows)
    return consumed, count


def _parse_complete(data: bytes, cut: int, text: str) -> tuple[list[list[str]], int]:
    """``data[:cut]``(``text`` はそのデコード結果)を行に分け、最後の行が引用符の中で
    終わっていれば(書きかけ)その行を除く。戻り値は (完全な行, そのバイト長)。"""
    if data.find(b'"', 0, cut) < 0:
        return list(csv.reader(io.StringIO(text, newline=""))), cut
    rows: list[list[str]] = []
    _count, consumed = _scan_complete(data, cut, text, rows)
    return rows, consumed


def _scan_complete(
    data: bytes, cut: int, text: str, rows: list[list[str]] | None
) -> tuple[int, int]:
    """引用符を含む ``data[:cut]`` の完全な行を数え(``rows`` があれば足し)、
    (行数, そのバイト長) を返す。"""
    ends = [m.end() for m in _EOL.finditer(data, 0, cut)]
    lines = len(ends)
    count = done = 0
    reader = csv.reader(io.StringIO(text + _SENTINEL, newline=""))
    for row in reader:
        if reader.line_num > lines:
            # 番兵の行まで読んだ: 番兵だけの行なら全行が完全、そうでなければ最後の行は
            # 引用符が閉じていない(書きかけ)。
            break
        if rows is not None:
            rows.append(row)
        count += 1
        done = reader.line_num
    return count, (ends[done - 1] if done else 0)


def _parse_complete_safe(
    data: bytes, cut: int, text: str
) -> tuple[list[list[str]], int]:
    """:func:`_parse_complete` と同じだが、CSV として読めない行(長すぎるセルなど)を
    :class:`_BadRow` に置き換えて続きを読む(追記監視を止めない — D3-09)。"""
    ends = [m.end() for m in _EOL.finditer(data, 0, cut)]
    lines = len(ends)
    rows: list[list[str]] = []
    done = 0
    reader = csv.reader(io.StringIO(text + _SENTINEL, newline=""))
    while True:
        try:
            row = next(reader)
        except StopIteration:
            break
        except csv.Error as exc:
            if reader.line_num > lines:
                break
            rows.append(_BadRow(_unparsable_message(exc)))
            done = reader.line_num
            continue
        if reader.line_num > lines:
            break
        rows.append(row)
        done = reader.line_num
    return rows, (ends[done - 1] if done else 0)


def _read_at(fh, start: int, size: int) -> bytes:
    fh.seek(start)
    return fh.read(size)


def _whole_lines(data: bytes, *, from_start: bool, to_end: bool) -> bytes:
    """``data`` から、途中で切れた最初・最後の行を除いた部分(行の区切りはバイトで探す)。"""
    if not from_start:
        first = _EOL.search(data)
        data = data[first.end() :] if first else b""
    if not to_end:
        data = data[: max(data.rfind(b"\n"), data.rfind(b"\r")) + 1]
    return data


def _first_record_end(data: bytes) -> int:
    """``data``(ファイルの先頭)のうち、最初の行(ヘッダ)の終わりのバイト位置。"""
    first_eol = _EOL.search(data)
    if first_eol is None:
        return len(data)
    if data.find(b'"', 0, first_eol.end()) < 0:
        return first_eol.end()
    # 引用符の中に改行がある見出し: csv で 1 行分を読み、その行数だけ進む。
    ends = [m.end() for m in _EOL.finditer(data)]
    reader = csv.reader(
        io.StringIO(data.decode("latin-1").replace("\x00", ""), newline="")
    )
    try:
        next(reader, None)
    except csv.Error:
        return first_eol.end()
    return ends[min(reader.line_num, len(ends)) - 1]


# ---------------------------------------------------------------------------
# 読み取り位置の保存(``_ingest_state`` のカーソル。VARCHAR(255) に収まる JSON — D3-03)
#
# 先頭と読み取り位置の直前のバイト列は、長さと CRC32 だけを保存する(再起動したとき、
# ファイルのその部分が変わっていなければ続きから読む)。
# ---------------------------------------------------------------------------
def _window_crc(data: bytes) -> list[int]:
    return [len(data), zlib.crc32(data)]


def tail_cursor(state: TailState) -> str:
    """読み取り位置を保存する文字列(JSON)。"""
    payload: dict[str, Any]
    if state.full_read:
        payload = {"f": 1, "r": state.records}
    else:
        payload = {
            "o": state.offset,
            "r": state.records,
            "e": state.encoding,
            "v": state.evidence,
            "h": _window_crc(state.head),
            "t": _window_crc(state.tail),
        }
    return json.dumps(payload, separators=(",", ":"))


def parse_tail_cursor(
    raw: str | None, key: str | None = None
) -> tuple[TailState, dict[str, tuple[int, int]]] | None:
    """:func:`tail_cursor` の逆。(読み取り位置, 窓の (長さ, CRC32))。形式が違えば None。"""
    try:
        data = json.loads(raw) if raw else None
        if not isinstance(data, dict):
            return None
        if data.get("f"):
            return TailState(full_read=True, records=int(data["r"]), key=key), {}
        state = TailState(
            offset=int(data["o"]),
            records=int(data["r"]),
            encoding=data.get("e"),
            evidence=int(data.get("v", 0)),
            key=key,
        )
        windows = {name: (int(data[name][0]), int(data[name][1])) for name in "ht"}
    except (AttributeError, KeyError, TypeError, ValueError, IndexError):
        return None
    return state, windows


def restore_windows(
    fh, size: int, state: TailState, windows: dict[str, tuple[int, int]]
) -> str | None:
    """保存した窓がファイルと一致すれば ``state`` に読み込み、違えばその理由を返す。"""
    if size < state.offset:
        return "ファイルが小さくなっている"
    for name, reason in (
        ("h", "ファイルが差し替えられている"),
        ("t", "ファイルが書き換えられている"),
    ):
        length, crc = windows[name]
        begin = 0 if name == "h" else state.offset - length
        window = _read_at(fh, begin, length) if begin >= 0 else b""
        if len(window) != length or zlib.crc32(window) != crc:
            return reason
        if name == "h":
            state.head = window
        else:
            state.tail = window
    return None
