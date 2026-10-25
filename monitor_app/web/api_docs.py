"""API ドキュメント(``/docs``・``/redoc``・``/openapi.json``)の配信。

FastAPI の既定のドキュメントは、バージョンを固定しない CDN のスクリプトを整合性の
確認(SRI)なしで読み込み、``MONITOR_PROTECT_READS`` を有効にしても誰でも見られる
(#D6-12)。ここでは:

- Swagger UI・ReDoc のバージョンを固定し、``integrity``(SRI)を付けて読み込む
  (CDN の中身がすり替わってもブラウザが実行しない)。ReDoc の Google Fonts は読まない。
- ``protect_reads`` が有効なら、ページはログイン(未認証ならログインページへ)、
  ``/openapi.json`` は API キーかセッションが必要(読み取り系の API と同じ扱い)。
- ``MONITOR_API_DOCS=false`` なら 3 つとも出さない(インターネットに出られない現場や、
  API を公開しない運用向け)。

URL・内容(OpenAPI の JSON、サブパス配信の ``servers``)は FastAPI の既定と同じ。
"""

from __future__ import annotations

import html
import json

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .serving import app_root_path

#: 読み込む Swagger UI(swagger-ui-dist)と ReDoc の版。更新するときは SRI も作り直す:
#: ``curl -s <URL> | openssl dgst -sha384 -binary | openssl base64 -A``
SWAGGER_UI_VERSION = "5.33.1"
REDOC_VERSION = "2.5.4"
_CDN = "https://cdn.jsdelivr.net/npm"
#: (URL, SRI)。値は npm の公開パッケージのファイルのハッシュと一致することを確かめてある。
SWAGGER_JS = (
    f"{_CDN}/swagger-ui-dist@{SWAGGER_UI_VERSION}/swagger-ui-bundle.js",
    "sha384-ZPehFMQommnnuaZ4rpxgkgTT2DKFVp4hZC/7pLit+9Lek9T1YGSo23eHFbvNkXkw",
)
SWAGGER_CSS = (
    f"{_CDN}/swagger-ui-dist@{SWAGGER_UI_VERSION}/swagger-ui.css",
    "sha384-Ov4/wv3j2bmct8cDc5X4ngJZohVPzEmc6uDPH8WeljUxO5vtoykvMEfbu9Vh6RaW",
)
REDOC_JS = (
    f"{_CDN}/redoc@{REDOC_VERSION}/bundles/redoc.standalone.js",
    "sha384-w447zOpYfw/1Tv/5AK9NfHTlQIqE3RVR6KY62jCyy9zNDgO64cMwGGP1Fj0zJVf5",
)
OPENAPI_PATH = "/openapi.json"


def _favicon(root: str) -> str:
    return f"{root}/static/img/favicon.ico"


def swagger_ui_html(title: str, openapi_url: str, root: str = "") -> str:
    js, js_sri = SWAGGER_JS
    css, css_sri = SWAGGER_CSS
    options = {
        "url": openapi_url,
        "dom_id": "#swagger-ui",
        "layout": "BaseLayout",
        "deepLinking": True,
        "showExtensions": True,
        "showCommonExtensions": True,
    }
    # </script> で終わらないよう、JSON の < をエスケープしてから埋め込む。
    config = json.dumps(options).replace("<", "\\u003c")
    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} - Swagger UI</title>
<link rel="icon" href="{html.escape(_favicon(root))}">
<link rel="stylesheet" href="{css}" integrity="{css_sri}" crossorigin="anonymous">
</head>
<body>
<div id="swagger-ui"></div>
<script src="{js}" integrity="{js_sri}" crossorigin="anonymous"></script>
<script>
const options = {config};
options.presets = [SwaggerUIBundle.presets.apis, SwaggerUIBundle.SwaggerUIStandalonePreset];
window.ui = SwaggerUIBundle(options);
</script>
</body>
</html>
"""


def redoc_html(title: str, openapi_url: str, root: str = "") -> str:
    js, js_sri = REDOC_JS
    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} - ReDoc</title>
<link rel="icon" href="{html.escape(_favicon(root))}">
<style>body {{ margin: 0; padding: 0; }}</style>
</head>
<body>
<redoc spec-url="{html.escape(openapi_url)}"></redoc>
<script src="{js}" integrity="{js_sri}" crossorigin="anonymous"></script>
</body>
</html>
"""


def install_api_docs(app: FastAPI) -> None:
    """``/openapi.json``・``/docs``・``/redoc`` を登録する(FastAPI の既定の代わり)。

    ``FastAPI(docs_url=None, redoc_url=None, openapi_url=None)`` で作ったアプリに使う。
    """
    from ..api.deps import page_auth_guard, require_read_auth

    @app.get(
        OPENAPI_PATH,
        include_in_schema=False,
        dependencies=[Depends(require_read_auth)],
    )
    async def openapi(request: Request) -> JSONResponse:
        schema = app.openapi()
        root = app_root_path(request)
        if root and app.root_path_in_servers:
            # サブパス配信: FastAPI の既定と同じく servers の先頭にサブパスを足す
            # (キャッシュした schema は書き換えない)。
            urls = {server.get("url") for server in schema.get("servers", [])}
            if root not in urls:
                schema = dict(schema)
                schema["servers"] = [{"url": root}, *schema.get("servers", [])]
        return JSONResponse(schema)

    @app.get("/docs", include_in_schema=False, dependencies=[Depends(page_auth_guard)])
    async def swagger_ui(request: Request) -> HTMLResponse:
        root = app_root_path(request)
        return HTMLResponse(swagger_ui_html(app.title, root + OPENAPI_PATH, root))

    @app.get("/redoc", include_in_schema=False, dependencies=[Depends(page_auth_guard)])
    async def redoc(request: Request) -> HTMLResponse:
        root = app_root_path(request)
        return HTMLResponse(redoc_html(app.title, root + OPENAPI_PATH, root))
