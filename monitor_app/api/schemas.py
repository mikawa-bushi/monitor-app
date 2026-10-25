"""API レスポンス用 Pydantic スキーマ。

レコード本体はテーブルが動的なので ``dict[str, Any]`` だが、エンベロープと
エラー形は型で固定し、OpenAPI に反映させる。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class TableDataResponse(BaseModel):
    table_name: str
    columns: list[str]
    data: list[dict[str, Any]]
    count: int


class RecordResponse(BaseModel):
    success: bool = True
    data: dict[str, Any]


class MessageResponse(BaseModel):
    success: bool = True
    message: str


class ViewDataResponse(BaseModel):
    view_name: str
    title: str
    description: str
    columns: list[str]
    column_labels: dict[str, str] = {}  # 列名 → 表示見出し(単位込み)。フロントが採用
    data: list[dict[str, Any]]
    cell_styles: dict[str, dict[str, Any]]
    alerts: list[dict[str, Any]] = []  # 現在アクティブなアラート(フェーズ1・A)


class SchemaResponse(BaseModel):
    success: bool = True
    tables: dict[str, Any]


class ErrorBody(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    detail: ErrorBody
