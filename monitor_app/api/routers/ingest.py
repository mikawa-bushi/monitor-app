"""データ取り込みエンドポイント(フェーズ1・C)。

センサ / PLC / MES がレコードを直接 POST するための口。手動 CSV 取り込みに
頼らず、連続的に発生する現場データを受け付ける。
"""

from __future__ import annotations

from typing import Any, Dict, List, Union

from fastapi import APIRouter, Body, Depends

from ..deps import get_crud_service, require_write_auth
from ...services.crud_service import CrudService

router = APIRouter(prefix="/api/ingest", tags=["Ingest"])


@router.post("/{table_name}")
def ingest(
    table_name: str,
    payload: Union[Dict[str, Any], List[Dict[str, Any]]] = Body(...),
    service: CrudService = Depends(get_crud_service),
    _: None = Depends(require_write_auth),
):
    """1 件または複数件のレコードを取り込む。

    body は単一オブジェクトでも配列でもよい。未知の列は捨て、列の論理型に変換する。
    """
    records = payload if isinstance(payload, list) else [payload]
    inserted = service.ingest_records(table_name, records)
    return {"success": True, "inserted": inserted}
