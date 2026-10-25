"""行の変換(services/rows.py)のテスト。

- 列ごとの変換関数の結果とエラーメッセージが ``coerce()`` と同じであること
- CSV のヘッダの対応付けが ``csv.DictReader`` と同じ解釈になること(重複列は後ろ、
  足りないセルは None、余分なセルは無視)と、表記ゆれの自動対応付け
- 外部ソースのレコード(mapping のドット記法・平坦化)
"""

from __future__ import annotations

import csv
import datetime as dt
import io
from decimal import Decimal

import pytest

from monitor_app import TableDef
from monitor_app.services.coercion import CoercionError, coerce
from monitor_app.services.rows import (
    ColumnError,
    RowConverter,
    converter_for,
    dot_get,
    flatten,
    normalize_name,
)

LOGICAL_TYPES = ["int", "float", "str", "bool", "date", "datetime"]

#: coerce() と結果・エラーメッセージを突き合わせる値(空白・全角・特殊な表記を含む)。
SAMPLES = [
    None,
    "",
    " ",
    "\t",
    "\u3000",
    "0",
    "1",
    "-7",
    "+5",
    " 42 ",
    "4\x1c",
    "\x1c4",
    "1_000",
    "１２３",
    "007",
    "1.5",
    " 2.5 ",
    "1e3",
    "nan",
    "inf",
    "-0",
    "abc",
    "1,234",
    "true",
    "True",
    " YES ",
    "no",
    "off",
    "maybe",
    "2024-01-15",
    " 2024-01-15 ",
    "2024/01/15",
    "2024/1/5",
    "2024-1-5",
    "2024-13-01",
    "2024-02-30",
    "2026-10-21 08:30",
    "2026-10-21T08:30:00",
    "2026/10/21 8:00",
    "2026-10-21T08:30:00.123456",
    "2026-10-21 25:00",
    "20240115",
    5,
    0,
    1.5,
    True,
    False,
    Decimal("2.5"),
    dt.date(2024, 1, 2),
    dt.datetime(2024, 1, 2, 3, 4, 5),
    [1, 2],
]


def _outcome(func, value):
    try:
        result = func(value)
    except CoercionError as exc:
        return ("error", str(exc))
    return ("ok", type(result), repr(result))  # repr: nan も比べられるように


@pytest.mark.parametrize("logical", LOGICAL_TYPES)
def test_converters_match_coerce_results_and_messages(logical):
    conv = converter_for(logical)
    for value in SAMPLES:
        expected = _outcome(lambda v: coerce(v, logical), value)
        assert _outcome(conv, value) == expected, (logical, value)


def test_row_converter_resolves_types_once_by_name_rules():
    tdef = TableDef(columns=["id", "line_id", "price", "temp", "note"])
    conv = RowConverter(tdef)
    assert conv.types == {
        "id": "int",
        "line_id": "int",
        "price": "float",
        "temp": "str",
        "note": "str",
    }
    assert conv.inferred_str_columns() == ["temp", "note"]
    assert conv.convert("price", " 1.5 ") == 1.5
    explicit = RowConverter(TableDef(columns={"id": "int", "temp": "str"}))
    assert explicit.inferred_str_columns() == []  # 型を明示した列は推論ではない


# ---------------------------------------------------------------------------
# CSV のヘッダ(csv.DictReader と同じ解釈)
# ---------------------------------------------------------------------------
def _dictreader_rows(text, columns):
    """従来の取り込み(DictReader で読み、設定の列だけ coerce)の結果。"""
    tdef = TableDef(columns=columns)
    out = []
    for raw in csv.DictReader(io.StringIO(text, newline="")):
        record = {}
        for key, value in raw.items():
            if key in tdef.column_names:
                record[key] = coerce(value, tdef.column_type(key))
        out.append(record)
    return out


@pytest.mark.parametrize(
    "text",
    [
        "a,b,a\n1,2\n1,2,3\n4,5,6,7\n",  # 重複列: 後ろのセル。足りなければ None
        "id,x,y\n1\n2,3\n3,4,5,6,7\n",  # 足りないセル・余分なセル
        "y,id,zz,x\n1,2,3,4\n",  # 並びが違う・未知の列
        "\ufeffid,x\n1,2\n",  # BOM が残った見出しは一致しない(従来どおり)
    ],
)
def test_header_plan_matches_dictreader(text):
    columns = {"id": "int", "a": "str", "b": "str", "x": "str", "y": "str"}
    plan = RowConverter(TableDef(columns=columns)).header_plan(
        next(csv.reader(io.StringIO(text, newline="")))
    )
    expected = _dictreader_rows(text, columns)
    rows = [r for r in csv.reader(io.StringIO(text, newline=""))][1:]
    got = [plan.convert(r) for r in rows if r]
    # 自動対応付けを除けば DictReader と同じ(BOM 付きの見出しは表記ゆれとして対応する)
    if "\ufeff" in text:
        assert plan.renamed == {"\ufeffid": "id"}
        assert got == [{"id": 1, "x": "2"}]
        assert expected == [{"x": "2"}]
    else:
        assert got == expected
        # dict の順(エラーを報告する列の順)も同じ
        assert [list(r) for r in got] == [list(r) for r in expected]


def test_header_plan_reports_first_failing_column_in_header_order():
    plan = RowConverter(TableDef(columns={"id": "int", "v": "float"})).header_plan(
        ["v", "id"]
    )
    with pytest.raises(CoercionError) as info:
        plan.convert(["bad-float", "bad-int"])
    assert str(info.value) == "'bad-float' を float に変換できません"


def test_header_plan_auto_maps_header_variants():
    columns = {"id": "int", "name": "str", "email": "str", "temp": "float"}
    plan = RowConverter(TableDef(columns=columns)).header_plan(
        [" ID ", "Name", "ｅｍａｉｌ", "e-mail", "温度"]
    )
    assert plan.renamed == {" ID ": "id", "Name": "name", "ｅｍａｉｌ": "email"}
    assert plan.matched == ["id", "name", "email"]
    assert plan.missing == ["temp"]
    assert plan.unknown == ["e-mail", "温度"]
    assert plan.convert(["7", "Ann", "a@x", "z", "1"]) == {
        "id": 7,
        "name": "Ann",
        "email": "a@x",
    }


def test_exact_header_wins_over_a_variant():
    plan = RowConverter(TableDef(columns={"id": "int", "name": "str"})).header_plan(
        ["id", "NAME", "name"]
    )
    assert plan.renamed == {}
    assert plan.unknown == ["NAME"]
    assert plan.convert(["1", "upper", "lower"]) == {"id": 1, "name": "lower"}


def test_ambiguous_variant_is_not_mapped():
    plan = RowConverter(TableDef(columns={"id": "int", "Code": "str", "code": "str"}))
    result = plan.header_plan(["id", "CODE"])
    assert result.unknown == ["CODE"]
    assert result.missing == ["Code", "code"]


def test_normalize_name():
    assert normalize_name(" Ｎａｍｅ\u3000") == "name"
    assert normalize_name("\ufeffID") == "id"


# ---------------------------------------------------------------------------
# 外部ソースのレコード
# ---------------------------------------------------------------------------
def test_from_record_without_mapping_uses_source_keys():
    conv = RowConverter(TableDef(columns={"id": "int", "name": "str", "v": "float"}))
    record = conv.from_record({"v": "1.5", "id": "3", "other": 9, "name": ["a", "b"]})
    assert record == {"v": 1.5, "id": 3, "name": "a b"}
    assert list(record) == ["v", "id", "name"]  # ソースのキーの順


def test_from_record_with_mapping_and_dot_notation():
    conv = RowConverter(
        TableDef(columns={"id": "int", "name": "str", "ts": "str", "v": "float"}),
        mapping={"id": "event.id", "ts": "times.0", "v": "missing.key", "zz": "x"},
    )
    record = conv.from_record({"event": {"id": "5"}, "times": ["t0", "t1"]})
    assert record == {"id": 5, "ts": "t0", "v": None}  # mapping に無い列は読まない


def test_from_record_reports_the_failing_column():
    conv = RowConverter(TableDef(columns={"id": "int", "v": "float"}))
    with pytest.raises(ColumnError) as info:
        conv.from_record({"id": "1", "v": "x"})
    assert info.value.column == "v"
    assert str(info.value) == "'x' を float に変換できません"
    assert isinstance(info.value, CoercionError)


def test_dot_get_and_flatten():
    data = {"a": [{"b": 1}, {"b": 2}]}
    assert dot_get(data, "a.1.b") == 2
    for bad in ("a.5.b", "a.x", "z", "a.0.b.c"):
        with pytest.raises(KeyError):
            dot_get(data, bad)
    assert flatten({"k": "日本"}) == '{"k": "日本"}'
    assert flatten([1, "x"]) == "1 x"
    assert flatten(3) == 3


def test_connectors_keep_the_old_helper_names():
    from monitor_app.services import connectors

    assert connectors._dot_get is dot_get
    assert connectors._flatten is flatten
