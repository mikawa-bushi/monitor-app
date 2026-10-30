"""UI 向けセッション認証エンドポイント(#11)。

``protect_reads``/``api_key`` 有効時に、ブラウザからキーを渡して同梱 UI を使える
ようにする。検証成功で HttpOnly な署名付き Cookie を発行し、以後の同一オリジン
fetch が自動的に認証される。

サブパス配信(``MONITOR_ROOT_PATH``)では、リダイレクト先をサブパスの内側にし、
Cookie の Path もサブパスに限定する(同じホストの別インスタンスとセッションが
混ざらない)。ルートで公開している既定では今までどおり ``/`` 基準。

セッションは発行時刻を署名に含め、12 時間で失効する。ログアウトしたセッションは
サーバー側でも無効にする(Cookie の値を控えていても使えない)。

管理者の鍵(``MONITOR_ADMIN_KEY``)でログインすると、管理者のセッション Cookie を
発行する(管理の画面と、閲覧・書き込みの両方に使える)。
"""

from __future__ import annotations

import re
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from ...settings.runtime import AppSettings
from ...web.serving import app_root_path, cookie_path
from ..deps import (
    ADMIN_SESSION_COOKIE,
    SESSION_COOKIE,
    SESSION_MAX_AGE,
    get_settings,
    login_key_matches,
    revoke_session,
    session_token,
)

router = APIRouter(prefix="/api", tags=["Auth"])

#: ログインの後に戻れるパス(アプリ内の絶対パスだけ。ほかのサイトへは戻さない)。
_NEXT_RE = re.compile(r"/(?:[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*)?")


def safe_next(value: str | None) -> str:
    """ログインの後に戻るパス。アプリ内のパスでなければ ``/``(オープンリダイレクト対策)。"""
    return value if value and _NEXT_RE.fullmatch(value) else "/"


def _set_session(request: Request, response, name: str, key: str) -> None:
    response.set_cookie(
        name,
        session_token(key),
        path=cookie_path(request),
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        max_age=SESSION_MAX_AGE,  # 12 時間(サーバー側でも同じ期間で失効させる)
    )


@router.post("/login")
def login(
    request: Request,
    key: str = Form(...),
    next: str = Form("/"),
    settings: AppSettings = Depends(get_settings),
):
    """キーを検証し、成功なら HttpOnly セッション Cookie を発行して ``next``(既定は /)へ。"""
    root = app_root_path(request)
    target = safe_next(next)
    is_user = login_key_matches(key, settings.api_key)
    is_admin = login_key_matches(key, settings.admin_key)
    if (settings.api_key or settings.admin_key) and not (is_user or is_admin):
        query = "error=1" if target == "/" else f"error=1&next={quote(target)}"
        return RedirectResponse(url=f"{root}/login?{query}", status_code=303)

    resp = RedirectResponse(url=f"{root}{target}", status_code=303)
    if is_user and settings.api_key:
        _set_session(request, resp, SESSION_COOKIE, settings.api_key)
    if is_admin and settings.admin_key:
        _set_session(request, resp, ADMIN_SESSION_COOKIE, settings.admin_key)
    return resp


@router.post("/logout")
def logout(request: Request, settings: AppSettings = Depends(get_settings)):
    """セッション Cookie(管理者のものも)を失効させ、ログインページへ。"""
    revoke_session(request, request.cookies.get(SESSION_COOKIE), settings.api_key)
    revoke_session(
        request, request.cookies.get(ADMIN_SESSION_COOKIE), settings.admin_key
    )
    resp = RedirectResponse(url=f"{app_root_path(request)}/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE, path=cookie_path(request))
    resp.delete_cookie(ADMIN_SESSION_COOKIE, path=cookie_path(request))
    return resp
