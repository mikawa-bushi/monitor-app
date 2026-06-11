"""アラート閲覧エンドポイント(フェーズ1・A)。"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ..deps import get_alert_engine, require_read_auth

router = APIRouter(prefix="/api/alerts", tags=["Alerts"])


@router.get("")
def list_active_alerts(
    engine=Depends(get_alert_engine),
    _: None = Depends(require_read_auth),
):
    """現在アクティブな(発火中の)アラート一覧。"""
    return {"alerts": engine.active_alerts()}
