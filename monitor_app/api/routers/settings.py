"""画面での設定の編集(管理の機能)。

``GET`` で使う設定と入力欄の候補を返し、``POST /check`` で点検だけ、``PUT`` で点検して
保存し、``DELETE`` で config.py の設定に戻す。保存・リセットはサーバーを止めずに反映する
(:meth:`~monitor_app.container.Services.sync_config`)。開いている画面は、応答の
``X-Monitor-Config`` ヘッダ(SSE では ``reload`` イベント)で設定が変わったことを知り、
読み直す。画面が読み込んだ後にほかの人が保存していれば 409(``STALE_SETTINGS``)。

点検(SQL の空実行)と保存は DB を使うためスレッドで行い、反映はイベントループの上で
行う(アラートの評価を始めるときに asyncio のタスクを作るため)。
"""

from __future__ import annotations

import dataclasses
from functools import partial
from typing import Any

import anyio
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ...services.settings_service import EditResult, SettingsEditor
from ...settings.overrides import editable_settings
from ..deps import get_actor, get_settings_editor, require_admin

router = APIRouter(prefix="/api/settings", tags=["Settings"])


class SettingsBody(BaseModel):
    """画面の設定の内容。"""

    settings: dict[str, Any] = Field(
        description="画面で編集できる項目(GET の settings と同じ形)"
    )
    revision: str | None = Field(
        None,
        description="画面が読み込んだときの版(GET の revision)。今の版と違えば 409",
    )


def _result_body(result: EditResult) -> dict[str, Any]:
    return {
        "ok": result.config is not None,
        "issues": [dataclasses.asdict(issue) for issue in result.issues],
        "view_columns": result.view_columns,
    }


def _page_body(editor: SettingsEditor, result: EditResult | None = None) -> dict:
    """設定ページが読む内容と点検の結果(``result`` が無ければ今の設定を点検する)。"""
    if result is None:
        result = editor.check(editable_settings(editor.config))
    return {**editor.snapshot(), **_result_body(result)}


@router.get("")
async def get_ui_settings(
    editor: SettingsEditor = Depends(get_settings_editor),
    _: None = Depends(require_admin),
):
    """使う設定(config.py + 画面で変えた差分)と、入力欄の候補・今の設定の問題を返す。"""
    return await anyio.to_thread.run_sync(_page_body, editor)


@router.post("/check")
async def check_ui_settings(
    body: SettingsBody,
    editor: SettingsEditor = Depends(get_settings_editor),
    _: None = Depends(require_admin),
):
    """画面の内容を点検する(保存はしない)。``monitor-app check`` と同じ判定。"""
    result = await anyio.to_thread.run_sync(editor.check, body.settings)
    return _result_body(result)


@router.put(
    "",
    responses={
        409: {"description": "画面が読み込んだ後に、ほかの人が保存した"},
        422: {"description": "点検でエラーが見つかった(保存していない)"},
    },
)
async def save_ui_settings(
    request: Request,
    body: SettingsBody,
    editor: SettingsEditor = Depends(get_settings_editor),
    actor: str = Depends(get_actor),
    _: None = Depends(require_admin),
):
    """点検して、エラーが無ければ保存し、サーバーを止めずに反映する。

    保存するのは config.py との差分だけ。警告は保存を止めない(応答の issues に入る)。
    """
    result = await anyio.to_thread.run_sync(
        partial(editor.save, body.settings, actor, revision=body.revision)
    )
    if result.config is None:
        return JSONResponse(
            status_code=422,
            content={
                "detail": {
                    "code": "INVALID_SETTINGS",
                    "message": "設定にエラーがあるため保存していません",
                },
                **_result_body(result),
            },
        )
    request.app.state.services.sync_config()
    return await anyio.to_thread.run_sync(_page_body, editor, result)


@router.delete("", responses={409: {"description": "ほかの人が先に保存した"}})
async def reset_ui_settings(
    request: Request,
    revision: str | None = Query(None, description="画面が読み込んだときの版"),
    editor: SettingsEditor = Depends(get_settings_editor),
    actor: str = Depends(get_actor),
    _: None = Depends(require_admin),
):
    """画面で変えた設定をすべて消し、config.py の設定に戻す(サーバーは止めない)。"""
    await anyio.to_thread.run_sync(partial(editor.reset, actor, revision=revision))
    request.app.state.services.sync_config()
    return await anyio.to_thread.run_sync(_page_body, editor)
