"""``services.coercion.coerce`` の単体テスト。

CSV 取り込みと API 入力の双方が通るデータ整合性の要なので、全論理型の
変換成功・失敗・境界(空文字 / None / 前後空白)を網羅する。
"""

from __future__ import annotations

import datetime as dt

import pytest

from monitor_app.services.coercion import CoercionError, coerce


# ---------------------------------------------------------------------------
# None / 空文字の扱い
# ---------------------------------------------------------------------------
def test_none_returns_none_for_any_type():
    assert coerce(None, "int") is None
    assert coerce(None, "str") is None
    assert coerce(None, "date") is None


@pytest.mark.parametrize("logical", ["int", "float", "bool", "date"])
def test_empty_string_becomes_none_for_non_str(logical):
    assert coerce("", logical) is None
    assert coerce("   ", logical) is None  # 空白のみも空扱い


def test_empty_string_stays_empty_for_str():
    # str 型だけは空文字を None にせずそのまま保持する。
    assert coerce("", "str") == ""
    assert coerce("   ", "str") == "   "


# ---------------------------------------------------------------------------
# int / float
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "value,expected",
    [("42", 42), (" 42 ", 42), ("-7", -7), (0, 0), (100, 100)],
)
def test_int_ok(value, expected):
    assert coerce(value, "int") == expected


@pytest.mark.parametrize("value", ["abc", "1.5", "1e3", "12x"])
def test_int_invalid_raises(value):
    with pytest.raises(CoercionError):
        coerce(value, "int")


@pytest.mark.parametrize(
    "value,expected",
    [("3.14", 3.14), (" -2.5 ", -2.5), ("1e3", 1000.0), ("10", 10.0)],
)
def test_float_ok(value, expected):
    assert coerce(value, "float") == expected


@pytest.mark.parametrize("value", ["abc", "1.2.3", "--3"])
def test_float_invalid_raises(value):
    with pytest.raises(CoercionError):
        coerce(value, "float")


# ---------------------------------------------------------------------------
# bool
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("value", ["1", "true", "TRUE", "Yes", "y", "t", "on", " on "])
def test_bool_truthy(value):
    assert coerce(value, "bool") is True


@pytest.mark.parametrize("value", ["0", "false", "No", "n", "f", "off", "OFF"])
def test_bool_falsy(value):
    assert coerce(value, "bool") is False


@pytest.mark.parametrize("value", ["maybe", "2", "y&n"])
def test_bool_invalid_raises_coercion_error(value):
    # bool は明示的に CoercionError を投げ、その re-raise 経路も通す。
    with pytest.raises(CoercionError):
        coerce(value, "bool")


# ---------------------------------------------------------------------------
# date
# ---------------------------------------------------------------------------
def test_date_from_isoformat_string():
    assert coerce("2024-01-15", "date") == dt.date(2024, 1, 15)
    assert coerce(" 2024-01-15 ", "date") == dt.date(2024, 1, 15)


def test_date_passthrough_for_date_and_datetime():
    d = dt.date(2024, 1, 15)
    assert coerce(d, "date") is d
    ts = dt.datetime(2024, 1, 15, 9, 30)
    assert coerce(ts, "date") is ts


# "2024/01/15" は Excel の日付の表記として受け付けるようになった(CRIT-12)。
# スラッシュ区切りでも存在しない日付は変換できない。
@pytest.mark.parametrize("value", ["not-a-date", "2024/13/15", "2024-13-99"])
def test_date_invalid_raises(value):
    with pytest.raises(CoercionError):
        coerce(value, "date")


# ---------------------------------------------------------------------------
# str(既定)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "value,expected",
    [("hello", "hello"), (123, "123"), (3.5, "3.5"), (True, "True")],
)
def test_str_stringifies(value, expected):
    assert coerce(value, "str") == expected
