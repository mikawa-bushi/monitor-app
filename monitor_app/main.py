"""アプリファクトリ。``create_app`` が設定を受け取り FastAPI を組み立てる。

import 時に副作用(DB 接続や app 生成)を起こさないため、すべての初期化を
``create_app`` 内に閉じ込める。これによりテストで設定を差し替えやすい。

サービス(DB・各サービス・バックグラウンドタスク)の配線は
:func:`~monitor_app.container.build_services` が担い、ここでは HTTP 層
(lifespan・テンプレート/静的ファイル・ミドルウェア・例外ハンドラ・ルーター)だけを組む。
UI 配信の部品(サブパス・キャッシュ・圧縮)は :mod:`monitor_app.web.serving` にある。
"""

from __future__ import annotations

import importlib.resources as resources
import logging
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from starlette.datastructures import MutableHeaders
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import __version__
from .api.errors import register_exception_handlers
from .api.routers import (
    alerts,
    audit,
    auth,
    crud,
    ingest,
    meta,
    pages,
    uploads,
    views,
)
from .api.routers import settings as settings_router
from .container import Services, build_services
from .security import configured_key_problems, host_policy
from .settings.declarative import MonitorConfig
from .settings.runtime import AppSettings
from .web.api_docs import install_api_docs
from .web.serving import (
    CompressionMiddleware,
    HostGuardMiddleware,
    RootPathMiddleware,
    VersionedStaticFiles,
    create_templates,
)

logger = logging.getLogger("monitor_app.main")

#: 登録するルーター(この順で登録する。同じパスに一致するルートは先に
#: 登録したものが優先されるため、順序は変えない)。
_ROUTERS = (
    crud.router,
    views.router,
    ingest.router,
    alerts.router,
    audit.router,
    auth.router,
    meta.router,
    settings_router.router,
    uploads.router,
    pages.router,
)


def _web_dir() -> Path:
    return Path(str(resources.files("monitor_app") / "web"))


#: 全レスポンスに付けるセキュリティヘッダ(応答側が既に付けていれば上書きしない)。
_SECURITY_HEADERS = (
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "SAMEORIGIN"),
    ("Referrer-Policy", "same-origin"),
)


class _SecurityHeadersMiddleware:
    """基本的なセキュリティヘッダを全レスポンスへ付与する(#S2)。

    ``nosniff`` は MIME スニッフィングによる XSS を防ぐ。``X-Frame-Options`` は
    クリックジャッキング対策(同梱 UI は同一オリジン構成)。厳格な CSP は
    base.html のインラインハンドラ除去とセットのため将来対応とする。

    素の ASGI ミドルウェアにしてある(BaseHTTPMiddleware は応答本文をストリームに
    詰め替えるため、要求ごとのタスク生成の負荷に加え、外側の gzip が小さな応答まで
    圧縮してしまう)。ヘッダは応答開始メッセージに足すだけで、本文には触れない。

    処理されなかった例外の 500 もここで返す(#D6-11)。Starlette の既定の 500 は
    すべてのミドルウェアの外側(ServerErrorMiddleware)で作られ、セキュリティヘッダが
    付かないため。応答には相関 ID だけを含め(他の 500 と同じ JSON の形)、例外は
    送り直してサーバーのログ(トレースバック)とテストの挙動は今までどおりにする。
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = False

        async def send_with_headers(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                headers = MutableHeaders(scope=message)
                for name, value in _SECURITY_HEADERS:
                    headers.setdefault(name, value)
            await send(message)

        try:
            await self.app(scope, receive, send_with_headers)
        except Exception:
            if started:
                raise
            ref = uuid.uuid4().hex[:8]
            logger.error(
                "[%s] 処理されなかった例外: %s %s",
                ref,
                scope.get("method", ""),
                scope.get("path", ""),
            )
            response = JSONResponse(
                {
                    "detail": {
                        "code": "INTERNAL_ERROR",
                        "message": f"内部エラーが発生しました (ref={ref})",
                    }
                },
                status_code=500,
            )
            await response(scope, receive, send_with_headers)
            raise


#: 使う設定の版を知らせる応答ヘッダ(画面で設定を変えたら、開いている画面が読み直す)。
CONFIG_REVISION_HEADER = "X-Monitor-Config"


class _ConfigRevisionMiddleware:
    """全レスポンスに使う設定の版(:data:`CONFIG_REVISION_HEADER`)を付ける。

    画面(core.js)はページの版(``<meta name="monitor-config">``)と比べ、違えば読み直す
    (設定ページで閾値やタイトルを変えたら、大型表示などがそのまま新しい設定になる)。
    """

    def __init__(self, app: ASGIApp, services: Services) -> None:
        self.app = app
        self.services = services

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_revision(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers[CONFIG_REVISION_HEADER] = self.services.config_revision
            await send(message)

        await self.app(scope, receive, send_with_revision)


def _warn_about_weak_api_key(settings: AppSettings) -> None:
    """説明用の値・短すぎる・ASCII 以外の API キーを起動時に警告する(#D6-17・#D6-07)。"""
    for problem in configured_key_problems(settings.api_key, settings.admin_key):
        logger.warning("%s(%s)", problem.message, problem.hint)


def create_app(
    config: MonitorConfig,
    settings: AppSettings | None = None,
    *,
    init_db: bool = True,
) -> FastAPI:
    settings = settings or AppSettings()
    services = build_services(config, settings, init_db=init_db)
    # 画面で変えた設定(DB に保存した差分)を重ねた、使う設定
    config = services.config

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # バックグラウンドタスク(フォルダ監視・アラート評価)を起動し、終了時に
        # 停止してから DB のコネクションを閉じる。
        try:
            await services.start()
            yield
        finally:
            try:
                await services.stop()
            finally:
                services.close()

    # API ドキュメントは web/api_docs.py が登録する(版を固定した CDN と SRI、
    # protect_reads でのログイン、MONITOR_API_DOCS=false での非公開)。
    app = FastAPI(
        title=config.app_title,
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    def follow_config(new: MonitorConfig) -> None:
        # 画面で設定を変えたら、API ドキュメントのタイトルも合わせる
        app.state.config = new
        app.title = new.app_title
        app.openapi_schema = None

    services.config_listeners.append(follow_config)

    # --- 状態(app.state に保持し、依存から参照する)---
    app.state.services = services
    app.state.settings = settings
    app.state.config = config
    app.state.db = services.db
    app.state.registry = services.registry
    app.state.connectors = services.connectors
    app.state.audit_service = services.audit_service
    app.state.view_cache = services.view_cache
    app.state.crud_service = services.crud_service
    app.state.view_service = services.view_service
    app.state.alert_engine = services.alert_engine
    app.state.kpi_service = services.kpi_service
    # ログアウトで無効にしたセッション → 失効する時刻(api/deps.py)
    app.state.revoked_sessions = {}

    # --- ビュー(HTML)とアセット ---
    # テンプレートの URL はルート相対(サブパス込み)、静的ファイルは ?v=<版> 付きで
    # 長期キャッシュさせる(web/serving.py)。
    web = _web_dir()
    app.state.templates = create_templates(web / "templates")
    app.mount(
        "/static", VersionedStaticFiles(directory=str(web / "static")), name="static"
    )

    # --- ミドルウェア・ハンドラ・ルーター ---
    # add_middleware は後に追加したものほど外側になる。サブパスの補正は圧縮・CORS・
    # ルーティングより外側で行い、それらが揃った scope を見るようにする。セキュリティ
    # ヘッダはさらに外側にし、サブパスの補正が返すリダイレクトにも付ける。
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    if settings.compress_responses:
        app.add_middleware(CompressionMiddleware)
    app.add_middleware(_ConfigRevisionMiddleware, services=services)
    app.add_middleware(RootPathMiddleware, root_path=settings.root_path)
    # DNS リバインディング対策(この PC だけで待ち受けるとき・MONITOR_ALLOWED_HOSTS)。
    # 断る応答にもセキュリティヘッダを付けるため、その内側に置く。
    policy = host_policy(
        bound_host=settings.host,
        allowed_hosts=settings.allowed_hosts,
        root_path=settings.root_path,
        forwarded_allow_ips=settings.forwarded_allow_ips,
    )
    if policy is not None:
        app.add_middleware(HostGuardMiddleware, policy=policy)
    app.add_middleware(_SecurityHeadersMiddleware)

    register_exception_handlers(app)
    if settings.api_docs:
        # FastAPI の既定と同じく、アプリのルーターより先に登録する。
        install_api_docs(app)
    for router in _ROUTERS:
        app.include_router(router)
    _warn_if_root_path_shadows_routes(app, settings.root_path)
    _warn_about_weak_api_key(settings)

    return app


def _warn_if_root_path_shadows_routes(app: FastAPI, root_path: str) -> None:
    """サブパスの先頭がアプリ自身のパス(/api, /static 等)と同じなら警告する。

    プレフィックスを外して転送するプロキシの後ろでは、``/api/...`` が「サブパス付き」
    なのか「外した後」なのかを区別できず、正しく振り分けられない。
    """
    first = root_path.strip("/").split("/", 1)[0]
    if not first:
        return
    paths = [getattr(route, "path", "") for route in app.routes]  # /docs, /static 等
    paths += [route.path for router in _ROUTERS for route in router.routes]
    if first in {path.strip("/").split("/", 1)[0] for path in paths}:
        logger.warning(
            "MONITOR_ROOT_PATH=%s はアプリのパス /%s と重なります。"
            "プロキシがプレフィックスを外して転送すると正しく動かないため、"
            "別の名前(例: /monitor)にしてください",
            root_path,
            first,
        )


def app_factory() -> FastAPI:
    """環境変数からアプリを組み立てるファクトリ。

    ``MONITOR_CONFIG_PATH`` で示された ``config.py`` を読み込む(プロジェクトのフォルダ
    や ``~`` で始まるパスも可。CLI の ``--config`` と同じ)。uvicorn の
    ``--reload`` は import 文字列を要求するため、CLI はこの関数を指定する。
    実行時設定は作業フォルダの ``.env`` に加えて、config.py のフォルダの ``.env`` も
    読む(``runserver --config <別のフォルダ>`` の CLI 側と同じ規則。作業フォルダの
    .env が優先)。設定に誤りがあれば長いトレースバックではなく 1 行のエラーを
    記録して終了する(``--reload`` 中なら、次に config.py を保存したときに再読み込み
    される)。
    """
    from .db.engine import DatabaseWaitAborted
    from .logging_conf import configure_logging
    from .settings.loader import ConfigError, load_config, resolve_config_path
    from .settings.runtime import load_app_settings, settings_error_message

    # CLI の --config と同じく、フォルダ(その中の config.py)と ~ も受け付ける
    config_path = resolve_config_path(
        os.environ.get("MONITOR_CONFIG_PATH", "config.py")
    )
    try:
        settings = load_app_settings(config_path)
    except Exception as exc:
        message = settings_error_message(exc)
        if message is None:
            raise
        # MONITOR_PORT=abc など: トレースバックではなく、変数名と指定できる値を 1 行で
        logger.error("%s(起動を中止します)", message)
        raise SystemExit(1) from None
    # MONITOR_LOG_FORMAT を反映する(runserver を通さず uvicorn から直接使う場合も)。
    configure_logging(settings.log_level, log_format=settings.log_format)
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        logger.error("設定を読み込めないため起動を中止します: %s", exc)
        raise SystemExit(1) from None
    try:
        return create_app(config, settings)
    except DatabaseWaitAborted:
        # データベースの起動待ちの間に停止の指示(docker stop など)を受けた。
        logger.info(
            "停止の指示を受けたため、データベースの起動待ちを中止して終了します"
        )
        raise SystemExit(0) from None
