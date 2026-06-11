"""FastAPI 依存。アプリ状態(app.state)へのアクセスと API キー検証。"""

from __future__ import annotations

import secrets

from fastapi import Depends, Request, Security
from fastapi.security import APIKeyHeader

from ..services.crud_service import CrudService
from ..services.view_service import ViewService
from ..settings.declarative import MonitorConfig
from ..settings.runtime import AppSettings

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def get_settings(request: Request) -> AppSettings:
    return request.app.state.settings


def get_config(request: Request) -> MonitorConfig:
    return request.app.state.config


def get_crud_service(request: Request) -> CrudService:
    return request.app.state.crud_service


def get_view_service(request: Request) -> ViewService:
    return request.app.state.view_service


def get_alert_engine(request: Request):
    """AlertEngine を返す(フェーズ1・A)。"""
    return request.app.state.alert_engine


def get_kpi_service(request: Request):
    """KpiService を返す(フェーズ2・E)。"""
    return request.app.state.kpi_service


def get_audit_service(request: Request):
    """AuditService を返す(フェーズ4・G)。"""
    return request.app.state.audit_service


def get_actor(
    request: Request,
    provided: str | None = Security(_api_key_header),
) -> str:
    """監査用の実行主体を表す文字列(フェーズ4・G)。

    API キー認証されていれば "api"、そうでなければ "anon" とし、クライアント IP を添える。
    """
    client = request.client.host if request.client else "unknown"
    who = "api" if provided else "anon"
    return f"{who}@{client}"


def _check_key(settings: AppSettings, provided: str | None) -> None:
    from fastapi import HTTPException

    if settings.api_key is None:
        return
    if not provided or not secrets.compare_digest(provided, settings.api_key):
        raise HTTPException(
            status_code=401,
            detail={"code": "UNAUTHORIZED", "message": "API キーが無効です"},
        )


def require_write_auth(
    settings: AppSettings = Depends(get_settings),
    provided: str | None = Security(_api_key_header),
) -> None:
    """書き込み系(POST/PUT/DELETE)で必須。api_key 未設定なら素通り。"""
    _check_key(settings, provided)


def require_read_auth(
    settings: AppSettings = Depends(get_settings),
    provided: str | None = Security(_api_key_header),
) -> None:
    """読み取り系。protect_reads=True かつ api_key 設定時のみ検証。"""
    if settings.protect_reads:
        _check_key(settings, provided)
