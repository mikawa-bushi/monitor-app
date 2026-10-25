"""データ取り込みエンドポイント(フェーズ1・C)。

センサ / PLC / MES がレコードを直接 POST するための口。手動 CSV 取り込みに
頼らず、連続的に発生する現場データを受け付ける。
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Body, Depends, Query

from ...services.crud_service import CrudService
from ..deps import get_crud_service, require_write_auth

router = APIRouter(prefix="/api/ingest", tags=["Ingest"])


@router.post(
    "/{table_name}",
    responses={
        409: {
            "description": "主キーが既存の行と重複した(on_conflict=error のとき。"
            "バッチ全体を取り消す)"
        }
    },
)
def ingest(
    table_name: str,
    payload: dict[str, Any] | list[dict[str, Any]] = Body(...),
    on_conflict: Literal["error", "ignore", "update"] = Query(
        "error",
        description="主キーが既存の行と重複したときの扱い。error=バッチ全体を拒否(409)、"
        "ignore=既存の行を読み飛ばす(再送しても重複しない)、update=既存の行を上書きする",
    ),
    service: CrudService = Depends(get_crud_service),
    _: None = Depends(require_write_auth),
):
    """1 件または複数件のレコードを取り込む。

    body は単一オブジェクトでも配列でもよい。未知の列は捨て、列の論理型に変換する。
    応答は ``{"success": true, "inserted": n}``。``on_conflict=ignore`` では
    読み飛ばした行数 ``skipped``、``on_conflict=update`` では上書きした行数
    ``updated`` も返す。
    """
    records = payload if isinstance(payload, list) else [payload]
    result = service.ingest_batch(table_name, records, on_conflict=on_conflict)
    return {"success": True, **result}
