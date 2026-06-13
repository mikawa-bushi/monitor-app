"""アプリファクトリ。``create_app`` が設定を受け取り FastAPI を組み立てる。

import 時に副作用(DB 接続や app 生成)を起こさないため、すべての初期化を
``create_app`` 内に閉じ込める。これによりテストで設定を差し替えやすい。
"""

from __future__ import annotations

import importlib.resources as resources
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.cors import CORSMiddleware

from .api.errors import register_exception_handlers
from .api.routers import alerts, audit, auth, crud, ingest, meta, pages, views
from .db.engine import Database
from .db.ingest_state import IngestStateStore
from .db.registry import TableRegistry
from .db.repository import TableRepository
from .services.alert_scheduler import AlertScheduler
from .services.alert_service import AlertEngine
from .services.audit_service import AuditService
from .services.connectors import build_connectors
from .services.crud_service import CrudService
from .services.importer import CsvImporter
from .services.ingest_watcher import IngestWatcher
from .services.kpi_service import KpiService
from .services.view_cache import ViewCache
from .services.view_service import ViewService
from .settings.declarative import MonitorConfig
from .settings.runtime import AppSettings


def _web_dir() -> Path:
    return Path(str(resources.files("monitor_app") / "web"))


def create_app(
    config: MonitorConfig,
    settings: Optional[AppSettings] = None,
    *,
    init_db: bool = True,
) -> FastAPI:
    settings = settings or AppSettings()

    db = Database(settings.database_url, echo=False)
    registry = TableRegistry(config)
    if init_db:
        registry.create_all(db)
    repository = TableRepository(db, registry)

    # ソースコネクタ(config.sources が定義されているとき常に組み立てる)。
    connectors = None
    if config.sources:
        state = IngestStateStore(db)
        state.create()
        connectors = build_connectors(config, registry, db, state)

    # 共有サービス(lifespan のスケジューラからも参照するため先に生成する)。
    view_cache = ViewCache(settings.view_cache_ttl_ms)
    view_service = ViewService(config, db, cache=view_cache)
    alert_engine = AlertEngine(config, settings)

    # フォルダ監視(フェーズ1・C)。lifespan で起動/停止する。
    watcher: Optional[IngestWatcher] = None
    if settings.ingest_watch:
        importer = CsvImporter(
            config, registry, db, settings.csv_dir, encoding=settings.csv_encoding
        )
        watcher = IngestWatcher(
            importer,
            settings.ingest_interval,
            keep=settings.ingest_mode == "append",
            connectors=connectors,
        )

    # アラートのバックグラウンド評価(#13)。alerts があれば常に起動し、
    # 画面を誰も開いていなくても閾値を評価・通知する。
    alert_scheduler: Optional[AlertScheduler] = None
    if config.alerts:
        interval_s = max(config.refresh_interval_ms, 1000) / 1000.0
        alert_scheduler = AlertScheduler(alert_engine, view_service, interval_s)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if watcher:
            watcher.start()
        if alert_scheduler:
            alert_scheduler.start()
        try:
            yield
        finally:
            if watcher:
                await watcher.stop()
            if alert_scheduler:
                await alert_scheduler.stop()

    app = FastAPI(
        title=config.app_title,
        docs_url="/docs",
        redoc_url="/redoc",
        lifespan=lifespan,
    )

    # --- 状態の組み立て(app.state に保持し、依存から参照する)---
    app.state.settings = settings
    app.state.config = config
    app.state.db = db
    app.state.registry = registry
    app.state.connectors = connectors
    audit_svc = AuditService(db, settings.audit_enabled)
    app.state.audit_service = audit_svc
    app.state.view_cache = view_cache
    app.state.crud_service = CrudService(
        config, registry, repository, audit_svc, view_cache=view_cache
    )
    app.state.view_service = view_service
    app.state.alert_engine = alert_engine
    app.state.kpi_service = KpiService(config, db)

    # --- ビュー(HTML)とアセット ---
    web = _web_dir()
    app.state.templates = Jinja2Templates(directory=str(web / "templates"))
    app.mount("/static", StaticFiles(directory=str(web / "static")), name="static")

    # --- ミドルウェア・ハンドラ・ルーター ---
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    register_exception_handlers(app)
    app.include_router(crud.router)
    app.include_router(views.router)
    app.include_router(ingest.router)
    app.include_router(alerts.router)
    app.include_router(audit.router)
    app.include_router(auth.router)
    app.include_router(meta.router)
    app.include_router(pages.router)

    return app


def app_factory() -> FastAPI:
    """環境変数からアプリを組み立てるファクトリ。

    ``MONITOR_CONFIG_PATH`` で示された ``config.py`` を読み込む。uvicorn の
    ``--reload`` は import 文字列を要求するため、CLI はこの関数を指定する。
    """
    from .logging_conf import configure_logging
    from .settings.loader import load_config

    settings = AppSettings()
    configure_logging(settings.log_level)
    config_path = os.environ.get("MONITOR_CONFIG_PATH", "config.py")
    config = load_config(config_path)
    return create_app(config, settings)
