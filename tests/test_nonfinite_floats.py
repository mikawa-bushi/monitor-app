"""float の列の ``NaN`` / ``inf``(センサーの欠測値)は、どの DB でも空(NULL)にする(D3-14)。

以前は SQLite が NaN を NULL・inf をそのまま、PostgreSQL は NaN を保存して AVG・MAX が
NaN になり、MySQL は取り込み全体が失敗した。実サーバーでの確認は
tests/server_db/test_nonfinite_server.py。
"""

from __future__ import annotations

import json
import math

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from monitor_app import MonitorConfig, TableDef
from monitor_app.db.engine import Database
from monitor_app.db.registry import TableRegistry
from monitor_app.main import create_app
from monitor_app.services.coercion import coerce
from monitor_app.services.importer import CsvImporter
from monitor_app.services.rows import converter_for
from monitor_app.settings.runtime import AppSettings

CSV = "id,temp\n1,21.5\n2,NaN\n3,inf\n4,-Infinity\n5,22.0\n6,nan\n"


def _config() -> MonitorConfig:
    return MonitorConfig(tables={"t": TableDef(columns={"id": "int", "temp": "float"})})


@pytest.mark.parametrize(
    "raw", ["NaN", "nan", "inf", "-inf", " Infinity ", "-Infinity"]
)
def test_nonfinite_text_becomes_none(raw):
    assert coerce(raw, "float") is None
    assert converter_for("float")(raw) is None


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_nonfinite_numbers_become_none(value):
    assert coerce(value, "float") is None
    assert converter_for("float")(value) is None


def test_finite_values_are_unchanged():
    assert converter_for("float")("21.5") == 21.5
    assert converter_for("float")("1e308") == 1e308
    assert coerce(" -0.0 ", "float") == 0.0


def test_csv_import_stores_missing_values(tmp_path):
    (tmp_path / "t.csv").write_text(CSV)
    db = Database(f"sqlite:///{(tmp_path / 'a.db').as_posix()}")
    try:
        report = CsvImporter(
            _config(), TableRegistry(_config()), db, tmp_path
        ).import_all()
        assert report.total_inserted == 6
        with db.readonly() as conn:
            rows = dict(conn.execute(text("SELECT id, temp FROM t")).all())
            avg = conn.execute(text("SELECT AVG(temp) FROM t")).scalar()
    finally:
        db.dispose()
    assert rows == {1: 21.5, 2: None, 3: None, 4: None, 5: 22.0, 6: None}
    assert avg == pytest.approx(21.75)


def test_api_input_with_nan_is_stored_as_null_and_served_as_json(tmp_path):
    app = create_app(
        _config(),
        AppSettings(database_url=f"sqlite:///{(tmp_path / 'a.db').as_posix()}"),
    )
    body = (
        '[{"id": 1, "temp": NaN}, {"id": 2, "temp": Infinity}, {"id": 3, "temp": 1.5}]'
    )
    with TestClient(app) as client:
        res = client.post(
            "/api/ingest/t", content=body, headers={"Content-Type": "application/json"}
        )
        assert res.status_code == 200, res.text
        created = client.post("/api/tables/t", json={"temp": "NaN"})
        listed = client.get("/api/tables/t")
    assert created.status_code == 201
    assert created.json()["data"]["temp"] is None
    data = json.loads(listed.text)["data"]
    assert [r["temp"] for r in data] == [None, None, 1.5, None]
