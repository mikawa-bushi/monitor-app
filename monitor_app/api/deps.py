"""FastAPI 依存。アプリ状態(app.state)へのアクセスと API キー / セッション検証。

認証(#11): 書き込み系は ``api_key`` 設定時に必須、読み取り系は ``protect_reads``
有効時に必須。クライアントは **X-API-Key ヘッダ** か **セッション Cookie** の
どちらかで認証できる。Cookie は同梱 Web UI がブラウザから認証するための経路で、
``/api/login`` がキー検証後に発行する(値はキーの HMAC で、生キーは保存しない)。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from fastapi import Depends, HTTPException, Request, Security
from fastapi.security import APIKeyHeader

from ..services.crud_service import CrudService
from ..services.view_service import ViewService
from ..settings.declarative import MonitorConfig
from ..settings.runtime import AppSettings

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

#: セッション Cookie 名(#11)。
SESSION_COOKIE = "monitor_session"


def session_token(api_key: str) -> str:
    """API キーから導出する署名付きセッショントークン。

    生キーを Cookie に置かないための HMAC。ステートレスなので再起動・多ワーカーでも
    一貫して検証できる。"""
    return hmac.new(
        api_key.encode("utf-8"), b"monitor-session", hashlib.sha256
    ).hexdigest()


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


def _is_authed(settings: AppSettings, request: Request, provided: str | None) -> bool:
    """ヘッダ or セッション Cookie で認証済みか。api_key 未設定なら常に True。"""
    if settings.api_key is None:
        return True
    if provided and secrets.compare_digest(provided, settings.api_key):
        return True
    cookie = request.cookies.get(SESSION_COOKIE)
    if cookie and secrets.compare_digest(cookie, session_token(settings.api_key)):
        return True
    return False


def get_actor(
    request: Request,
    provided: str | None = Security(_api_key_header),
) -> str:
    """監査用の実行主体を表す文字列(フェーズ4・G)。

    ヘッダ認証なら "api"、Cookie セッションなら "ui"、いずれも無ければ "anon"。
    クライアント IP を添える。
    """
    client = request.client.host if request.client else "unknown"
    if provided:
        who = "api"
    elif request.cookies.get(SESSION_COOKIE):
        who = "ui"
    else:
        who = "anon"
    return f"{who}@{client}"


def _check_auth(settings: AppSettings, request: Request, provided: str | None) -> None:
    if not _is_authed(settings, request, provided):
        raise HTTPException(
            status_code=401,
            detail={"code": "UNAUTHORIZED", "message": "API キーが無効です"},
        )


def require_write_auth(
    request: Request,
    settings: AppSettings = Depends(get_settings),
    provided: str | None = Security(_api_key_header),
) -> None:
    """書き込み系(POST/PUT/DELETE)で必須。api_key 未設定なら素通り。"""
    _check_auth(settings, request, provided)


def require_read_auth(
    request: Request,
    settings: AppSettings = Depends(get_settings),
    provided: str | None = Security(_api_key_header),
) -> None:
    """読み取り系。protect_reads=True かつ api_key 設定時のみ検証。"""
    if settings.protect_reads:
        _check_auth(settings, request, provided)


class RedirectToLogin(Exception):
    """HTML ページが未認証のとき送出。/login へのリダイレクトに変換される(#11)。"""


def page_auth_guard(
    request: Request,
    settings: AppSettings = Depends(get_settings),
    provided: str | None = Security(_api_key_header),
) -> None:
    """HTML ページ用ガード。protect_reads+api_key 有効かつ未認証なら /login へ。"""
    if settings.protect_reads and settings.api_key:
        if not _is_authed(settings, request, provided):
            raise RedirectToLogin()
