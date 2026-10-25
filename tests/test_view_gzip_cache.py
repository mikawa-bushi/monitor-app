"""ビューの GET は gzip にした本文をスナップショットごとに 1 回だけ作る(#D2-01)。

圧縮ミドルウェアは要求のたびに本文を圧縮し直すため、キャッシュから返す大きなビューでは
時間の大半が圧縮になり、10 万行のビューでは v2.3.1(圧縮なし)より遅くなっていた。
ビューの GET は圧縮済みの本文をスナップショットに残して使い回す。
"""

from __future__ import annotations

import gzip
import json
import zlib

import pytest
from fastapi.testclient import TestClient

from monitor_app import MonitorConfig, TableDef, ViewDef
from monitor_app.main import create_app
from monitor_app.services.view_service import ViewSnapshot
from monitor_app.settings.runtime import AppSettings
from monitor_app.web.serving import GZIP_COMPRESSLEVEL, accepts_gzip

GZIP = {"Accept-Encoding": "gzip"}
IDENTITY = {"Accept-Encoding": "identity"}

CONFIG = MonitorConfig(
    tables={"t": TableDef(columns={"id": "int", "note": "str"})},
    views={
        "big": ViewDef(query="SELECT * FROM t ORDER BY id"),
        "tiny": ViewDef(query="SELECT id FROM t WHERE id = 1"),
    },
)


def _client(**settings) -> TestClient:
    app = create_app(
        CONFIG, AppSettings(_env_file=None, database_url="sqlite://", **settings)
    )
    client = TestClient(app)
    client.post(
        "/api/ingest/t", json=[{"id": i, "note": "測定値" * 20} for i in range(300)]
    )
    return client


class _CountingCompressor:
    """``zlib.compressobj()`` の結果を包み、本文を実際に圧縮した回数を数える。"""

    def __init__(self, inner, calls: list[str]) -> None:
        self._inner = inner
        self._calls = calls

    def compress(self, data):
        if len(data):
            self._calls.append("compressobj")
        return self._inner.compress(data)

    def __getattr__(self, name):
        return getattr(self._inner, name)


@pytest.fixture
def compressions(monkeypatch):
    """本文を zlib で圧縮した回数(gzip.compress と GZipMiddleware の GzipFile の両方)。

    圧縮器を作っただけの回は数えない(Starlette 1.3 の GZipMiddleware は、圧縮済みの
    応答をそのまま通すときも要求ごとに GzipFile を作る)。
    """
    calls: list[str] = []
    real_compress, real_compressobj = zlib.compress, zlib.compressobj

    def compress(data, *args, **kwargs):
        if len(data):
            calls.append("compress")
        return real_compress(data, *args, **kwargs)

    def compressobj(*args, **kwargs):
        return _CountingCompressor(real_compressobj(*args, **kwargs), calls)

    monkeypatch.setattr(zlib, "compress", compress)
    monkeypatch.setattr(zlib, "compressobj", compressobj)
    return calls


def test_a_cached_view_is_compressed_once_for_all_gzip_requests(compressions):
    client = _client()
    first = client.get("/api/views/big", headers=GZIP)
    compressions.clear()  # 1 回目は圧縮する(スナップショットに残す)
    for _ in range(3):
        again = client.get("/api/views/big", headers=GZIP)
        assert again.status_code == 200
        assert again.headers["content-encoding"] == "gzip"
        assert again.content == first.content
    assert compressions == []  # 2 回目以降は圧縮し直さない


def test_the_compressed_body_matches_the_plain_json_and_keeps_the_etag():
    client = _client()
    with client.stream("GET", "/api/views/big", headers=GZIP) as res:
        raw = b"".join(res.iter_raw())
        headers = res.headers
    plain = client.get("/api/views/big", headers=IDENTITY)
    assert raw[:2] == b"\x1f\x8b" and gzip.decompress(raw) == plain.content
    assert headers["content-encoding"] == "gzip"
    assert headers["vary"].lower() == "accept-encoding"  # 二重に付けない
    assert headers["etag"] == plain.headers["etag"]
    assert headers["cache-control"] == plain.headers["cache-control"]
    assert int(headers["content-length"]) == len(raw)
    assert "content-encoding" not in plain.headers
    assert len(json.loads(plain.content)["data"]) == 300

    not_modified = client.get(
        "/api/views/big", headers={**GZIP, "If-None-Match": headers["etag"]}
    )
    assert not_modified.status_code == 304
    assert not_modified.content == b""
    assert "content-encoding" not in not_modified.headers


def test_new_data_is_compressed_again():
    client = _client()
    before = client.get("/api/views/big", headers=GZIP)
    client.post("/api/ingest/t", json=[{"id": 999, "note": "追加"}])
    after = client.get("/api/views/big", headers=GZIP)
    assert after.headers["etag"] != before.headers["etag"]
    assert len(after.json()["data"]) == 301


def test_compression_off_returns_plain_json():
    client = _client(compress_responses=False)
    res = client.get("/api/views/big", headers=GZIP)
    assert res.status_code == 200
    assert "content-encoding" not in res.headers
    assert len(res.json()["data"]) == 300


def test_small_views_are_not_compressed(compressions):
    client = _client()
    compressions.clear()
    res = client.get("/api/views/tiny", headers=GZIP)
    assert res.json()["data"] == [{"id": 1}]
    assert "content-encoding" not in res.headers
    assert compressions == []


def test_snapshot_compresses_again_only_when_the_alerts_change():
    snap = ViewSnapshot({"data": [{"id": i} for i in range(200)], "columns": ["id"]})
    first = snap.gzip_body(b"[]", GZIP_COMPRESSLEVEL)
    assert snap.gzip_body(b"[]", GZIP_COMPRESSLEVEL) is first
    alerts = b'[{"view":"v"}]'
    other = snap.gzip_body(alerts, GZIP_COMPRESSLEVEL)
    assert gzip.decompress(first) == snap.body(b"[]")
    assert gzip.decompress(other) == snap.body(alerts)


@pytest.mark.parametrize(
    "headers,expected",
    [
        ([(b"accept-encoding", b"gzip, deflate, br")], True),
        ([(b"accept-encoding", b"identity")], False),
        ([], False),
        ([(b"accept-encoding", b"gzip"), (b"range", b"bytes=0-10")], False),
        ([(b"accept-encoding", b"gzip"), (b"accept", b"text/event-stream")], False),
    ],
)
def test_accepts_gzip_follows_the_middleware(headers, expected):
    assert accepts_gzip({"type": "http", "headers": headers}) is expected
