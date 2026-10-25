"""CSV の文字コードの決まり(取り込み・追記監視・点検・推論が共有する)。

``MONITOR_CSV_ENCODING`` は ``auto``(既定)か固定の文字コード名。``auto`` は UTF-8
(BOM の有無は問わない)で読み、読めなければ ``MONITOR_CSV_ENCODING_FALLBACK`` の
文字コード(既定: cp932)を順に試す(DX-04)。

ここには SQLAlchemy などの重い依存を置かない(点検・推論からも軽く使えるように)。
"""

from __future__ import annotations

import codecs
import re
from collections.abc import Iterable, Sequence

from ..exceptions import CsvEncodingError

#: 自動判別を表す設定値。
AUTO = "auto"
#: ``auto`` で最初に試す文字コード(BOM があれば読み飛ばす UTF-8)。
UTF8_FIRST = "utf-8-sig"
#: 追記監視でバイト位置から読める文字コード(改行・引用符・カンマのバイトが
#: 多バイト文字の途中に現れない ASCII 互換のもの)。
TAILABLE_CODECS = frozenset(
    {
        "ascii",
        "utf-8",
        "utf-8-sig",
        "cp932",
        "shift_jis",
        "shift_jis_2004",
        "shift_jisx0213",
        "euc_jp",
        "euc_jis_2004",
        "euc_jisx0213",
        "iso8859-1",
        "cp1252",
    }
)
#: ``errors="surrogateescape"`` で読んだときに、読めなかったバイトが化けた文字。
_ESCAPED = re.compile("[\udc80-\udcff]")
_BAD_NAME_HINT = (
    "MONITOR_CSV_ENCODING / MONITOR_CSV_ENCODING_FALLBACK の値を確認してください"
    "(例: auto, utf-8-sig, cp932)"
)


def is_auto(encoding: str) -> bool:
    """設定値が自動判別(``auto``)か。"""
    return encoding.strip().lower() == AUTO


def split_encodings(value: str | Sequence[str] | None) -> list[str]:
    """``"euc-jp, cp932"`` のようなカンマ区切り(またはリスト)を名前のリストにする。"""
    if value is None:
        return []
    items = value.split(",") if isinstance(value, str) else list(value)
    return [item.strip() for item in items if item and item.strip()]


def codec_name(encoding: str) -> str:
    """文字コードの正式名(``utf8`` → ``utf-8``)。使えない名前は :class:`CsvEncodingError`。"""
    try:
        return codecs.lookup(encoding).name
    except LookupError:
        raise CsvEncodingError(
            f"文字コード '{encoding}' は使えません", _BAD_NAME_HINT
        ) from None


def is_known(encoding: str) -> bool:
    """文字コード名として使えるか。"""
    try:
        codecs.lookup(encoding)
    except LookupError:
        return False
    return True


def candidates(
    encoding: str,
    fallback: str | Sequence[str] | None = None,
    *,
    strict: bool = True,
) -> list[str]:
    """CSV を読むときに試す文字コードの順。

    ``auto`` なら ``utf-8-sig`` → 予備(``fallback``)の順で、同じ文字コードの別名は
    1 つにまとめる。固定の文字コードならそれだけ。``strict=True`` は使えない名前を
    :class:`CsvEncodingError` にし、``strict=False`` は黙って除く(点検は名前の誤りを
    別に報告するため)。
    """
    if not is_auto(encoding):
        if strict:
            codec_name(encoding)
        elif not is_known(encoding):
            return []
        return [encoding]
    chosen: list[str] = []
    seen: set[str] = set()
    for name in [UTF8_FIRST, *split_encodings(fallback)]:
        if strict:
            key = codec_name(name)
        elif is_known(name):
            key = codecs.lookup(name).name
        else:
            continue
        if key not in seen:
            seen.add(key)
            chosen.append(name)
    return chosen


def is_utf8(encoding: str) -> bool:
    """UTF-8 の仲間(``utf-8`` / ``utf-8-sig`` / ``utf8`` など)か。"""
    try:
        return codecs.lookup(encoding).name in ("utf-8", "utf-8-sig")
    except LookupError:
        return False


def label(encoding: str) -> str:
    """表示用の名前(UTF-8 の仲間は ``UTF-8``、それ以外は指定どおり)。"""
    return "UTF-8" if is_utf8(encoding) else encoding


def is_tailable(encodings: Iterable[str]) -> bool:
    """すべての文字コードが追記監視(バイト位置からの読み取り)に向くか。"""
    try:
        return all(codec_name(e) in TAILABLE_CODECS for e in encodings)
    except CsvEncodingError:
        return False


def has_escaped(text: str) -> bool:
    """``surrogateescape`` で読んだ文字列に、読めなかったバイトが含まれるか。"""
    return not text.isascii() and _ESCAPED.search(text) is not None


def line_evidence(data: bytes, encoding: str) -> int:
    """``data`` を ``encoding`` で読んだときの「その文字コードらしさ」。

    ASCII 以外を含み、その文字コードとして読める行の数から、読めない行の数を引いた値。
    正なら(壊れたバイトが混じっていても)その文字コードのファイル、0 以下ならたまたま
    読めた別の文字コードのファイルとみなす(D3-01, D3-02)。``data`` は行の先頭から
    始まり行の終わりで終わること(途中で切れた多バイト文字を読めない行と数えないため)。
    """
    good = bad = 0
    for line in data.decode(encoding, "surrogateescape").splitlines():
        if line.isascii():
            continue
        if _ESCAPED.search(line) is None:
            good += 1
        else:
            bad += 1
    return good - bad
