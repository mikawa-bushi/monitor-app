"""ビューの応答(1 回だけ作る JSON・ETag/304・max_rows)のテスト。

JSON はスナップショットごとに pydantic-core で 1 回だけ作り、GET・一括取得・SSE が
同じバイト列を使う。従来の GET(``response_model`` による変換)と同じ JSON になる
ことを、SQLite の型と PostgreSQL / MySQL が返す型(Decimal・日時など)で確かめる。
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import uuid
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from monitor_app import AlertRule, MonitorConfig, TableDef, ViewDef
from monitor_app.api.routers.views import _sse_event_stream
from monitor_app.api.schemas import ViewDataResponse
from monitor_app.main import create_app
from monitor_app.services import view_service as view_service_mod
from monitor_app.services.view_service import ViewSnapshot, dumps, is_time_relative
from monitor_app.settings.runtime import AppSettings

FAKE_COLUMNS = ["id", "amount", "ts", "day", "at", "elapsed", "blob", "uid", "note"]
#: PostgreSQL / MySQL のドライバが返す型(SQLite の text() では返らない)。
FAKE_ROWS = [
    (
        1,
        Decimal("12.3456"),
        datetime(2026, 1, 2, 3, 4, 5),
        date(2026, 1, 2),
        time(3, 4, 5),
        timedelta(hours=1, seconds=3),
        b"ab",
        uuid.UUID(int=1),
        None,
    ),
    (
        2,
        Decimal("3"),
        datetime(2026, 1, 2, 3, 4, 5, 678000, tzinfo=timezone.utc),
        date(2026, 12, 31),
        time(23, 59, 59, 1),
        timedelta(days=-1, microseconds=5),
        b"",
        uuid.UUID(int=2**128 - 1),
        '日本語 "quote" </script>',
    ),
]


def _settings(**overrides) -> AppSettings:
    return AppSettings(_env_file=None, database_url="sqlite://", **overrides)


def _reference_get_json(payload: dict) -> dict:
    """従来の GET と同じ経路(``response_model=ViewDataResponse``)で JSON にした結果。"""
    ref = FastAPI()

    @ref.get("/ref", response_model=ViewDataResponse)
    def _ref():
        return payload

    with TestClient(ref) as client:
        res = client.get("/ref")
    assert res.status_code == 200, res.text
    return res.json()


def _drive_sse(app, view_name: str) -> list[dict]:
    flags = [False, True]

    async def is_disconnected():
        return flags.pop(0)

    async def drive():
        return [
            ev
            async for ev in _sse_event_stream(
                app.state.view_service,
                app.state.alert_engine,
                view_name,
                0.0,
                is_disconnected,
            )
        ]

    return asyncio.run(drive())


@pytest.fixture
def fake_app(monkeypatch):
    config = MonitorConfig(
        tables={"t": TableDef(columns={"id": "int", "amount": "float"})},
        views={"v": ViewDef(query="SELECT 1", title="偽の行")},
        alerts=[AlertRule(view="v", column="amount", op=">", value=10)],
    )
    app = create_app(config, _settings())
    monkeypatch.setattr(
        app.state.view_service,
        "_fetch",
        lambda sql, params: (list(FAKE_COLUMNS), list(FAKE_ROWS)),
    )
    return app


# ---------------------------------------------------------------------------
# 従来の GET との一致(パース後の JSON)
# ---------------------------------------------------------------------------
def test_get_matches_the_response_model_output_for_driver_types(fake_app):
    client = TestClient(fake_app)
    body = client.get("/api/views/v").json()

    snap = fake_app.state.view_service.get_snapshot("v")
    reference = _reference_get_json(
        {**snap.payload, "alerts": fake_app.state.alert_engine.active_alerts()}
    )
    assert body == reference
    # 代表値: Decimal は文字列、timedelta は ISO 8601、bytes は UTF-8 文字列
    row = body["data"][0]
    assert row["amount"] == "12.3456"
    assert row["ts"] == "2026-01-02T03:04:05"
    assert row["elapsed"] == "PT1H3S"
    assert row["blob"] == "ab"
    assert row["uid"] == "00000000-0000-0000-0000-000000000001"
    # Decimal('12.3456') > 10 は違反、Decimal('3') は違反しない(Decimal でも比較できる)
    assert [a["count"] for a in body["alerts"]] == [1]


def test_batch_and_sse_now_match_get_for_driver_types(fake_app):
    client = TestClient(fake_app)
    get_body = client.get("/api/views/v").json()
    batch = client.get("/api/views/batch", params={"names": "v"}).json()
    assert batch["views"]["v"] == get_body

    events = _drive_sse(fake_app, "v")
    assert len(events) == 1 and events[0]["event"] == "message"
    assert json.loads(events[0]["data"]) == get_body


def test_get_matches_the_response_model_output_for_sqlite_types():
    config = MonitorConfig(
        tables={
            "t": TableDef(
                columns={
                    "id": "int",
                    "x": "float",
                    "s": "str",
                    "b": "bool",
                    "d": "date",
                }
            )
        },
        views={"v": ViewDef(query="SELECT * FROM t ORDER BY id", title="型")},
    )
    app = create_app(config, _settings())
    client = TestClient(app)
    rows = [
        {"x": 1.5, "s": "a", "b": True, "d": "2026-10-21"},
        {"x": -0.0001, "s": "日本語", "b": False, "d": "2026-01-01"},
        {"x": 1e20, "s": "", "b": None, "d": None},
        {"x": None, "s": None, "b": None, "d": None},
    ]
    assert client.post("/api/ingest/t", json=rows).status_code == 200

    body = client.get("/api/views/v").json()
    snap = app.state.view_service.get_snapshot("v")
    assert body == _reference_get_json({**snap.payload, "alerts": []})
    # 行の組み立て(dict(zip))も従来の mappings() と同じ
    with app.state.db.readonly() as conn:
        legacy = [
            dict(r) for r in conn.execute(text(config.views["v"].query)).mappings()
        ]
    assert snap.rows == legacy


def test_nan_and_infinity_become_null_instead_of_breaking_the_json(monkeypatch):
    config = MonitorConfig(views={"v": ViewDef(query="SELECT 1")})
    app = create_app(config, _settings())
    monkeypatch.setattr(
        app.state.view_service,
        "_fetch",
        lambda sql, params: (["a", "b"], [(math.nan, math.inf)]),
    )

    def strict(constant):
        raise AssertionError(f"JSON に {constant} が出力されました")

    res = TestClient(app).get("/api/views/v")
    assert res.status_code == 200
    assert json.loads(res.text, parse_constant=strict)["data"] == [
        {"a": None, "b": None}
    ]


def test_unserialisable_values_fall_back_instead_of_failing():
    class Custom:
        def __str__(self) -> str:
            return "custom!"

    assert json.loads(dumps({"v": Custom()})) == {"v": "custom!"}
    # UTF-8 として読めない bytes(BLOB)は base64(URL セーフ)で表す
    assert json.loads(dumps({"v": b"\xff\x00"})) == {"v": "_wA="}


def test_snapshot_is_serialised_once_and_shared(tmp_path):
    config = MonitorConfig(
        tables={"t": TableDef(columns={"id": "int", "v": "str"})},
        views={"v": ViewDef(query="SELECT * FROM t")},
    )
    app = create_app(
        config,
        AppSettings(_env_file=None, database_url=f"sqlite:///{tmp_path / 'a.db'}"),
    )
    client = TestClient(app)
    client.post("/api/ingest/t", json=[{"v": "a"}, {"v": "b"}])
    svc = app.state.view_service
    first = svc.get_snapshot("v")
    assert svc.get_snapshot("v") is first  # 同じスナップショット(JSON も再利用)
    alerts = dumps([])
    assert client.get("/api/views/v").content == first.body(alerts)
    assert first.text(alerts) is first.text(alerts)  # SSE 用の文字列も共有
    assert first.digest == ViewSnapshot(first.payload).digest  # ハッシュは内容で決まる
    app.state.services.close()


# ---------------------------------------------------------------------------
# ETag / 304
# ---------------------------------------------------------------------------
@pytest.fixture
def client(app) -> TestClient:  # conftest の app(users 5 件など)を使う
    return TestClient(app)


def test_get_sends_a_weak_etag_and_no_cache(client):
    res = client.get("/api/views/users_view")
    assert res.status_code == 200
    etag = res.headers["etag"]
    assert etag.startswith('W/"') and etag.endswith('"')
    assert res.headers["cache-control"] == "private, no-cache"
    assert res.headers["content-type"] == "application/json"


def test_matching_if_none_match_returns_304_without_body(client):
    etag = client.get("/api/views/users_view").headers["etag"]
    res = client.get("/api/views/users_view", headers={"If-None-Match": etag})
    assert res.status_code == 304
    assert res.content == b""
    assert res.headers["etag"] == etag
    assert res.headers["cache-control"] == "private, no-cache"


@pytest.mark.parametrize(
    "header",
    ["*", '"other", {etag}', '{etag}, W/"x"', "{strong}"],
)
def test_if_none_match_forms(client, header):
    etag = client.get("/api/views/users_view").headers["etag"]
    value = header.format(etag=etag, strong=etag.removeprefix("W/"))
    res = client.get("/api/views/users_view", headers={"If-None-Match": value})
    assert res.status_code == 304


def test_stale_etag_gets_the_full_body(client):
    res = client.get("/api/views/users_view", headers={"If-None-Match": 'W/"stale"'})
    assert res.status_code == 200
    assert res.json()["data"]


def test_etag_changes_with_the_data(client):
    etag = client.get("/api/views/users_view").headers["etag"]
    client.post("/api/tables/users", json={"name": "Zoe", "email": "z@e.com"})
    res = client.get("/api/views/users_view", headers={"If-None-Match": etag})
    assert res.status_code == 200
    assert res.headers["etag"] != etag


def test_etag_changes_with_the_active_alerts():
    config = MonitorConfig(
        tables={"t": TableDef(columns={"id": "int", "temp": "float"})},
        views={
            "plain": ViewDef(query="SELECT 1 AS one"),
            "temps": ViewDef(query="SELECT * FROM t"),
        },
        alerts=[AlertRule(view="temps", column="temp", op=">", value=80)],
    )
    client = TestClient(create_app(config, _settings()))
    etag = client.get("/api/views/plain").headers["etag"]
    client.post("/api/ingest/t", json=[{"temp": 99}])
    assert client.get("/api/views/temps").json()["alerts"]  # 別ビューで発火
    res = client.get("/api/views/plain", headers={"If-None-Match": etag})
    assert res.status_code == 200  # データは同じでもアラートが変わった
    assert res.json()["alerts"][0]["view"] == "temps"


def test_paged_and_full_responses_have_different_etags(client):
    full = client.get("/api/views/users_view").headers["etag"]
    page = client.get("/api/views/users_view", params={"limit": 2}).headers["etag"]
    assert full != page


def test_batch_etag_and_304(client):
    params = {"names": "users_view,orders_summary"}
    res = client.get("/api/views/batch", params=params)
    etag = res.headers["etag"]
    assert res.headers["cache-control"] == "private, no-cache"
    again = client.get(
        "/api/views/batch", params=params, headers={"If-None-Match": etag}
    )
    assert again.status_code == 304 and again.content == b""

    reordered = client.get(
        "/api/views/batch",
        params={"names": "orders_summary,users_view"},
        headers={"If-None-Match": etag},
    )
    assert reordered.status_code == 200  # 並びが違えば本文も違う
    assert list(reordered.json()["views"]) == ["orders_summary", "users_view"]

    client.post("/api/tables/users", json={"name": "Zoe", "email": "z@e.com"})
    changed = client.get(
        "/api/views/batch", params=params, headers={"If-None-Match": etag}
    )
    assert changed.status_code == 200


def test_batch_ignores_unknown_and_duplicate_names(client):
    body = client.get(
        "/api/views/batch", params={"names": "users_view, ghost,users_view,,"}
    ).json()
    assert list(body["views"]) == ["users_view"]
    assert client.get("/api/views/batch", params={"names": ","}).json() == {"views": {}}


def test_openapi_keeps_the_response_model_of_get_view(app):
    op = app.openapi()["paths"]["/api/views/{view_name}"]["get"]
    schema = op["responses"]["200"]["content"]["application/json"]["schema"]
    assert schema["$ref"].endswith("/ViewDataResponse")
    assert "304" in op["responses"]


# ---------------------------------------------------------------------------
# ViewDef.max_rows
# ---------------------------------------------------------------------------
@pytest.fixture
def capped_client():
    config = MonitorConfig(
        tables={"t": TableDef(columns={"id": "int", "temp": "float"})},
        views={
            "capped": ViewDef(query="SELECT * FROM t ORDER BY id", max_rows=3),
        },
        alerts=[AlertRule(view="capped", column="temp", op=">", value=80)],
    )
    client = TestClient(create_app(config, _settings()))
    rows = [{"id": i, "temp": 20 + i} for i in range(1, 6)] + [{"id": 6, "temp": 99}]
    assert client.post("/api/ingest/t", json=rows).status_code == 200
    return client


@pytest.mark.parametrize(
    "params, ids",
    [
        ({}, [1, 2, 3]),
        ({"limit": 2}, [1, 2]),
        ({"limit": 10}, [1, 2, 3]),
        ({"limit": 2, "offset": 2}, [3]),
        ({"limit": 2, "offset": 3}, []),
        ({"offset": 2}, [1, 2, 3]),  # limit が無ければ offset は使わない(従来どおり)
    ],
)
def test_max_rows_caps_full_and_paged_results(capped_client, params, ids):
    data = capped_client.get("/api/views/capped", params=params).json()["data"]
    assert [row["id"] for row in data] == ids


def test_alerts_use_the_capped_rows_as_full_data(capped_client):
    # 違反する行(id=6)は上限の外にあるため、ビューの「全データ」には含まれない
    assert capped_client.get("/api/views/capped").json()["alerts"] == []


@pytest.fixture
def views_log():
    logger = logging.getLogger("monitor_app.views")
    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append
    old_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    yield records
    logger.removeHandler(handler)
    logger.setLevel(old_level)


def test_large_uncapped_view_warns_once(monkeypatch, views_log):
    monkeypatch.setattr(view_service_mod, "LARGE_VIEW_ROWS", 3)
    config = MonitorConfig(
        tables={"t": TableDef(columns={"id": "int"})},
        views={
            "big": ViewDef(query="SELECT * FROM t"),
            "bounded": ViewDef(query="SELECT * FROM t", max_rows=4),
        },
    )
    app = create_app(config, _settings())
    client = TestClient(app)
    client.post("/api/ingest/t", json=[{"id": i} for i in range(1, 6)])

    client.get("/api/views/big")
    app.state.view_cache.clear()
    client.get("/api/views/big")  # 計算し直しても 2 回目は警告しない
    client.get("/api/views/big", params={"limit": 5})  # ページ取得は対象外
    client.get("/api/views/bounded")  # 上限つきのビューは対象外
    warnings = [r for r in views_log if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "'big'" in message and "5 行" in message and "max_rows" in message


# ---------------------------------------------------------------------------
# 時刻・乱数に依存する SQL の判定
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM t WHERE ts > datetime('now', '-1 day')",
        "SELECT strftime('%s','now') - MAX(ts) FROM t",
        "SELECT * FROM t WHERE d = CURRENT_DATE",
        "SELECT current_timestamp",
        "SELECT * FROM t WHERE ts > NOW() - INTERVAL 1 HOUR",
        "SELECT * FROM t ORDER BY RANDOM() LIMIT 5",
        "SELECT * FROM t ORDER BY rand()",
        "SELECT date() AS today",
        "SELECT UNIX_TIMESTAMP() - ts FROM t",
        "SELECT * FROM t WHERE ts > LOCALTIMESTAMP",
        # 時刻を省略した SQLite の strftime / PostgreSQL の日付の特殊値・age()
        "SELECT strftime('%s') - MAX(ts) FROM t",
        "SELECT strftime( '%Y-%m-%d' ) AS today",
        "SELECT * FROM t WHERE d >= 'today'",
        "SELECT * FROM t WHERE d = 'yesterday'::date",
        "SELECT age(ts) FROM t",
        "SELECT hex(randomblob(4)) AS token",
        "SELECT uuid_generate_v4()",
    ],
)
def test_time_relative_sql_is_detected(sql):
    assert is_time_relative(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM measurements ORDER BY id",
        "SELECT line, AVG(temp) FROM t GROUP BY line",
        "SELECT date(ts) AS day, COUNT(*) FROM t GROUP BY day",
        "SELECT * FROM known_errors",
        "SELECT strftime('%Y-%m', ts) AS month FROM t",
        "SELECT * FROM today_stats",
        "SELECT image, usage FROM t",
    ],
)
def test_plain_sql_is_not_time_relative(sql):
    assert not is_time_relative(sql)


def test_pre_serialised_bodies_and_304_work_behind_gzip_middleware():
    """圧縮ミドルウェア(GZipMiddleware)の後ろでも本文・ETag・304 が正しく扱われる。"""
    from starlette.middleware.gzip import GZipMiddleware

    config = MonitorConfig(
        tables={"t": TableDef(columns={"id": "int", "note": "str"})},
        views={"v": ViewDef(query="SELECT * FROM t ORDER BY id")},
    )
    app = create_app(config, _settings())
    app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=5)
    client = TestClient(app)
    client.post(
        "/api/ingest/t", json=[{"id": i, "note": "測定値" * 20} for i in range(200)]
    )

    res = client.get("/api/views/v", headers={"Accept-Encoding": "gzip"})
    assert res.status_code == 200
    assert res.headers["content-encoding"] == "gzip"
    assert len(res.json()["data"]) == 200  # 展開後は通常の JSON
    etag = res.headers["etag"]

    again = client.get(
        "/api/views/v", headers={"Accept-Encoding": "gzip", "If-None-Match": etag}
    )
    assert again.status_code == 304
    assert again.content == b""
    assert "content-encoding" not in again.headers
    assert again.headers["etag"] == etag


def test_failing_view_query_is_a_json_500_and_not_cached():
    config = MonitorConfig(views={"bad": ViewDef(query="SELECT * FROM missing_table")})
    app = create_app(config, _settings())
    client = TestClient(app, raise_server_exceptions=False)
    for _ in range(2):
        res = client.get("/api/views/bad")
        assert res.status_code == 500
        assert res.json()["detail"]["code"] == "QUERY_EXECUTION_ERROR"
    assert app.state.view_cache.size == 0


def test_legacy_helpers_still_work(client):
    svc = client.app.state.view_service
    payload, digest = svc.get_view_hashed("users_view")
    assert digest == svc.get_snapshot("users_view").digest
    assert svc.data_hash(payload) == svc.data_hash(svc.get_view("users_view"))
    payload["data"] = []  # 返り値の書き換えはキャッシュに影響しない
    assert svc.get_view("users_view")["data"]
