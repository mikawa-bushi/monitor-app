"""ビュー表示エンドポイント。一括取得と SSE ストリーム。

データ取得のたびにアラートエンジンで閾値を評価し(フェーズ1・A)、現在アクティブな
アラートを payload に付与する。常時表示の Kiosk / ダッシュボードがこの評価を駆動する
(誰も見ていないビューはバックグラウンドの AlertScheduler が評価する — #13)。
"""

from __future__ import annotations

import csv
import io
import json
from typing import Optional

import anyio
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response, StreamingResponse
from sse_starlette.sse import EventSourceResponse

from ..deps import get_alert_engine, get_config, get_view_service, require_read_auth
from ..schemas import ViewDataResponse
from ...services.view_service import ViewService
from ...settings.declarative import MonitorConfig

router = APIRouter(prefix="/api/views", tags=["Views"])


def _build_payload(
    service: ViewService,
    engine,
    view_name: str,
    limit: Optional[int] = None,
    offset: int = 0,
) -> dict:
    """ビューデータを取得し、アラートを評価して付与する。"""
    payload = service.get_view(view_name, limit=limit, offset=offset)
    engine.evaluate_view(view_name, payload["data"])
    payload["alerts"] = engine.active_alerts()
    return payload


@router.get("/batch")
def get_views_batch(
    names: str = Query(..., description="カンマ区切りのビュー名"),
    service: ViewService = Depends(get_view_service),
    engine=Depends(get_alert_engine),
    _: None = Depends(require_read_auth),
):
    """複数ビューを 1 リクエストでまとめて返す(ダッシュボード用 — #19)。

    存在しないビュー名は黙って無視する。共有キャッシュ(#18)を通すため、
    同一ビューを各カードが個別取得するより DB 負荷が下がる。
    """
    from ...exceptions import ViewNotFoundError

    out: dict = {}
    for raw in names.split(","):
        name = raw.strip()
        if not name:
            continue
        try:
            out[name] = _build_payload(service, engine, name)
        except ViewNotFoundError:
            continue
    return {"views": out}


@router.get("/{view_name}", response_model=ViewDataResponse)
def get_view(
    view_name: str,
    limit: Optional[int] = Query(None, ge=1, description="返す最大行数(#17)"),
    offset: int = Query(0, ge=0, description="先頭からのスキップ行数(#17)"),
    service: ViewService = Depends(get_view_service),
    engine=Depends(get_alert_engine),
    _: None = Depends(require_read_auth),
):
    """ビューを一度だけ取得する(ポーリング用)。"""
    return _build_payload(service, engine, view_name, limit=limit, offset=offset)


@router.get("/{view_name}/export")
def export_view(
    view_name: str,
    format: str = "csv",
    service: ViewService = Depends(get_view_service),
    _: None = Depends(require_read_auth),
):
    """ビューを CSV または Excel(xlsx)でダウンロードする(フェーズ4・H)。

    xlsx は openpyxl が導入されている場合のみ。未導入なら CSV にフォールバックする。
    """
    payload = service.get_view(view_name)
    columns = payload["columns"]
    rows = payload["data"]

    if format == "xlsx":
        try:
            from openpyxl import Workbook
        except ImportError:
            format = "csv"  # openpyxl 未導入なら CSV へ
        else:
            wb = Workbook()
            ws = wb.active
            ws.append(columns)
            for row in rows:
                ws.append([row.get(c) for c in columns])
            buf = io.BytesIO()
            wb.save(buf)
            return Response(
                content=buf.getvalue(),
                media_type=(
                    "application/vnd.openxmlformats-officedocument."
                    "spreadsheetml.sheet"
                ),
                headers={
                    "Content-Disposition": f'attachment; filename="{view_name}.xlsx"'
                },
            )

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns)
    writer.writeheader()
    for row in rows:
        writer.writerow({c: row.get(c) for c in columns})
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{view_name}.csv"'},
    )


@router.get("/{view_name}/stream")
async def stream_view(
    view_name: str,
    request: Request,
    service: ViewService = Depends(get_view_service),
    engine=Depends(get_alert_engine),
    config: MonitorConfig = Depends(get_config),
    _: None = Depends(require_read_auth),
):
    """ビューデータを Server-Sent Events で配信する。

    ``refresh_interval_ms`` ごとに再クエリし、内容のハッシュが変化したときだけ
    ``message`` イベントを送る。差分がなければ送信しない。共有キャッシュ(#18)に
    より、複数クライアントが同じビューを購読してもクエリ・ハッシュは 1 回。
    """
    interval = max(config.refresh_interval_ms, 250) / 1000.0

    def _payload_and_hash() -> tuple[dict, str]:
        payload, digest = service.get_view_hashed(view_name)
        engine.evaluate_view(view_name, payload["data"])
        payload["alerts"] = engine.active_alerts()
        return payload, digest

    async def event_generator():
        last_hash: str | None = None
        while True:
            if await request.is_disconnected():
                break
            payload, current = await anyio.to_thread.run_sync(_payload_and_hash)
            if current != last_hash:
                last_hash = current
                yield {"event": "message", "data": json.dumps(payload, default=str)}
            await anyio.sleep(interval)

    return EventSourceResponse(event_generator())
