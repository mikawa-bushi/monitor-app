"""ドメイン例外を JSON レスポンスに変換するハンドラ。"""

from __future__ import annotations

import logging
import uuid
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.exc import DBAPIError

from ..db import errors as db_errors
from ..exceptions import MonitorAppError, SchemaMismatchError
from ..web.serving import app_root_path
from .deps import RedirectToLogin

logger = logging.getLogger("monitor_app.api")


def _body(code: str, message: str) -> dict:
    return {"detail": {"code": code, "message": message}}


def _db_error_response(request: Request, exc: DBAPIError) -> JSONResponse:
    """DB ドライバの例外を JSON のエラーエンベロープに変換する(CRIT-01, DEPLOY-13)。

    SQL 文やパラメータは応答に含めない。409/422 は送信側の誤りなので理由を返し、
    500 系は相関 ID を返して詳細をサーバーのログに残す。
    """
    kind = db_errors.classify(exc)
    if kind == db_errors.CONFLICT:
        message = db_errors.conflict_message(exc)
        logger.info(
            "DB constraint violation on %s %s: %s",
            request.method,
            request.url.path,
            db_errors.driver_message(exc),
        )
        return JSONResponse(status_code=409, content=_body("CONFLICT", message))
    if kind == db_errors.INVALID:
        message = db_errors.invalid_message(exc)
        logger.info(
            "DB rejected a value on %s %s: %s",
            request.method,
            request.url.path,
            db_errors.driver_message(exc),
        )
        return JSONResponse(status_code=422, content=_body("INVALID_PAYLOAD", message))
    if kind == db_errors.BUSY:
        # ほかの書き込み(大きな CSV の取り込みなど)のロック待ちの時間切れ。一時的な
        # ものなので 503 と Retry-After で再送を促し、トレースバックは残さない(D4-06)。
        logger.warning(
            "DB busy on %s %s: %s",
            request.method,
            request.url.path,
            db_errors.driver_message(exc),
        )
        return JSONResponse(
            status_code=503,
            content=_body("DATABASE_BUSY", db_errors.busy_message()),
            headers={"Retry-After": "2"},
        )
    ref = uuid.uuid4().hex[:8]
    if kind == db_errors.SCHEMA:
        logger.error(
            "[%s] DB_SCHEMA_MISMATCH on %s %s: %s",
            ref,
            request.method,
            request.url.path,
            db_errors.driver_message(exc),
        )
        message = f"{db_errors.schema_message(exc)} (ref={ref})"
        return JSONResponse(
            status_code=500, content=_body("DB_SCHEMA_MISMATCH", message)
        )
    logger.error(
        "[%s] DATABASE_ERROR on %s %s: %s",
        ref,
        request.method,
        request.url.path,
        db_errors.driver_message(exc),
        exc_info=exc,
    )
    return JSONResponse(
        status_code=500,
        content=_body(
            "DATABASE_ERROR", f"データベースエラーが発生しました (ref={ref})"
        ),
    )


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(DBAPIError)
    async def _handle_db_error(request: Request, exc: DBAPIError):
        return _db_error_response(request, exc)

    @app.exception_handler(SchemaMismatchError)
    async def _handle_schema_mismatch(request: Request, exc: SchemaMismatchError):
        # 500 系だが利用者が直せる設定の問題なので、案内(メッセージ)も返す。
        ref = uuid.uuid4().hex[:8]
        logger.error(
            "[%s] %s on %s %s: %s",
            ref,
            exc.code,
            request.method,
            request.url.path,
            exc.message,
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=_body(exc.code, f"{exc.message} (ref={ref})"),
        )

    @app.exception_handler(MonitorAppError)
    async def _handle_domain(_request: Request, exc: MonitorAppError):
        # 500 系は内部詳細を漏らさず、相関 ID だけ返してログに残す。
        if exc.status_code >= 500:
            ref = uuid.uuid4().hex[:8]
            logger.error("[%s] %s: %s", ref, exc.code, exc.message, exc_info=exc)
            return JSONResponse(
                status_code=exc.status_code,
                content=_body(exc.code, f"内部エラーが発生しました (ref={ref})"),
            )
        return JSONResponse(
            status_code=exc.status_code, content=_body(exc.code, exc.message)
        )

    @app.exception_handler(RedirectToLogin)
    async def _handle_login_redirect(request: Request, exc: RedirectToLogin):
        # 未認証の HTML ページアクセスはログインページへ誘導する(#11)。
        # サブパス配信(MONITOR_ROOT_PATH)ではサブパスの内側のログインページへ。
        url = app_root_path(request) + "/login"
        if exc.next_path:
            url += "?next=" + quote(exc.next_path)
        return RedirectResponse(url=url, status_code=303)
