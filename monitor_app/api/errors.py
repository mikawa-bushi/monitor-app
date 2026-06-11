"""ドメイン例外を JSON レスポンスに変換するハンドラ。"""

from __future__ import annotations

import logging
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ..exceptions import MonitorAppError

logger = logging.getLogger("monitor_app.api")


def _body(code: str, message: str) -> dict:
    return {"detail": {"code": code, "message": message}}


def register_exception_handlers(app: FastAPI) -> None:
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
