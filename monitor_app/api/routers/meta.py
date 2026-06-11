"""メタ情報エンドポイント(スキーマ一覧・ヘルスチェック)。"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ..deps import (
    get_crud_service,
    get_kpi_service,
    require_read_auth,
)
from ..schemas import SchemaResponse
from ...services.crud_service import CrudService

router = APIRouter(prefix="/api", tags=["Meta"])


@router.get("/schema", response_model=SchemaResponse)
def get_schema(
    service: CrudService = Depends(get_crud_service),
    _: None = Depends(require_read_auth),
):
    """全テーブルのスキーマ(列・主キー・外部キー)を返す。"""
    return SchemaResponse(tables=service.schema())


@router.get("/kpis")
def get_kpis(
    service=Depends(get_kpi_service),
    _: None = Depends(require_read_auth),
):
    """全 KPI カードを評価して返す(フェーズ2・E)。"""
    return {"kpis": service.evaluate_all()}


@router.get("/health")
def health():
    """死活監視用。"""
    return {"status": "ok"}
