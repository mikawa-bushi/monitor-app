"""メタ情報エンドポイント(スキーマ一覧・ヘルスチェック)。"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import anyio
from anyio.lowlevel import RunVar
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import text

from ... import __version__
from ...services.crud_service import CrudService
from ...services.kpi_service import KpiService
from ..deps import (
    get_crud_service,
    get_kpi_service,
    require_read_auth,
)
from ..schemas import SchemaResponse

if TYPE_CHECKING:
    from ...container import Services
    from ...services.periodic import PeriodicTask

router = APIRouter(prefix="/api", tags=["Meta"])
logger = logging.getLogger("monitor_app.api")

#: ヘルスチェック専用のスレッドの最大数。ほかの API や SSE が AnyIO の既定の
#: スレッドを使い切っていても(遅いビューの計算待ちなど)、死活監視
#: (Docker の HEALTHCHECK など)が時間切れにならないよう、別の枠で DB を確かめる。
HEALTH_THREADS = 4
_health_limiter: RunVar[anyio.CapacityLimiter] = RunVar("monitor_app_health_limiter")


def _health_thread_limiter() -> anyio.CapacityLimiter:
    try:
        return _health_limiter.get()
    except LookupError:
        limiter = anyio.CapacityLimiter(HEALTH_THREADS)
        _health_limiter.set(limiter)
        return limiter


@router.get("/schema", response_model=SchemaResponse)
def get_schema(
    service: CrudService = Depends(get_crud_service),
    _: None = Depends(require_read_auth),
):
    """全テーブルのスキーマ(列・主キー・外部キー)を返す。"""
    return SchemaResponse(tables=service.schema())


@router.get("/kpis")
def get_kpis(
    service: KpiService = Depends(get_kpi_service),
    _: None = Depends(require_read_auth),
):
    """全 KPI カードを評価して返す(フェーズ2・E)。"""
    return {"kpis": service.evaluate_all()}


def _database_ok(services: Services) -> bool:
    """DB へ ``SELECT 1`` を実行して応答を確かめる(読み取り専用のコネクションで)。"""
    try:
        with services.db.readonly() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - 理由を問わず「DB 異常」として返す
        # 詳細はサーバーのログにだけ残す(応答には接続先や SQL を出さない)。
        logger.warning(
            "ヘルスチェック: データベースに接続できません (%s: %s)",
            type(exc).__name__,
            str(exc).strip().splitlines()[0] if str(exc).strip() else "",
        )
        return False
    return True


def _task_status(task: PeriodicTask) -> dict[str, Any]:
    last_tick = task.last_tick
    return {
        "running": task.running,
        # 表示はサーバーのローカル時刻(オフセット付きの ISO 8601)。
        "last_tick": (
            last_tick.astimezone().isoformat(timespec="seconds") if last_tick else None
        ),
        "last_error": task.last_error,
    }


def _tasks_status(services: Services) -> dict[str, dict[str, Any]]:
    """バックグラウンドタスク名 → 状態。同名のタスクがあれば ``名前 (2)`` のように区別する。"""
    tasks: dict[str, dict[str, Any]] = {}
    for task in services.background_tasks:
        key, number = task.name, 2
        while key in tasks:
            key, number = f"{task.name} ({number})", number + 1
        tasks[key] = _task_status(task)
    return tasks


def _health(request: Request) -> tuple[int, dict[str, Any]]:
    services: Services = request.app.state.services
    healthy = _database_ok(services)
    body = {
        "status": "ok" if healthy else "error",
        "version": __version__,
        "database": "ok" if healthy else "error",
        "tasks": _tasks_status(services),
    }
    return (200 if healthy else 503), body


@router.get(
    "/health",
    responses={
        200: {"description": "正常(データベースに接続できる)"},
        503: {"description": "データベースに接続できない"},
    },
)
async def health(request: Request):
    """死活監視用(認証不要)。

    データベースへ ``SELECT 1`` を実行し、応答できれば 200、できなければ 503 を返す。
    本文にはバージョンと、バックグラウンドタスク(フォルダ監視・アラート評価など)の
    状態(実行中か・最後に成功した時刻・直近の失敗の種類)を含める。
    """
    status, body = await anyio.to_thread.run_sync(
        _health, request, limiter=_health_thread_limiter()
    )
    return JSONResponse(body, status_code=status)


@router.head("/health", include_in_schema=False)
async def health_head(request: Request) -> Response:
    """HEAD で死活監視するツール向け(本文なし、ステータスは GET と同じ)。"""
    status, _body = await anyio.to_thread.run_sync(
        _health, request, limiter=_health_thread_limiter()
    )
    return Response(status_code=status)
