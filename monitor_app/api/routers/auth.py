"""UI 向けセッション認証エンドポイント(#11)。

``protect_reads``/``api_key`` 有効時に、ブラウザからキーを渡して同梱 UI を使える
ようにする。検証成功で HttpOnly な署名付き Cookie を発行し、以後の同一オリジン
fetch が自動的に認証される。
"""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from ..deps import SESSION_COOKIE, get_settings, session_token
from ...settings.runtime import AppSettings

router = APIRouter(prefix="/api", tags=["Auth"])


@router.post("/login")
def login(
    request: Request,
    key: str = Form(...),
    settings: AppSettings = Depends(get_settings),
):
    """キーを検証し、成功なら HttpOnly セッション Cookie を発行して / へ。"""
    if settings.api_key is not None and not secrets.compare_digest(
        key, settings.api_key
    ):
        return RedirectResponse(url="/login?error=1", status_code=303)

    resp = RedirectResponse(url="/", status_code=303)
    if settings.api_key is not None:
        resp.set_cookie(
            SESSION_COOKIE,
            session_token(settings.api_key),
            httponly=True,
            samesite="lax",
            secure=request.url.scheme == "https",
            max_age=60 * 60 * 12,  # 12 時間
        )
    return resp


@router.post("/logout")
def logout():
    """セッション Cookie を失効させ、ログインページへ。"""
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp
