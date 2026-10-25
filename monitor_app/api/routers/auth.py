"""UI 向けセッション認証エンドポイント(#11)。

``protect_reads``/``api_key`` 有効時に、ブラウザからキーを渡して同梱 UI を使える
ようにする。検証成功で HttpOnly な署名付き Cookie を発行し、以後の同一オリジン
fetch が自動的に認証される。

サブパス配信(``MONITOR_ROOT_PATH``)では、リダイレクト先をサブパスの内側にし、
Cookie の Path もサブパスに限定する(同じホストの別インスタンスとセッションが
混ざらない)。ルートで公開している既定では今までどおり ``/`` 基準。

セッションは発行時刻を署名に含め、12 時間で失効する。ログアウトしたセッションは
サーバー側でも無効にする(Cookie の値を控えていても使えない)。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from ...settings.runtime import AppSettings
from ...web.serving import app_root_path, cookie_path
from ..deps import (
    SESSION_COOKIE,
    SESSION_MAX_AGE,
    get_settings,
    login_key_matches,
    revoke_session,
    session_token,
)

router = APIRouter(prefix="/api", tags=["Auth"])


@router.post("/login")
def login(
    request: Request,
    key: str = Form(...),
    settings: AppSettings = Depends(get_settings),
):
    """キーを検証し、成功なら HttpOnly セッション Cookie を発行して / へ。"""
    root = app_root_path(request)
    if settings.api_key and not login_key_matches(key, settings.api_key):
        return RedirectResponse(url=f"{root}/login?error=1", status_code=303)

    resp = RedirectResponse(url=f"{root}/", status_code=303)
    if settings.api_key:
        resp.set_cookie(
            SESSION_COOKIE,
            session_token(settings.api_key),
            path=cookie_path(request),
            httponly=True,
            samesite="lax",
            secure=request.url.scheme == "https",
            max_age=SESSION_MAX_AGE,  # 12 時間(サーバー側でも同じ期間で失効させる)
        )
    return resp


@router.post("/logout")
def logout(request: Request):
    """セッション Cookie を失効させ、ログインページへ。"""
    revoke_session(request, request.cookies.get(SESSION_COOKIE))
    resp = RedirectResponse(url=f"{app_root_path(request)}/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE, path=cookie_path(request))
    return resp
