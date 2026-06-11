"""API レスポンス用 Pydantic スキーマ。

レコード本体はテーブルが動的なので ``dict[str, Any]`` だが、エンベロープと
エラー形は型で固定し、OpenAPI に反映させる。
"""

from __future__ import annotations

from typing import Any, Dict, List

from pydantic import BaseModel


class TableDataResponse(BaseModel):
    table_name: str
    columns: List[str]
    data: List[Dict[str, Any]]
    count: int


class RecordResponse(BaseModel):
    success: bool = True
    data: Dict[str, Any]


class MessageResponse(BaseModel):
    success: bool = True
    message: str


class ViewDataResponse(BaseModel):
    view_name: str
    title: str
    description: str
    columns: List[str]
    data: List[Dict[str, Any]]
    cell_styles: Dict[str, Dict[str, Any]]
    alerts: List[Dict[str, Any]] = []  # 現在アクティブなアラート(フェーズ1・A)


class SchemaResponse(BaseModel):
    success: bool = True
    tables: Dict[str, Any]


class ErrorBody(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    detail: ErrorBody
