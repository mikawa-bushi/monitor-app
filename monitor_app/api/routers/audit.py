"""監査ログ閲覧エンドポイント(フェーズ4・G)。"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ..deps import get_audit_service, require_read_auth

router = APIRouter(prefix="/api/audit", tags=["Audit"])


@router.get("")
def list_audit(
    table: str | None = None,
    limit: int = 100,
    service=Depends(get_audit_service),
    _: None = Depends(require_read_auth),
):
    """直近の変更履歴を返す。``table`` で対象テーブルを絞れる。

    audit_enabled が False の場合は空配列を返す。
    """
    return {"entries": service.list_recent(table_name=table, limit=limit)}
