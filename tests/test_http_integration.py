"""アプリ全体(ミドルウェア込み)での、画面・ビュー API・SSE の組み合わせの確認。

- 画面の CSV ダウンロードのリンク(``?bom=1``)で、Excel 向けの BOM 付き CSV が届く。
- gzip 圧縮(``MONITOR_COMPRESS_RESPONSES``、既定で有効)と、組み立て済みの JSON の
  応答・ETag / 304 が両立する(圧縮しても ETag が変わらず、304 には本文が無い)。
- SSE は圧縮しない(イベントを 1 件ずつすぐ届ける)。最初のイベントは GET と同じ JSON。
"""

from __future__ import annotations

import gzip
import html
import json
import re
from urllib.parse import unquote

import anyio
import pytest
from contract import snapshot
from fastapi.testclient import TestClient

from monitor_app import MonitorConfig, TableDef, ViewDef
from monitor_app.main import create_app
from monitor_app.settings.runtime import AppSettings

ROWS = 300


@pytest.fixture
def app(tmp_path):
    config = MonitorConfig(
        tables={
            "items": TableDef(columns={"id": "int", "name": "str", "price": "float"})
        },
        views={"商品一覧": ViewDef(query="SELECT * FROM items ORDER BY id")},
    )
    settings = AppSettings(
        _env_file=None, database_url=f"sqlite:///{(tmp_path / 'a.db').as_posix()}"
    )
    application = create_app(config, settings)
    application.state.services.repository.insert_many(
        "items",
        [
            {"id": i, "name": f"製品{i:04d}", "price": i * 1.5}
            for i in range(1, ROWS + 1)
        ],
    )
    yield application
    application.state.services.close()


VIEW = "/api/views/%E5%95%86%E5%93%81%E4%B8%80%E8%A6%A7"  # 商品一覧
GZIP = {"Accept-Encoding": "gzip"}


def test_ui_export_link_downloads_a_csv_with_a_bom(app):
    with TestClient(app) as client:
        page = client.get("/table/%E5%95%86%E5%93%81%E4%B8%80%E8%A6%A7")
        assert page.status_code == 200
        (href,) = re.findall(r'href="([^"]*/export[^"]*)"', page.text)
        href = html.unescape(href)
        assert "bom=1" in href and "format=csv" in href

        response = client.get(href, headers=GZIP)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/csv")
        assert response.headers.get("content-encoding") == "gzip"  # 大きい応答は圧縮
        body = response.content  # httpx が gzip を戻す
        assert body.startswith(b"\xef\xbb\xbf")  # Excel が UTF-8 と判別する BOM
        lines = body[3:].decode("utf-8").splitlines()
        assert lines[0] == "id,name,price" and lines[1] == "1,製品0001,1.5"
        assert len(lines) == ROWS + 1
        # 日本語のビュー名でもダウンロードできる(RFC 5987 の filename*)
        assert "filename*=UTF-8''" in response.headers["content-disposition"]

        plain = client.get(href.replace("&bom=1", ""), headers=GZIP)
        assert not plain.content.startswith(b"\xef\xbb\xbf")  # 既定は BOM なし
        assert plain.content == body[3:]


@pytest.mark.parametrize("path", [VIEW, "/api/views/batch?names=商品一覧"])
def test_gzip_keeps_the_etag_and_conditional_requests(app, path):
    with TestClient(app) as client:
        identity = client.get(path, headers={"Accept-Encoding": "identity"})
        compressed = client.get(path, headers=GZIP)
        assert identity.status_code == compressed.status_code == 200
        assert "content-encoding" not in identity.headers
        assert compressed.headers["content-encoding"] == "gzip"
        # 組み立て済みの JSON を圧縮しても中身は同じで、ETag も同じ
        assert compressed.content == identity.content
        etag = compressed.headers["etag"]
        assert etag.startswith('W/"') and etag == identity.headers["etag"]
        assert compressed.headers["cache-control"] == "private, no-cache"
        assert compressed.headers.get("vary", "").lower() == "accept-encoding"

        not_modified = client.get(path, headers={**GZIP, "If-None-Match": etag})
        assert not_modified.status_code == 304
        assert not_modified.content == b""
        assert "content-encoding" not in not_modified.headers
        assert not_modified.headers["etag"] == etag

        data = json.loads(identity.content)
        if path == VIEW:
            assert len(data["data"]) == ROWS
        else:
            assert len(data["views"]["商品一覧"]["data"]) == ROWS


def test_compressed_body_is_valid_gzip_of_the_json(app):
    """httpx の自動展開に頼らず、届いたバイト列が gzip であることも確かめる。"""
    with TestClient(app) as client:
        with client.stream("GET", VIEW, headers=GZIP) as response:
            raw = b"".join(response.iter_raw())
        identity = client.get(VIEW, headers={"Accept-Encoding": "identity"}).content
    assert raw[:2] == b"\x1f\x8b" and gzip.decompress(raw) == identity
    assert len(raw) < len(identity) / 3


async def _first_event(app, path: str, headers: list[tuple[bytes, bytes]]):
    """SSE を ASGI で呼び、最初のイベントを受け取ったら切断する(本文はそのまま返す)。"""
    start: dict = {}
    body = bytearray()
    first = anyio.Event()
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await first.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.start":
            start.update(message)
        elif message["type"] == "http.response.body":
            body.extend(message.get("body", b""))
            if not message.get("more_body") or b"\n\n" in body or b"\r\n\r\n" in body:
                first.set()

    raw_path, _, query = path.partition("?")
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": unquote(raw_path),  # ASGI の path は復号済み(raw_path はそのまま)
        "raw_path": raw_path.encode(),
        "query_string": query.encode(),
        "root_path": "",
        "headers": [(b"host", b"testserver"), *headers],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }
    with anyio.fail_after(10):
        await app(scope, receive, send)
    return start, bytes(body)


@pytest.mark.parametrize(
    "accept", [b"text/event-stream", b"*/*"], ids=["eventsource", "curl"]
)
def test_sse_is_not_compressed_and_matches_the_view_json(app, accept):
    with TestClient(app) as client:
        payload = client.get(VIEW).json()
        start, raw = anyio.run(
            _first_event,
            app,
            f"{VIEW}/stream",
            [(b"accept", accept), (b"accept-encoding", b"gzip")],
        )
    headers = {k.decode().lower(): v.decode() for k, v in start["headers"]}
    assert start["status"] == 200
    assert snapshot.mime_type(headers["content-type"]) == "text/event-stream"
    assert "content-encoding" not in headers  # 1 件ずつすぐ届ける
    event = snapshot.parse_sse(raw)[0]
    data = json.loads(event["data"])
    assert data["data"] == payload["data"] and "alerts" in data


def test_stream_of_an_unknown_view_is_a_404(app):
    """存在しないビューの SSE は、配信を始める前に 404 の JSON で断る。

    名前を変えたビューを開いたままのキオスクが再接続を繰り返しても、サーバーの
    ログにトレースバックを残さない。
    """
    with TestClient(app) as client:
        response = client.get("/api/views/nope/stream")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "VIEW_NOT_FOUND"


def test_page_links_encode_view_names_with_special_characters(tmp_path):
    """手書きの config.py のビュー名に # ? % や空白があっても、画面のリンクが壊れない。"""
    names = ["a#b", "50%", "x?y", "日本 語"]
    config = MonitorConfig(
        tables={"t": TableDef(columns={"id": "int"})},
        views={name: ViewDef(query="SELECT * FROM t") for name in names},
    )
    url = f"sqlite:///{(tmp_path / 'links.db').as_posix()}"
    app = create_app(config, AppSettings(_env_file=None, database_url=url))
    with TestClient(app) as client:
        page = client.get("/")
        assert page.status_code == 200
        hrefs = set(re.findall(r'href="(/table/[^"]*)"', page.text))
        assert hrefs >= {
            "/table/a%23b",
            "/table/50%25",
            "/table/x%3Fy",
            "/table/%E6%97%A5%E6%9C%AC%20%E8%AA%9E",
        }
        for href in hrefs:
            assert client.get(html.unescape(href)).status_code == 200, href


def test_health_check_works_with_and_without_the_sub_path(tmp_path):
    """MONITOR_ROOT_PATH を設定しても、コンテナのヘルスチェック(直接 /api/health)と
    プロキシ経由(/monitor/api/health)の両方が 200 を返す。"""
    config = MonitorConfig(tables={"t": TableDef(columns={"id": "int"})})
    url = f"sqlite:///{(tmp_path / 'health.db').as_posix()}"
    settings = AppSettings(_env_file=None, database_url=url, root_path="/monitor")
    with TestClient(create_app(config, settings)) as client:
        for path in ("/api/health", "/monitor/api/health"):
            response = client.get(path)
            assert response.status_code == 200, path
            assert response.json()["status"] == "ok"
        assert client.head("/api/health").status_code == 200
