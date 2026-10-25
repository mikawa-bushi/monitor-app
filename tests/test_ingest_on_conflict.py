"""インジェスト API の主キー衝突時の扱い(CRIT-08: ``?on_conflict=error|ignore|update``)。

PLC などが同じバッチを再送したとき、既定では 409 の JSON でバッチ全体を拒否し
(以前は text/plain の 500)、``ignore`` なら新しい行だけを取り込み、``update`` なら
既存の行を上書きする。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from monitor_app import MonitorConfig, TableDef, ViewDef
from monitor_app.main import create_app
from monitor_app.settings.runtime import AppSettings


@pytest.fixture
def client() -> TestClient:
    config = MonitorConfig(
        tables={
            "samples": TableDef(
                columns={"id": "int", "line": "str", "temp": "float"},
                primary_key="id",
            )
        },
        views={"latest": ViewDef(query="SELECT * FROM samples ORDER BY id")},
    )
    app = create_app(config, AppSettings(_env_file=None, database_url="sqlite://"))
    return TestClient(app, raise_server_exceptions=False)


def _batch(ids) -> list[dict]:
    return [{"id": i, "line": "L1", "temp": 20.0 + i} for i in ids]


def _ids(client) -> list[int]:
    return [row["id"] for row in client.get("/api/tables/samples").json()["data"]]


def test_default_response_is_unchanged(client):
    res = client.post("/api/ingest/samples", json=_batch(range(1, 11)))
    assert res.status_code == 200
    assert res.json() == {"success": True, "inserted": 10}


def test_retried_batch_is_rejected_with_a_409_json_and_nothing_is_inserted(client):
    """CRIT-08 の再現: 10 行の後に、同じ 10 行 + 新しい 1 行を再送する。"""
    client.post("/api/ingest/samples", json=_batch(range(1, 11)))

    res = client.post("/api/ingest/samples", json=_batch(range(1, 12)))

    assert res.status_code == 409
    assert res.headers["content-type"] == "application/json"
    detail = res.json()["detail"]
    assert detail["code"] == "CONFLICT"
    assert "1, 2, 3, 4, 5, 6, 7, 8, 9, 10" in detail["message"]
    assert "on_conflict=ignore" in detail["message"]
    assert _ids(client) == list(range(1, 11))  # バッチ全体を取り消す(従来どおり)


def test_many_duplicates_are_summarised(client):
    client.post("/api/ingest/samples", json=_batch(range(1, 31)))
    res = client.post("/api/ingest/samples", json=_batch(range(1, 31)))
    assert res.status_code == 409
    assert "ほか 20 件" in res.json()["detail"]["message"]


def test_duplicate_keys_within_one_batch_are_reported(client):
    res = client.post("/api/ingest/samples", json=_batch([1, 2, 2]))
    assert res.status_code == 409
    assert "重複しています: 2" in res.json()["detail"]["message"]
    assert _ids(client) == []


def test_ignore_inserts_only_the_new_rows(client):
    client.post("/api/ingest/samples", json=_batch(range(1, 11)))
    res = client.post(
        "/api/ingest/samples",
        params={"on_conflict": "ignore"},
        json=_batch(range(1, 12)),
    )
    assert res.status_code == 200
    assert res.json() == {"success": True, "inserted": 1, "skipped": 10}
    assert _ids(client) == list(range(1, 12))
    # 何度再送しても重複しない
    res = client.post(
        "/api/ingest/samples",
        params={"on_conflict": "ignore"},
        json=_batch(range(1, 12)),
    )
    assert res.json() == {"success": True, "inserted": 0, "skipped": 11}


def test_update_overwrites_existing_rows(client):
    client.post("/api/ingest/samples", json=_batch([1, 2]))
    res = client.post(
        "/api/ingest/samples",
        params={"on_conflict": "update"},
        json=[{"id": 2, "temp": 99.5}, {"id": 3, "line": "L2", "temp": 1.0}],
    )
    assert res.json() == {"success": True, "inserted": 1, "updated": 1}
    rows = {r["id"]: r for r in client.get("/api/tables/samples").json()["data"]}
    assert rows[2] == {"id": 2, "line": "L1", "temp": 99.5}  # 送らなかった列は残る
    assert rows[3] == {"id": 3, "line": "L2", "temp": 1.0}


def test_views_see_new_rows_after_an_ignore_ingest(client):
    assert client.get("/api/views/latest").json()["data"] == []
    client.post(
        "/api/ingest/samples", params={"on_conflict": "ignore"}, json=_batch([1])
    )
    assert [r["id"] for r in client.get("/api/views/latest").json()["data"]] == [1]


@pytest.mark.parametrize("mode", ["error", "ignore", "update"])
def test_invalid_values_still_reject_the_whole_batch(client, mode):
    res = client.post(
        "/api/ingest/samples",
        params={"on_conflict": mode},
        json=[{"id": 1, "temp": 1}, {"id": 2, "temp": "hot"}],
    )
    assert res.status_code == 422
    assert res.json()["detail"]["code"] == "INVALID_PAYLOAD"
    assert _ids(client) == []


def test_unknown_mode_is_a_validation_error(client):
    res = client.post(
        "/api/ingest/samples", params={"on_conflict": "replace"}, json=_batch([1])
    )
    assert res.status_code == 422


def test_records_with_different_columns_keep_all_values(client):
    """以前は 1 件目に無い列の値が黙って捨てられていた(executemany の仕様)。"""
    res = client.post(
        "/api/ingest/samples",
        json=[{"id": 1, "line": "L1"}, {"id": 2, "line": "L2", "temp": 30.5}],
    )
    assert res.status_code == 200
    rows = client.get("/api/tables/samples").json()["data"]
    assert rows == [
        {"id": 1, "line": "L1", "temp": None},
        {"id": 2, "line": "L2", "temp": 30.5},
    ]


def test_records_missing_columns_of_the_first_record_are_accepted(client):
    """以前は 2 件目に 1 件目の列が無いと StatementError(text/plain の 500)だった。"""
    res = client.post(
        "/api/ingest/samples",
        json=[{"id": 1, "line": "L1", "temp": 1.0}, {"id": 2, "temp": 2.0}],
    )
    assert res.status_code == 200
    assert _ids(client) == [1, 2]


def test_openapi_documents_the_new_parameter(client):
    op = client.get("/openapi.json").json()["paths"]["/api/ingest/{table_name}"]["post"]
    (param,) = [p for p in op["parameters"] if p["name"] == "on_conflict"]
    assert param["in"] == "query"
    assert param["required"] is False
    assert param["schema"]["default"] == "error"
    assert param["schema"]["enum"] == ["error", "ignore", "update"]
    assert "409" in op["responses"]


def test_ingest_records_keeps_returning_the_inserted_count(client):
    service = client.app.state.crud_service
    assert service.ingest_records("samples", _batch([1, 2])) == 2
