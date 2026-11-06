"""HTML ページ配信(ホーム・ビュー表示)。Jinja2 テンプレートを使う。"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from ...exceptions import TableNotFoundError, ViewNotFoundError
from ...settings.declarative import MonitorConfig
from ...web import static_asset_path
from ..deps import (
    RedirectToLogin,
    admin_access_problem,
    get_config,
    page_auth_guard,
)
from .auth import safe_next

router = APIRouter(tags=["Pages"], include_in_schema=False)


def _escape_json_for_script(raw: str) -> str:
    """``<script type="application/json">`` 内へ安全に埋め込める JSON 文字列を返す(#S1)。

    ``json.dumps(ensure_ascii=False)`` / Pydantic の ``model_dump_json`` は ``<`` ``>``
    ``&`` を素通しするため、ビュータイトル等に ``</script>`` が含まれると script タグを
    閉じて DOM 破壊・XSS になり得る。HTML で意味を持つ文字と行区切り(JS では文字列を
    壊す U+2028/U+2029)を ``\\uXXXX`` へ置換する。``JSON.parse`` が元の文字へ復号する
    ため、埋め込まれるデータの意味は変わらない。
    """
    return (
        raw.replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _nav_groups(config: MonitorConfig, *, chart: bool | None = None) -> list[dict]:
    """ナビゲーション用グループリストを返す。

    config.views の出現順でグループ化する。group=="" のビューは、他にグループが
    存在する場合のみ「その他」として末尾に集約する。全ビューが group=="" の場合は
    group 名 "" の単一グループを返す。

    ``chart`` で chart の有無によりビューを絞り込める:

    chart=True  → chart 付きビューのみ
    chart=False → chart なしビューのみ
    chart=None  → 全ビュー
    """
    named: dict[str, list[dict]] = {}  # group 名 → ビューリスト(出現順)
    ungrouped: list[dict] = []

    for vname, vdef in config.views.items():
        if chart is True and vdef.chart is None:
            continue
        if chart is False and vdef.chart is not None:
            continue
        entry = {"name": vname, "title": vdef.title or vname}
        if vdef.group:
            named.setdefault(vdef.group, []).append(entry)
        else:
            ungrouped.append(entry)

    # グループが一切ない場合は単一の無名グループを返す
    if not named:
        return [{"group": "", "views": ungrouped}]

    result = [{"group": g, "views": vs} for g, vs in named.items()]
    if ungrouped:
        result.append({"group": "その他", "views": ungrouped})
    return result


#: 後方互換の別名(``_nav_groups`` が ``chart`` 引数での絞り込みを兼ねる)。
_nav_groups_filtered = _nav_groups


def _favicon(config: MonitorConfig) -> str:
    """表示する favicon の static パス。

    v1 形式の設定の ``FAVICON_PATH='favicon.ico'``(v1 の既定値)は v2 の配置では
    見つからないため、同梱の ``img/favicon.ico`` を使う(設定値そのものは変えない)。
    """
    return static_asset_path(config.favicon_path)


def _ctx(config: MonitorConfig, **extra) -> dict:
    return {
        "app_title": config.app_title,
        "header_text": config.header_text,
        "footer_text": config.footer_text,
        "favicon_path": _favicon(config),
        "nav_groups": _nav_groups(config),
        **extra,
    }


@router.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    config: MonitorConfig = Depends(get_config),
    _: None = Depends(page_auth_guard),
):
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
    dashboard_json = _escape_json_for_script(
        json.dumps(
            {"columns": config.dashboard.columns, "views": dash_views},
            ensure_ascii=False,
        )
    )

    # chart を持たないビューのみ nav_groups と同形式で返す(リンク一覧用)
    list_views = _nav_groups(config, chart=False)

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
                {"name": name, "heading": t.form.heading(name)}
                for name, t in config.tables.items()
                if t.form is not None
            ],
            dashboard_json=dashboard_json,
            list_views=list_views,
        ),
    )


@router.get("/form/{table_name}", response_class=HTMLResponse)
def show_form(
    request: Request,
    table_name: str,
    config: MonitorConfig = Depends(get_config),
    _: None = Depends(page_auth_guard),
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
def kiosk(
    request: Request,
    config: MonitorConfig = Depends(get_config),
    _: None = Depends(page_auth_guard),
):
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
            kiosk_json=_escape_json_for_script(
                json.dumps({"views": kiosk_views}, ensure_ascii=False)
            ),
            has_kiosk_charts=any(v["chart"] for v in kiosk_views),
            has_kpis=bool(config.kpis),
            rotate_seconds=kiosk_cfg.rotate_seconds if kiosk_cfg else 15,
            theme=kiosk_cfg.theme if kiosk_cfg else "dark",
            refresh_interval_ms=config.refresh_interval_ms,
        ),
    )


@router.get("/login", response_class=HTMLResponse)
def login_page(
    request: Request,
    error: int = 0,
    next: str = "/",
    config: MonitorConfig = Depends(get_config),
):
    """ログインページ(#11)。protect_reads 有効時・管理の画面で未認証だと誘導される。"""
    templates = request.app.state.templates
    target = safe_next(next)
    settings = request.app.state.settings
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "app_title": config.app_title,
            "header_text": config.header_text,
            "favicon_path": _favicon(config),
            "error": bool(error),
            "next": target,
            # 管理の画面から来たときは、管理者の鍵でログインすることを示す
            "admin_login": target in _ADMIN_PAGES and bool(settings.admin_key),
            "has_admin_key": bool(settings.admin_key),
        },
    )


#: 管理の画面(設定の編集・CSV のアップロード)のパス。
_ADMIN_PAGES = ("/settings", "/upload")


def _admin_page_problem(request: Request, path: str) -> str | None:
    """管理の画面を開けない理由(開けるなら None)。ログインが要れば /login へ送る。

    ページを開くだけ(GET)なので、ほかのサイトのリンクから開いてもよい(保存などの要求は
    API の require_admin が CSRF を確かめる)。
    """
    settings = request.app.state.settings
    problem = admin_access_problem(settings, request, request.headers.get("x-api-key"))
    if problem is None:
        return None
    if problem.status_code == 401:
        raise RedirectToLogin(path)
    return problem.detail["message"]


@router.get("/settings", response_class=HTMLResponse)
def settings_page(
    request: Request,
    config: MonitorConfig = Depends(get_config),
    _: None = Depends(page_auth_guard),
):
    """設定ページ(管理の機能)。表示・ビュー・KPI・アラートなどを画面で編集する。

    設定の内容は settings.js が /api/settings から読み、保存する。
    """
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "settings.html",
        _ctx(
            config,
            current_page="settings",
            admin_disabled=_admin_page_problem(request, "/settings"),
        ),
    )


@router.get("/upload", response_class=HTMLResponse)
def upload_page(
    request: Request,
    config: MonitorConfig = Depends(get_config),
    _: None = Depends(page_auth_guard),
):
    """CSV のアップロード(管理の機能)。送信は upload.js が /api/upload/{table} へ行う。"""
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "upload.html",
        _ctx(
            config,
            current_page="upload",
            admin_disabled=_admin_page_problem(request, "/upload"),
            tables=[
                {"name": name, "columns": tdef.column_names}
                for name, tdef in config.tables.items()
            ],
        ),
    )


@router.get("/table/{view_name}", response_class=HTMLResponse)
def show_table(
    request: Request,
    view_name: str,
    config: MonitorConfig = Depends(get_config),
    _: None = Depends(page_auth_guard),
):
    """1 つのビューを表示するページ。データは JS が API から取得する。"""
    if view_name not in config.views:
        raise ViewNotFoundError(f"ビュー '{view_name}' は定義されていません")
    templates = request.app.state.templates
    vdef = config.views[view_name]
    chart_json = (
        _escape_json_for_script(vdef.chart.model_dump_json()) if vdef.chart else "null"
    )
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
