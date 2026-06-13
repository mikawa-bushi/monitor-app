"""ビュー(表示用ビュー)の実行。生 SQL を読み取り専用接続で実行する。"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from ..exceptions import QueryExecutionError, ViewNotFoundError
from ..settings.declarative import MonitorConfig
from ..db.engine import Database


class ViewService:
    def __init__(self, config: MonitorConfig, db: Database) -> None:
        self.config = config
        self.db = db

    def get_view(self, view_name: str) -> Dict[str, Any]:
        if view_name not in self.config.views:
            raise ViewNotFoundError(f"ビュー '{view_name}' は定義されていません")
        vdef = self.config.views[view_name]

        try:
            with self.db.readonly() as conn:
                result = conn.execute(text(vdef.query))
                columns = list(result.keys())
                rows: List[Dict[str, Any]] = [dict(r) for r in result.mappings()]
        except SQLAlchemyError as exc:
            raise QueryExecutionError(
                f"ビュー '{view_name}' のクエリ実行に失敗しました"
            ) from exc

        cell_styles = {
            col: style.model_dump(by_alias=True, exclude_none=True)
            for col, style in vdef.styles.items()
        }
        return {
            "view_name": view_name,
            "title": vdef.title or view_name,
            "description": vdef.description,
            "columns": columns,
            "column_labels": dict(vdef.labels),
            "data": rows,
            "cell_styles": cell_styles,
        }

    @staticmethod
    def data_hash(payload: Dict[str, Any]) -> str:
        """ビューデータの行内容からハッシュを作る(SSE の差分検出用)。"""
        blob = json.dumps(payload["data"], sort_keys=True, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()
