"""テーブル CRUD エンドポイント。"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, Query

from ..deps import (
    get_actor,
    get_crud_service,
    require_read_auth,
    require_write_auth,
)
from ..schemas import MessageResponse, RecordResponse, TableDataResponse
from ...services.crud_service import CrudService

router = APIRouter(prefix="/api/tables", tags=["CRUD"])


@router.get("/{table_name}", response_model=TableDataResponse)
def list_records(
    table_name: str,
    limit: Optional[int] = Query(None, ge=1, description="返す最大行数(#17)"),
    offset: int = Query(0, ge=0, description="先頭からのスキップ行数(#17)"),
    service: CrudService = Depends(get_crud_service),
    _: None = Depends(require_read_auth),
):
    """テーブルのレコードを取得する。``limit``/``offset`` 省略時は全件。"""
    rows = service.list_records(table_name, limit=limit, offset=offset)
    columns = service.config.tables[table_name].column_names
    return TableDataResponse(
        table_name=table_name, columns=columns, data=rows, count=len(rows)
    )


@router.get("/{table_name}/{record_id}", response_model=RecordResponse)
def get_record(
    table_name: str,
    record_id: str,
    service: CrudService = Depends(get_crud_service),
    _: None = Depends(require_read_auth),
):
    """主キーを指定して 1 レコードを取得する。"""
    return RecordResponse(data=service.get_record(table_name, record_id))


@router.post("/{table_name}", response_model=RecordResponse, status_code=201)
def create_record(
    table_name: str,
    payload: Dict[str, Any] = Body(...),
    service: CrudService = Depends(get_crud_service),
    actor: str = Depends(get_actor),
    _: None = Depends(require_write_auth),
):
    """レコードを作成する。主キーは無視され、未知の列は捨てられる。"""
    return RecordResponse(data=service.create_record(table_name, payload, actor=actor))


@router.put("/{table_name}/{record_id}", response_model=RecordResponse)
def update_record(
    table_name: str,
    record_id: str,
    payload: Dict[str, Any] = Body(...),
    service: CrudService = Depends(get_crud_service),
    actor: str = Depends(get_actor),
    _: None = Depends(require_write_auth),
):
    """レコードを更新する。"""
    return RecordResponse(
        data=service.update_record(table_name, record_id, payload, actor=actor)
    )


@router.delete("/{table_name}/{record_id}", response_model=MessageResponse)
def delete_record(
    table_name: str,
    record_id: str,
    service: CrudService = Depends(get_crud_service),
    actor: str = Depends(get_actor),
    _: None = Depends(require_write_auth),
):
    """レコードを削除する。"""
    service.delete_record(table_name, record_id, actor=actor)
    return MessageResponse(message="Record deleted successfully")
