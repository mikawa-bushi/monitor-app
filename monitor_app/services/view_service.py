"""ビュー(表示用ビュー)の実行。生 SQL を読み取り専用接続で実行する。

結果は :class:`ViewCache`(任意)で TTL 共有し(#18)、SSE・ダッシュボード・
アラート評価による同一ビューの多重クエリを 1 回にまとめる。``limit``/``offset``
で行数を絞れる(#17)。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from ..exceptions import QueryExecutionError, ViewNotFoundError
from ..settings.declarative import MonitorConfig
from ..db.engine import Database
from .view_cache import ViewCache


class ViewService:
    def __init__(
        self,
        config: MonitorConfig,
        db: Database,
        cache: Optional[ViewCache] = None,
    ) -> None:
        self.config = config
        self.db = db
        self.cache = cache

    def _compute(
        self, view_name: str, limit: Optional[int], offset: int
    ) -> Tuple[Dict[str, Any], str]:
        """クエリを実行し、(payload, hash) を返す。キャッシュの計算関数。"""
        vdef = self.config.views[view_name]

        sql = vdef.query
        params: Dict[str, Any] = {}
        if limit is not None:
            # 任意クエリを副問い合わせで包んで行数を絞る(#17)。
            sql = f"SELECT * FROM ({vdef.query}) AS _sub LIMIT :_limit OFFSET :_offset"
            params = {"_limit": limit, "_offset": max(offset, 0)}

        try:
            with self.db.readonly() as conn:
                result = conn.execute(text(sql), params)
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
        payload = {
            "view_name": view_name,
            "title": vdef.title or view_name,
            "description": vdef.description,
            "columns": columns,
            "column_labels": dict(vdef.labels),
            "data": rows,
            "cell_styles": cell_styles,
        }
        blob = json.dumps(rows, sort_keys=True, default=str)
        digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()
        return payload, digest

    def _base(
        self, view_name: str, limit: Optional[int], offset: int
    ) -> Tuple[Dict[str, Any], str]:
        if view_name not in self.config.views:
            raise ViewNotFoundError(f"ビュー '{view_name}' は定義されていません")
        key = (view_name, limit, offset)
        if self.cache is not None:
            return self.cache.get_or_compute(
                key, lambda: self._compute(view_name, limit, offset)
            )
        return self._compute(view_name, limit, offset)

    def get_view(
        self, view_name: str, limit: Optional[int] = None, offset: int = 0
    ) -> Dict[str, Any]:
        """ビューの payload を返す。呼び出し側が ``alerts`` を足せるよう浅いコピー
        を返す(キャッシュ済みオブジェクトを破壊しない)。"""
        base, _digest = self._base(view_name, limit, offset)
        return dict(base)

    def get_view_hashed(
        self, view_name: str, limit: Optional[int] = None, offset: int = 0
    ) -> Tuple[Dict[str, Any], str]:
        """(payload のコピー, 行データのハッシュ) を返す。SSE の差分検出用。
        ハッシュはキャッシュ時に 1 度だけ計算されるため、ポーリングのたびに
        再計算しない(#18)。"""
        base, digest = self._base(view_name, limit, offset)
        return dict(base), digest

    @staticmethod
    def data_hash(payload: Dict[str, Any]) -> str:
        """ビューデータの行内容からハッシュを作る(後方互換用)。"""
        blob = json.dumps(payload["data"], sort_keys=True, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()
