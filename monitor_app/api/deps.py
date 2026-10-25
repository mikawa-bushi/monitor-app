"""FastAPI 依存。アプリ状態(app.state)へのアクセスと API キー / セッション検証。

認証(#11): 書き込み系は ``api_key`` 設定時に必須、読み取り系は ``protect_reads``
有効時に必須。クライアントは **X-API-Key ヘッダ** か **セッション Cookie** の
どちらかで認証できる。Cookie は同梱 Web UI がブラウザから認証するための経路で、
``/api/login`` がキー検証後に発行する(値は発行時刻とキーの HMAC で、生キーは
保存しない)。セッションは発行から :data:`SESSION_MAX_AGE` 秒で失効し、ログアウトした
セッションはサーバー側でも無効にする(#D6-13)。

キーの比較はバイト列で行う(#D6-07)。ASCII 以外の文字を含むキーやヘッダでも
500 にならず、一致しなければ 401 になる。
"""

from __future__ import annotations

import hashlib
import hmac
import time

from fastapi import Depends, HTTPException, Request, Security
from fastapi.security import APIKeyHeader

from ..services.alert_service import AlertEngine
from ..services.audit_service import AuditService
from ..services.crud_service import CrudService
from ..services.kpi_service import KpiService
from ..services.view_service import ViewService
from ..settings.declarative import MonitorConfig
from ..settings.runtime import AppSettings

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

#: セッション Cookie 名(#11)。
SESSION_COOKIE = "monitor_session"
#: セッションの有効期間(秒)。Cookie の Max-Age と同じ 12 時間。
SESSION_MAX_AGE = 60 * 60 * 12
#: 発行時刻が未来になっているセッションを受け付ける幅(秒。時計の補正に備える)。
_CLOCK_SKEW = 60


def _signature(api_key: str, issued_at: int) -> str:
    return hmac.new(
        api_key.encode("utf-8"),
        f"monitor-session:{issued_at}".encode("ascii"),
        hashlib.sha256,
    ).hexdigest()


def session_token(api_key: str, issued_at: int | None = None) -> str:
    """API キーから導出する署名付きセッショントークン(``<発行時刻>.<HMAC>``)。

    生キーを Cookie に置かないための HMAC。ステートレスなので再起動しても検証できる。
    発行時刻を署名に含め、:data:`SESSION_MAX_AGE` を過ぎたものは受け付けない。
    空のキーからは作らない(誰でも同じ値を作れてしまう)。
    """
    if not api_key:
        raise ValueError("空の API キーからはセッションを作れません")
    if issued_at is None:
        issued_at = int(time.time())
    return f"{issued_at}.{_signature(api_key, issued_at)}"


def _same_secret(provided: bytes, expected: bytes) -> bool:
    return hmac.compare_digest(provided, expected)


def api_key_matches(provided: str, api_key: str | None) -> bool:
    """X-API-Key ヘッダの値がキーと一致するか。

    Starlette はヘッダを latin-1 で文字列にするため、latin-1 で戻すと送られたバイト列に
    なる。キーは UTF-8 のバイト列と比べる(日本語のキーも UTF-8 で送れば一致する)。
    """
    if not api_key or not provided:
        return False
    return _same_secret(provided.encode("latin-1", "replace"), api_key.encode("utf-8"))


def login_key_matches(provided: str, api_key: str | None) -> bool:
    """ログインのフォームで入力されたキー(UTF-8 の文字列)がキーと一致するか。"""
    if not api_key:
        return False
    return _same_secret(provided.encode("utf-8"), api_key.encode("utf-8"))


def _revoked_sessions(request: Request) -> dict[str, float]:
    """ログアウトで無効にしたセッション → 失効する時刻(プロセスごと)。"""
    state = request.app.state
    revoked = getattr(state, "revoked_sessions", None)
    if revoked is None:
        revoked = {}
        state.revoked_sessions = revoked
    return revoked


def session_is_valid(
    token: str | None, api_key: str | None, *, now: float | None = None
) -> bool:
    """セッション Cookie の値が、このキーで発行した有効期間内のものか。"""
    if not token or not api_key:
        return False
    issued, _, signature = token.partition(".")
    if not issued.isdigit() or not signature:
        return False
    issued_at = int(issued)
    current = time.time() if now is None else now
    if not -_CLOCK_SKEW <= current - issued_at <= SESSION_MAX_AGE:
        return False
    return _same_secret(
        signature.encode("latin-1", "replace"),
        _signature(api_key, issued_at).encode("ascii"),
    )


def revoke_session(request: Request, token: str | None) -> None:
    """ログアウトしたセッションを、有効期間が切れるまでサーバー側でも無効にする。"""
    settings: AppSettings = request.app.state.settings
    if not session_is_valid(token, settings.api_key):
        return
    now = time.time()
    revoked = _revoked_sessions(request)
    for old, until in list(revoked.items()):  # 期限の切れたものを捨てる
        if until <= now:
            del revoked[old]
    issued_at = int(str(token).partition(".")[0])
    revoked[str(token)] = issued_at + SESSION_MAX_AGE


def get_settings(request: Request) -> AppSettings:
    return request.app.state.settings


def get_config(request: Request) -> MonitorConfig:
    return request.app.state.config


def get_crud_service(request: Request) -> CrudService:
    return request.app.state.crud_service


def get_view_service(request: Request) -> ViewService:
    return request.app.state.view_service


def get_alert_engine(request: Request) -> AlertEngine:
    """AlertEngine を返す(フェーズ1・A)。"""
    return request.app.state.alert_engine


def get_kpi_service(request: Request) -> KpiService:
    """KpiService を返す(フェーズ2・E)。"""
    return request.app.state.kpi_service


def get_audit_service(request: Request) -> AuditService:
    """AuditService を返す(フェーズ4・G)。"""
    return request.app.state.audit_service


def _is_authed(settings: AppSettings, request: Request, provided: str | None) -> bool:
    """ヘッダ or セッション Cookie で認証済みか。api_key 未設定(空を含む)なら常に True。"""
    if not settings.api_key:
        return True
    if provided and api_key_matches(provided, settings.api_key):
        return True
    cookie = request.cookies.get(SESSION_COOKIE)
    return session_is_valid(
        cookie, settings.api_key
    ) and cookie not in _revoked_sessions(request)


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
    if (
        settings.protect_reads
        and settings.api_key
        and not _is_authed(settings, request, provided)
    ):
        raise RedirectToLogin()
