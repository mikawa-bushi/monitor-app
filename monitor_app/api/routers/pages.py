"""HTML ページ配信(ホーム・ビュー表示)。Jinja2 テンプレートを使う。"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from ..deps import get_config
from ...exceptions import TableNotFoundError, ViewNotFoundError
from ...settings.declarative import MonitorConfig

router = APIRouter(tags=["Pages"], include_in_schema=False)


def _nav_groups(config: MonitorConfig) -> list[dict]:
    """ナビゲーション用グループリストを返す。

    config.views の出現順でグループ化する。group=="" のビューは、他にグループが
    存在する場合のみ「その他」として末尾に集約する。全ビューが group=="" の場合は
    group 名 "" の単一グループを返す。
    """
    named: dict[str, list[dict]] = {}  # group 名 → ビューリスト(出現順)
    ungrouped: list[dict] = []

    for vname, vdef in config.views.items():
        entry = {"name": vname, "title": vdef.title or vname}
        if vdef.group:
            if vdef.group not in named:
                named[vdef.group] = []
            named[vdef.group].append(entry)
        else:
            ungrouped.append(entry)

    # グループが一切ない場合は単一の無名グループを返す
    if not named:
        return [{"group": "", "views": ungrouped}]

    result = [{"group": g, "views": vs} for g, vs in named.items()]
    if ungrouped:
        result.append({"group": "その他", "views": ungrouped})
    return result


def _nav_groups_filtered(
    config: MonitorConfig, *, chart: bool | None = None
) -> list[dict]:
    """nav_groups と同形式で、chart の有無でビューを絞り込んで返す。

    chart=True  → chart 付きビューのみ
    chart=False → chart なしビューのみ
    chart=None  → 全ビュー(_nav_groups と同じ)
    """
    named: dict[str, list[dict]] = {}
    ungrouped: list[dict] = []

    for vname, vdef in config.views.items():
        if chart is True and vdef.chart is None:
            continue
        if chart is False and vdef.chart is not None:
            continue
        entry = {"name": vname, "title": vdef.title or vname}
        if vdef.group:
            if vdef.group not in named:
                named[vdef.group] = []
            named[vdef.group].append(entry)
        else:
            ungrouped.append(entry)

    if not named:
        return [{"group": "", "views": ungrouped}]

    result = [{"group": g, "views": vs} for g, vs in named.items()]
    if ungrouped:
        result.append({"group": "その他", "views": ungrouped})
    return result


def _ctx(config: MonitorConfig, **extra) -> dict:
    return {
        "app_title": config.app_title,
        "header_text": config.header_text,
        "footer_text": config.footer_text,
        "favicon_path": config.favicon_path,
        "nav_groups": _nav_groups(config),
        **extra,
    }


@router.get("/", response_class=HTMLResponse)
def index(request: Request, config: MonitorConfig = Depends(get_config)):
    """ビュー一覧を表示するホームページ。"""
    templates = request.app.state.templates

    # ダッシュボード用: dashboard.views 指定がある場合はその順、なければ chart 付き全ビュー
    if config.dashboard.views:
        dash_views = [
            {
                "name": vname,
                "title": config.views[vname].title or vname,
                "chart": config.views[vname].chart.model_dump(),
            }
            for vname in config.dashboard.views
        ]
    else:
        dash_views = [
            {
                "name": vname,
                "title": vdef.title or vname,
                "chart": vdef.chart.model_dump(),
            }
            for vname, vdef in config.views.items()
            if vdef.chart is not None
        ]
    dashboard_json = json.dumps(
        {"columns": config.dashboard.columns, "views": dash_views},
        ensure_ascii=False,
    )

    # chart を持たないビューのみ nav_groups と同形式で返す(リンク一覧用)
    list_views = _nav_groups_filtered(config, chart=False)

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
            dashboard_json=dashboard_json,
            list_views=list_views,
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
    kiosk_views = [
        {
            "name": name,
            "title": config.views[name].title or name,
            "chart": (
                config.views[name].chart.model_dump()
                if config.views[name].chart
                else None
            ),
        }
        for name in config.kiosk_views()
        if name in config.views
    ]
    return templates.TemplateResponse(
        request,
        "kiosk.html",
        _ctx(
            config,
            kiosk_json=json.dumps({"views": kiosk_views}, ensure_ascii=False),
            has_kiosk_charts=any(v["chart"] for v in kiosk_views),
            has_kpis=bool(config.kpis),
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
            current_view=view_name,
        ),
    )
