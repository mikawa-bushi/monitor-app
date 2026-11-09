"""UI 配信の HTTP 配線: サブパス(root_path)・ルート相対 URL・静的ファイルのキャッシュ・gzip。

- サブパス配信: プレフィックスを外すプロキシ(nginx ``proxy_pass http://app/;``)と
  外さないプロキシ(``proxy_pass http://app;``)の両方を、TestClient への要求パスで再現する。
- 既定(root_path なし)では今までどおりのリダイレクト先・Cookie になること。
"""

from __future__ import annotations

import gzip
import inspect
import logging
import re
from urllib.parse import quote

import anyio
import pytest
from contract import snapshot
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.middleware.gzip import GZipMiddleware
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

import monitor_app
from monitor_app import ChartDef, FormDef, KioskConfig, MonitorConfig, TableDef, ViewDef
from monitor_app.main import create_app
from monitor_app.settings.runtime import AppSettings
from monitor_app.web import serving

# ?v= は「配布物のバージョン-静的ファイルのハッシュ」
VERSION_QS = "v=" + quote(serving.ASSET_VERSION, safe="")
BIG_VIEW_ROWS = 3000


def _config() -> MonitorConfig:
    return MonitorConfig(
        tables={
            "items": TableDef(
                columns={"id": "int", "name": "str", "qty": "int"},
                primary_key="id",
                form=FormDef(submit_label="登録"),
            ),
        },
        views={
            "items_view": ViewDef(query="SELECT * FROM items", title="品目"),
            "chart_view": ViewDef(
                query="SELECT * FROM items",
                title="グラフ",
                chart=ChartDef(type="line", x="id", y="qty"),
            ),
            "big_view": ViewDef(
                query=(
                    "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n "
                    f"WHERE x < {BIG_VIEW_ROWS}) SELECT x AS id, 'row-' || x AS label FROM n"
                ),
                title="大きなビュー",
            ),
        },
        kiosk=KioskConfig(views=["items_view", "chart_view"]),
    )


_CREATED: list = []


def _app(**settings):
    base = {"database_url": "sqlite://", "_env_file": None}
    app = create_app(_config(), AppSettings(**{**base, **settings}))
    _CREATED.append(app)
    return app


@pytest.fixture(autouse=True)
def _close_apps():
    """lifespan を通さずに作ったアプリの DB 接続を、テストごとに閉じる。

    Services.close() はインメモリ SQLite を(アプリと同じ寿命にするため)破棄しない。
    テストではもう使わないので、エンジンごと破棄して接続を閉じる。
    """
    yield
    while _CREATED:
        app = _CREATED.pop()
        app.state.services.close()
        app.state.db.dispose()


@pytest.fixture
def client():
    return TestClient(_app())


@pytest.fixture
def sub_client():
    """MONITOR_ROOT_PATH=/monitor のアプリ。"""
    return TestClient(_app(root_path="/monitor"))


PAGES = [
    "/",
    "/table/items_view",
    "/table/chart_view",
    "/form/items",
    "/kiosk",
    "/login",
]


def _urls(html: str) -> list[str]:
    """HTML 中の href/src/action と、index.html の動的ローダー(s.src = "...")の URL。"""
    attrs = re.findall(r'(?:href|src|action)="([^"]*)"', html)
    loader = re.findall(r'\.src = "([^"]*)"', html)
    return attrs + loader


# ---------------------------------------------------------------------------
# 既定(ルートで公開): 今までどおり + ルート相対・バージョン付き
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("page", PAGES)
def test_default_pages_use_root_relative_versioned_urls(client, page):
    res = client.get(page)
    assert res.status_code == 200
    html = res.text
    assert '<meta name="monitor-root" content="">' in html
    assert (
        "http://testserver" not in html
    )  # 絶対 URL を出さない(TLS 終端の混在コンテンツ対策)
    urls = _urls(html)
    assert urls
    for url in urls:
        assert url.startswith("/"), url
        if url.startswith("/static/"):
            assert url.endswith("?" + VERSION_QS), url


def test_every_asset_referenced_by_the_pages_is_served(client):
    assets = {
        u for page in PAGES for u in _urls(client.get(page).text) if "/static/" in u
    }
    for name in ("core.js", "app.js", "kiosk.js", "dashboard.js", "form.js", "app.css"):
        assert any(name in a for a in assets), name
    for asset in sorted(assets):
        assert client.get(asset).status_code == 200, asset


def test_core_js_is_loaded_before_the_page_scripts(client):
    for page in ("/", "/table/chart_view", "/form/items", "/kiosk"):
        scripts = re.findall(r'<script src="([^"]+)"', client.get(page).text)
        names = [s.split("/")[-1].split("?")[0] for s in scripts]
        assert "core.js" in names, page
        core = names.index("core.js")
        for other in ("app.js", "kiosk.js", "form.js", "kpis.js", "alerts-ui.js"):
            if other in names:
                assert names.index(other) > core, (page, names)


def test_default_redirects_and_cookie_path_are_unchanged():
    client = TestClient(_app(api_key="k", protect_reads=True))
    res = client.get("/", follow_redirects=False)
    assert (res.status_code, res.headers["location"]) == (303, "/login")
    res = client.post("/api/login", data={"key": "bad"}, follow_redirects=False)
    assert res.headers["location"] == "/login?error=1"
    res = client.post("/api/login", data={"key": "k"}, follow_redirects=False)
    assert res.headers["location"] == "/"
    assert "path=/;" in res.headers["set-cookie"].lower()
    res = client.post("/api/logout", follow_redirects=False)
    assert res.headers["location"] == "/login"
    assert "path=/;" in res.headers["set-cookie"].lower()


def test_login_form_posts_through_url_for(client):
    assert 'action="/api/login"' in client.get("/login").text


def test_csv_export_link_requests_a_bom(client):
    html = client.get("/table/items_view").text
    assert 'href="/api/views/items_view/export?format=csv&amp;bom=1"' in html


def test_alert_banner_is_a_live_region(client):
    for page in ("/table/items_view", "/kiosk"):
        assert re.search(
            r'id="alert-banner"[^>]*aria-live="polite"', client.get(page).text
        )


def test_navigation_landmarks_are_labelled(client):
    html = client.get("/").text
    navs = re.findall(r"<nav\b[^>]*>", html)
    assert navs and all("aria-label=" in n for n in navs), navs
    assert 'aria-controls="sidebar"' in html and 'aria-expanded="false"' in html


# ---------------------------------------------------------------------------
# サブパス配信(MONITOR_ROOT_PATH)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("prefix", ["", "/monitor"], ids=["stripping", "non-stripping"])
@pytest.mark.parametrize("page", PAGES)
def test_root_path_pages_for_both_proxy_styles(sub_client, prefix, page):
    """prefix="" はプレフィックスを外すプロキシ、"/monitor" は外さないプロキシの要求。"""
    res = sub_client.get(prefix + page)
    assert res.status_code == 200, res.text[:200]
    html = res.text
    assert '<meta name="monitor-root" content="/monitor">' in html
    assert "http://testserver" not in html
    for url in _urls(html):
        assert url.startswith("/monitor/"), url


@pytest.mark.parametrize("prefix", ["", "/monitor"], ids=["stripping", "non-stripping"])
def test_root_path_assets_and_api_for_both_proxy_styles(sub_client, prefix):
    res = sub_client.get(f"{prefix}/static/css/app.css?{VERSION_QS}")
    assert res.status_code == 200
    assert res.headers["cache-control"] == serving.IMMUTABLE_CACHE_CONTROL
    assert sub_client.get(f"{prefix}/static/js/core.js").status_code == 200
    assert sub_client.get(f"{prefix}/api/views/items_view").status_code == 200
    assert (
        sub_client.post(
            f"{prefix}/api/tables/items", json={"name": "a", "qty": 1}
        ).status_code
        == 201
    )
    assert sub_client.get(f"{prefix}/api/health").json()["status"] == "ok"


def test_root_path_links_point_inside_the_prefix(sub_client):
    html = sub_client.get("/").text
    assert 'href="/monitor/table/items_view"' in html
    assert 'href="/monitor/kiosk"' in html
    assert f'href="/monitor/static/css/app.css?{VERSION_QS}"' in html
    assert 'action="/monitor/api/login"' in sub_client.get("/login").text


@pytest.mark.parametrize("prefix", ["", "/monitor"], ids=["stripping", "non-stripping"])
def test_root_path_redirects_and_cookie_path(prefix):
    client = TestClient(_app(root_path="/monitor/", api_key="k", protect_reads=True))
    res = client.get(prefix + "/table/items_view", follow_redirects=False)
    assert (res.status_code, res.headers["location"]) == (303, "/monitor/login")
    res = client.post(prefix + "/api/login", data={"key": "x"}, follow_redirects=False)
    assert res.headers["location"] == "/monitor/login?error=1"
    res = client.post(prefix + "/api/login", data={"key": "k"}, follow_redirects=False)
    assert res.headers["location"] == "/monitor/"
    cookie = res.headers["set-cookie"].lower()
    assert "monitor_session=" in cookie and "path=/monitor;" in cookie
    res = client.post(prefix + "/api/logout", follow_redirects=False)
    assert res.headers["location"] == "/monitor/login"
    assert "path=/monitor;" in res.headers["set-cookie"].lower()


@pytest.mark.parametrize(
    "query, location",
    [("", "/monitor/"), ("?a=1&b=%E3%81%82", "/monitor/?a=1&b=%E3%81%82")],
)
def test_bare_root_path_redirects_root_relative(sub_client, query, location):
    """``/monitor``(末尾スラッシュなし)は ``/monitor/`` へ。スキーム・ホストを含めない。

    Starlette の redirect_slashes は Host ヘッダから絶対 URL を作るため、TLS 終端の
    プロキシの裏では http:// に、Host を書き換えるプロキシの裏では内部アドレスになる。
    """
    res = sub_client.get(
        "/monitor" + query,
        headers={"Host": "app-internal:9990"},
        follow_redirects=False,
    )
    assert (res.status_code, res.headers["location"]) == (307, location)
    assert res.headers["x-content-type-options"] == "nosniff"  # 他の応答と同じヘッダ
    assert res.headers["referrer-policy"] == "same-origin"
    assert sub_client.get("/monitor" + query).status_code == 200  # 追従すると一覧


def test_bare_root_path_from_the_server_redirects_too():
    client = TestClient(_app(), root_path="/sub")
    res = client.get("/sub", follow_redirects=False)
    assert (res.status_code, res.headers["location"]) == (307, "/sub/")


def test_default_root_keeps_starlette_slash_redirects():
    """既定(サブパスなし)の redirect_slashes は今までどおり(挙動を変えない)。"""
    res = TestClient(_app()).get("/table/items_view/", follow_redirects=False)
    assert res.status_code == 307
    assert res.headers["location"] == "http://testserver/table/items_view"


def test_non_ascii_root_path_login_sets_an_encoded_cookie_path():
    """日本語のサブパスでもログイン/ログアウトできる(Cookie の Path はエンコードして送る)。"""
    encoded = "/%E7%9B%A3%E8%A6%96"  # /監視
    client = TestClient(
        _app(root_path="/監視", api_key="k", protect_reads=True),
        base_url="http://testserver" + encoded,
        raise_server_exceptions=False,
    )
    res = client.get("/table/items_view", follow_redirects=False)
    assert (res.status_code, res.headers["location"]) == (303, encoded + "/login")
    res = client.post("/api/login", data={"key": "k"}, follow_redirects=False)
    assert (res.status_code, res.headers["location"]) == (303, encoded + "/")
    assert f"path={encoded.lower()};" in res.headers["set-cookie"].lower()
    assert client.get("/api/views/items_view").status_code == 200  # Cookie が送られる
    res = client.post("/api/logout", follow_redirects=False)
    assert (res.status_code, res.headers["location"]) == (303, encoded + "/login")
    assert f"path={encoded.lower()};" in res.headers["set-cookie"].lower()


def test_cookie_path_helper():
    assert serving.cookie_path({"root_path": ""}) == "/"
    assert serving.cookie_path({"root_path": "/monitor/"}) == "/monitor"
    assert serving.cookie_path({"root_path": "/監視"}) == "/%E7%9B%A3%E8%A6%96"
    assert serving.cookie_path({"root_path": "/a b"}) == "/a%20b"


def test_root_path_session_cookie_authenticates_ui_requests():
    client = TestClient(
        _app(root_path="/monitor", api_key="k", protect_reads=True),
        base_url="http://testserver/monitor",
    )
    assert client.get("/api/views/items_view").status_code == 401
    client.post("/api/login", data={"key": "k"}, follow_redirects=False)
    # Cookie の Path=/monitor の内側の要求にだけ送られ、認証が通る
    assert client.get("/api/views/items_view").status_code == 200
    assert client.get("/table/items_view").status_code == 200


def test_root_path_swagger_points_at_the_prefixed_openapi(sub_client):
    assert "/monitor/openapi.json" in sub_client.get("/docs").text
    schema = sub_client.get("/openapi.json").json()
    assert {"url": "/monitor"} in schema.get("servers", [])


def test_server_provided_root_path_is_honoured_without_settings():
    """uvicorn --root-path などサーバー側でサブパスを与えた場合も静的ファイルが返る。"""
    client = TestClient(_app(), root_path="/sub")
    html = client.get("/table/items_view").text
    assert '<meta name="monitor-root" content="/sub">' in html
    assert f'href="/sub/static/css/app.css?{VERSION_QS}"' in html
    assert client.get("/static/css/app.css").status_code == 200


def test_root_path_that_shadows_an_app_route_is_warned(caplog):
    with caplog.at_level(logging.WARNING, logger="monitor_app.main"):
        _app(root_path="/api")
    assert any("MONITOR_ROOT_PATH=/api" in r.getMessage() for r in caplog.records)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="monitor_app.main"):
        _app(root_path="/monitor")
    assert not caplog.records


async def _call(app, scope_overrides: dict) -> dict:
    seen: dict = {}

    async def inner(scope, receive, send):
        seen.update(scope)

    await serving.RootPathMiddleware(inner, **scope_overrides.pop("init"))(
        {"type": "http", "path": "/x", "raw_path": b"/x", "root_path": ""}
        | scope_overrides,
        None,
        None,
    )
    return seen


@pytest.mark.parametrize(
    "init, scope, path, raw_path, root_path",
    [
        ({"root_path": "/m"}, {}, "/m/x", b"/m/x", "/m"),  # 外すプロキシ
        (
            {"root_path": "/m"},
            {"path": "/m/x", "raw_path": b"/m/x"},
            "/m/x",
            b"/m/x",
            "/m",
        ),
        ({"root_path": "/m"}, {"path": "/m/", "raw_path": b"/m/"}, "/m/", b"/m/", "/m"),
        (
            {"root_path": "/m"},
            {"path": "/mx", "raw_path": b"/mx"},
            "/m/mx",
            b"/m/mx",
            "/m",
        ),
        ({"root_path": ""}, {}, "/x", b"/x", ""),  # 既定: 何もしない
        ({"root_path": ""}, {"root_path": "/s"}, "/s/x", b"/s/x", "/s"),  # サーバー指定
        (
            {"root_path": "/日本"},
            {},
            "/日本/x",
            b"/%E6%97%A5%E6%9C%AC/x",
            "/日本",
        ),
    ],
)
def test_root_path_middleware_normalises_the_scope(
    init, scope, path, raw_path, root_path
):
    seen = anyio.run(_call, None, {"init": init, **scope})
    assert (seen["path"], seen["raw_path"], seen["root_path"]) == (
        path,
        raw_path,
        root_path,
    )


def test_root_path_middleware_redirects_the_bare_root_without_calling_the_app():
    called = []
    sent: list = []

    async def inner(scope, receive, send):
        called.append(scope)

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/m",
        "raw_path": b"/m",
        "root_path": "",
        "query_string": b"x=1",
        "headers": [],
    }
    anyio.run(serving.RootPathMiddleware(inner, "/m"), scope, None, send)
    assert called == []
    start = sent[0]
    assert start["status"] == 307
    assert (b"location", b"/m/?x=1") in start["headers"]


def test_root_path_middleware_passes_lifespan_through():
    seen = {}

    async def inner(scope, receive, send):
        seen.update(scope)

    anyio.run(serving.RootPathMiddleware(inner, "/m"), {"type": "lifespan"}, None, None)
    assert seen == {"type": "lifespan"}


@pytest.mark.parametrize(
    "scope, expected",
    [
        ({"root_path": "/m"}, "/m"),
        ({"root_path": "/m/"}, "/m"),
        ({"root_path": ""}, ""),
        ({}, ""),
        ({"app_root_path": "", "root_path": "/static"}, ""),  # Mount の内側
        ({"app_root_path": "/m", "root_path": "/m/static"}, "/m"),
    ],
)
def test_app_root_path(scope, expected):
    assert serving.app_root_path(scope) == expected


# ---------------------------------------------------------------------------
# 静的ファイルのキャッシュ
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "query, expected",
    [
        (f"?{VERSION_QS}", serving.IMMUTABLE_CACHE_CONTROL),
        (f"?x=1&{VERSION_QS}", serving.IMMUTABLE_CACHE_CONTROL),
        ("", serving.REVALIDATE_CACHE_CONTROL),
        ("?v=", serving.REVALIDATE_CACHE_CONTROL),
        ("?version=1", serving.REVALIDATE_CACHE_CONTROL),
        # 別の版(古いページ・アップグレード中に並ぶ別インスタンス)には不変を指示しない。
        # 指示すると、その URL に違う版の中身が 1 年間キャッシュされてしまう
        ("?v=0.0.1", serving.REVALIDATE_CACHE_CONTROL),
        ("?x=1&v=2", serving.REVALIDATE_CACHE_CONTROL),
    ],
)
def test_static_cache_control_depends_on_the_version_query(client, query, expected):
    res = client.get("/static/js/app.js" + query)
    assert res.status_code == 200
    assert res.headers["cache-control"] == expected


def test_static_revalidation_returns_304_with_the_same_policy(client):
    first = client.get(f"/static/css/app.css?{VERSION_QS}")
    res = client.get(
        f"/static/css/app.css?{VERSION_QS}",
        headers={"If-None-Match": first.headers["etag"]},
    )
    assert res.status_code == 304
    assert res.content == b""
    assert res.headers["cache-control"] == serving.IMMUTABLE_CACHE_CONTROL
    assert "content-encoding" not in res.headers


def test_asset_version_combines_the_release_and_the_static_content(tmp_path):
    """版を上げずに JS/CSS が変わっても(git からのビルド・開発中)URL が変わる。"""
    assert serving.ASSET_VERSION.startswith(monitor_app.__version__ + "-")
    (tmp_path / "js").mkdir()
    (tmp_path / "js" / "a.js").write_text("one", encoding="utf-8")
    first = serving.static_digest(tmp_path)
    assert len(first) == 10 and first == serving.static_digest(tmp_path)
    (tmp_path / "js" / "a.js").write_text("two", encoding="utf-8")
    second = serving.static_digest(tmp_path)
    assert second != first
    (tmp_path / "js" / "a.js").rename(tmp_path / "js" / "b.js")
    assert serving.static_digest(tmp_path) != second  # 名前の変更も
    assert serving.static_digest(tmp_path / "missing") == ""  # 版だけになる


def test_unused_images_are_not_shipped():
    static = serving.__file__.rsplit("serving.py", 1)[0] + "static/img/"
    import os

    assert not os.path.exists(static + "background.webp")
    assert not os.path.exists(static + "logo.webp")
    assert os.path.exists(static + "favicon.ico")


# ---------------------------------------------------------------------------
# gzip
# ---------------------------------------------------------------------------
def test_large_json_view_is_gzipped(client):
    res = client.get("/api/views/big_view", headers={"Accept-Encoding": "gzip"})
    assert res.status_code == 200
    assert res.headers["content-encoding"] == "gzip"
    assert "accept-encoding" in res.headers["vary"].lower()
    assert len(res.json()["data"]) == BIG_VIEW_ROWS  # httpx が透過的に展開する


def test_small_and_identity_responses_are_not_compressed(client):
    small = client.get("/api/health", headers={"Accept-Encoding": "gzip"})
    assert "content-encoding" not in small.headers
    identity = client.get(
        "/api/views/big_view", headers={"Accept-Encoding": "identity"}
    )
    assert "content-encoding" not in identity.headers


def test_static_assets_are_gzipped(client):
    res = client.get(f"/static/js/chart.umd.min.js?{VERSION_QS}")
    assert res.headers["content-encoding"] == "gzip"


def test_head_requests_on_static_files_are_not_compressed(client):
    """HEAD は本文なし。圧縮せず、Content-Length は元のファイルの大きさのまま。"""
    url = f"/static/js/chart.umd.min.js?{VERSION_QS}"
    size = len(client.get(url, headers={"Accept-Encoding": "identity"}).content)
    res = client.head(url, headers={"Accept-Encoding": "gzip"})
    assert res.status_code == 200 and res.content == b""
    assert "content-encoding" not in res.headers
    assert int(res.headers["content-length"]) == size
    assert res.headers["cache-control"] == serving.IMMUTABLE_CACHE_CONTROL


def test_range_requests_are_not_compressed(client):
    res = client.get(
        "/static/js/chart.umd.min.js",
        headers={"Accept-Encoding": "gzip", "Range": "bytes=0-99"},
    )
    assert res.status_code == 206
    assert "content-encoding" not in res.headers
    assert len(res.content) == 100


def test_compression_can_be_disabled():
    client = TestClient(_app(compress_responses=False))
    res = client.get("/api/views/big_view", headers={"Accept-Encoding": "gzip"})
    assert "content-encoding" not in res.headers


@pytest.mark.parametrize(
    "headers",
    [
        {"accept-encoding": "gzip"},  # EventSource(Accept: text/event-stream)
        {"accept-encoding": "gzip", "accept": "*/*"},  # curl などの素の要求
    ],
)
def test_sse_stream_is_never_compressed(headers):
    app = _app()
    status, response_headers, raw = anyio.run(
        _first_sse_event, app, "/api/views/items_view/stream", headers
    )
    assert status == 200
    assert response_headers["content-type"].startswith("text/event-stream")
    assert "content-encoding" not in response_headers
    events = snapshot.parse_sse(raw)
    assert events and events[0]["event"] == "message"


async def _first_sse_event(app, path, headers):
    """``snapshot.read_first_sse_event`` と同じ。Accept を差し替えられるようにした版。"""
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

    merged = {"host": "testserver", "accept": "text/event-stream", **headers}
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(k.encode(), v.encode()) for k, v in merged.items()],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }
    with anyio.fail_after(10):
        await app(scope, receive, send)
    return (
        start.get("status"),
        {k.decode().lower(): v.decode() for k, v in start.get("headers", [])},
        bytes(body),
    )


def _mini_app():
    big = {"rows": ["x" * 40] * 200}

    async def json_view(request):
        return JSONResponse(big)

    async def prebuilt(request):
        # 事前にシリアライズ済みの本文(ビューのキャッシュがこの形で返す)。ETag 付き
        return Response(
            b'{"a":"' + b"y" * 5000 + b'"}',
            media_type="application/json",
            headers={"ETag": 'W/"v1"'},
        )

    async def not_modified(request):
        return Response(status_code=304, headers={"ETag": '"abc"'})

    async def events(request):
        async def gen():
            for i in range(3):
                yield f"data: {i}\n\n" + ":" + " " * 2000 + "\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    app = Starlette(
        routes=[
            Route("/json", json_view),
            Route("/prebuilt", prebuilt),
            Route("/304", not_modified),
            Route("/events", events),
        ]
    )
    return serving.CompressionMiddleware(app)


def test_compression_middleware_behaviour():
    client = TestClient(_mini_app())
    gz = {"Accept-Encoding": "gzip"}
    res = client.get("/json", headers=gz)
    assert res.headers["content-encoding"] == "gzip" and len(res.json()["rows"]) == 200
    res = client.get("/prebuilt", headers=gz)
    assert res.headers["content-encoding"] == "gzip" and res.json()["a"] == "y" * 5000
    # 画面(core.js)は ETag の一致で「変化なし」を判定する。圧縮しても ETag は変わらない
    assert res.headers["etag"] == 'W/"v1"'
    assert client.get("/prebuilt").headers["etag"] == 'W/"v1"'
    res = client.get("/304", headers=gz)
    assert res.status_code == 304 and "content-encoding" not in res.headers
    assert res.headers["etag"] == '"abc"'
    for accept in ("text/event-stream", "*/*"):
        res = client.get("/events", headers={**gz, "Accept": accept})
        assert "content-encoding" not in res.headers
        assert res.text.startswith("data: 0\n\n")


_GZIP_CONFIGURABLE = (
    "exclude_content_types" in inspect.signature(GZipMiddleware).parameters
)


def test_gzip_excludes_event_streams_explicitly_when_configurable():
    options = serving._gzip_options(1024, 5)
    if _GZIP_CONFIGURABLE:
        assert "text/event-stream" in options["exclude_content_types"]
        assert serving.XLSX_CONTENT_TYPE in options["exclude_content_types"]
    else:  # Starlette 1.3 系: 引数が無く、text/event-stream は既定で除外される
        assert "exclude_content_types" not in options
        assert "text/event-stream" in serving.DEFAULT_EXCLUDED_CONTENT_TYPES


@pytest.mark.skipif(not _GZIP_CONFIGURABLE, reason="除外を指定できない Starlette")
def test_xlsx_export_is_not_recompressed(client):
    pytest.importorskip("openpyxl")
    res = client.get(
        "/api/views/big_view/export",
        params={"format": "xlsx"},
        headers={"Accept-Encoding": "gzip"},
    )
    assert res.status_code == 200
    assert res.headers["content-type"] == serving.XLSX_CONTENT_TYPE
    assert "content-encoding" not in res.headers


def test_gzip_level_is_moderate():
    """level 9(Starlette の既定)は大きな JSON で桁違いに遅い。圧縮率はほぼ同じ。"""
    middleware = serving.CompressionMiddleware(lambda *a: None)
    assert middleware.gzip.compresslevel == serving.GZIP_COMPRESSLEVEL == 5
    assert middleware.gzip.minimum_size == serving.GZIP_MINIMUM_SIZE == 1024
    payload = b'{"id": 1, "name": "abc"},' * 4000
    assert len(gzip.compress(payload, 5)) < len(payload) / 10


# ---------------------------------------------------------------------------
# OpenAPI
# ---------------------------------------------------------------------------
def test_openapi_reports_the_package_version(client):
    assert (
        client.get("/openapi.json").json()["info"]["version"] == monitor_app.__version__
    )
    assert _app().openapi()["info"]["version"] == monitor_app.__version__
