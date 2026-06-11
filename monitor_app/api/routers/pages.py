"""HTML ページ配信(ホーム・ビュー表示)。Jinja2 テンプレートを使う。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from ..deps import get_config
from ...exceptions import TableNotFoundError, ViewNotFoundError
from ...settings.declarative import MonitorConfig

router = APIRouter(tags=["Pages"], include_in_schema=False)


def _ctx(config: MonitorConfig, **extra) -> dict:
    return {
        "app_title": config.app_title,
        "header_text": config.header_text,
        "footer_text": config.footer_text,
        "favicon_path": config.favicon_path,
        **extra,
    }


@router.get("/", response_class=HTMLResponse)
def index(request: Request, config: MonitorConfig = Depends(get_config)):
    """ビュー一覧を表示するホームページ。"""
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "index.html",
        _ctx(
            config,
            views=list(config.views.keys()),
            has_kiosk=config.kiosk is not None,
            has_kpis=bool(config.kpis),
            refresh_interval_ms=config.refresh_interval_ms,
            form_tables=[
                name for name, t in config.tables.items() if t.form is not None
            ],
        ),
    )


@router.get("/form/{table_name}", response_class=HTMLResponse)
def show_form(
    request: Request, table_name: str, config: MonitorConfig = Depends(get_config)
):
    """作業者向け入力フォーム(フェーズ3・F)。

    テーブルスキーマからタッチ向けフォームを自動生成する。送信は既存の
    CRUD API(POST /api/tables/{table})を使う。
    """
    if table_name not in config.tables:
        raise TableNotFoundError(f"テーブル '{table_name}' は定義されていません")
    service = request.app.state.crud_service
    form = service.form_schema(table_name)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "form.html",
        _ctx(config, table_name=table_name, form=form),
    )


@router.get("/kiosk", response_class=HTMLResponse)
def kiosk(request: Request, config: MonitorConfig = Depends(get_config)):
    """Andon / 大型表示モード(フェーズ1・B)。

    複数ビューを一定間隔で自動ローテーションし、アラートを大きく表示する。
    ラインの大型モニタに常時表示することを想定する。
    """
    templates = request.app.state.templates
    kiosk_cfg = config.kiosk
    return templates.TemplateResponse(
        request,
        "kiosk.html",
        _ctx(
            config,
            kiosk_views=config.kiosk_views(),
            rotate_seconds=kiosk_cfg.rotate_seconds if kiosk_cfg else 15,
            theme=kiosk_cfg.theme if kiosk_cfg else "dark",
            refresh_interval_ms=config.refresh_interval_ms,
        ),
    )


@router.get("/table/{view_name}", response_class=HTMLResponse)
def show_table(
    request: Request, view_name: str, config: MonitorConfig = Depends(get_config)
):
    """1 つのビューを表示するページ。データは JS が API から取得する。"""
    if view_name not in config.views:
        raise ViewNotFoundError(f"ビュー '{view_name}' は定義されていません")
    templates = request.app.state.templates
    vdef = config.views[view_name]
    chart_json = vdef.chart.model_dump_json() if vdef.chart else "null"
    return templates.TemplateResponse(
        request,
        "table.html",
        _ctx(
            config,
            view_name=view_name,
            view_title=vdef.title or view_name,
            view_description=vdef.description,
            refresh_interval_ms=config.refresh_interval_ms,
            refresh_mode=config.refresh_mode,
            has_chart=vdef.chart is not None,
            chart_json=chart_json,
        ),
    )
