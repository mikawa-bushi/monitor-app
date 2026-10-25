"""ビューのエクスポート(CSV / xlsx)のファイル名と BOM のテスト(FE-06)。"""

from __future__ import annotations

import io
import re
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from monitor_app import MonitorConfig, TableDef, ViewDef
from monitor_app.main import create_app
from monitor_app.settings.runtime import AppSettings

BOM = b"\xef\xbb\xbf"


@pytest.fixture
def client() -> TestClient:
    config = MonitorConfig(
        tables={"t": TableDef(columns={"id": "int", "name": "str"})},
        views={
            "生産実績": ViewDef(query="SELECT id, name FROM t ORDER BY id"),
            "users_view": ViewDef(query="SELECT id, name FROM t ORDER BY id"),
            'line 1 "A"': ViewDef(query="SELECT id, name FROM t ORDER BY id"),
            "ライン1_実績": ViewDef(query="SELECT id, name FROM t ORDER BY id"),
        },
    )
    app = create_app(config, AppSettings(_env_file=None, database_url="sqlite://"))
    test_client = TestClient(app)
    test_client.post("/api/ingest/t", json=[{"name": "山田"}, {"name": "Smith"}])
    return test_client


def _filename_star(disposition: str) -> str | None:
    match = re.search(r"filename\*=UTF-8''([^;\s]+)", disposition)
    return unquote(match.group(1)) if match else None


def _ascii_filename(disposition: str) -> str:
    match = re.search(r'filename="([^"]*)"', disposition)
    assert match, disposition
    return match.group(1)


def test_japanese_view_name_uses_rfc5987_filename_with_ascii_fallback(client):
    res = client.get("/api/views/生産実績/export")
    assert res.status_code == 200
    disposition = res.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    assert _filename_star(disposition) == "生産実績.csv"
    assert _ascii_filename(disposition) == "export.csv"
    assert disposition.isascii()


def test_ascii_view_name_keeps_todays_header(client):
    res = client.get("/api/views/users_view/export")
    assert res.headers["content-disposition"] == 'attachment; filename="users_view.csv"'


def test_unsafe_ascii_characters_are_not_put_in_the_quoted_filename(client):
    disposition = client.get('/api/views/line 1 "A"/export').headers[
        "content-disposition"
    ]
    assert _ascii_filename(disposition) == "line_1_A.csv"
    assert _filename_star(disposition) == 'line 1 "A".csv'


def test_mixed_names_keep_their_ascii_part(client):
    disposition = client.get("/api/views/ライン1_実績/export").headers[
        "content-disposition"
    ]
    assert _filename_star(disposition) == "ライン1_実績.csv"
    assert _ascii_filename(disposition).endswith(".csv")
    assert _ascii_filename(disposition).isascii()


def test_default_csv_bytes_are_unchanged(client):
    res = client.get("/api/views/users_view/export")
    assert res.content.startswith(b"id,name\r\n")  # BOM なし(スクリプト向けの既定)
    assert res.headers["content-type"] == "text/csv; charset=utf-8"
    assert res.text.splitlines() == ["id,name", "1,山田", "2,Smith"]


@pytest.mark.parametrize("value", ["1", "true"])
def test_bom_option_prefixes_a_utf8_bom(client, value):
    res = client.get("/api/views/生産実績/export", params={"bom": value})
    assert res.status_code == 200
    assert res.content.startswith(BOM + b"id,name\r\n")
    assert res.content[len(BOM) :].decode("utf-8").splitlines()[1] == "1,山田"


def test_bom_false_and_format_csv_explicit(client):
    res = client.get(
        "/api/views/users_view/export", params={"format": "csv", "bom": "false"}
    )
    assert res.content.startswith(b"id,name")


def test_xlsx_export_uses_the_same_filename_rules(client):
    openpyxl = pytest.importorskip("openpyxl")
    res = client.get("/api/views/生産実績/export", params={"format": "xlsx"})
    assert res.status_code == 200
    assert _filename_star(res.headers["content-disposition"]) == "生産実績.xlsx"
    sheet = openpyxl.load_workbook(io.BytesIO(res.content)).active
    assert [c.value for c in sheet[1]] == ["id", "name"]
    assert sheet["B2"].value == "山田"


def test_openapi_documents_the_bom_option_and_keeps_format(client):
    params = {
        p["name"]: p
        for p in client.app.openapi()["paths"]["/api/views/{view_name}/export"]["get"][
            "parameters"
        ]
    }
    assert params["format"]["schema"]["default"] == "csv"
    assert params["bom"]["schema"]["type"] == "boolean"
    assert params["bom"]["schema"]["default"] is False
    assert params["bom"]["required"] is False
