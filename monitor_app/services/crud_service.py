"""CRUD のビジネスロジック。列フィルタ・型変換・存在チェックを行う。"""

from __future__ import annotations

from typing import Any, Dict, List

from typing import Optional

from ..exceptions import InvalidPayloadError, RecordNotFoundError, TableNotFoundError
from ..settings.declarative import MonitorConfig
from ..db.registry import TableRegistry
from ..db.repository import TableRepository
from .audit_service import AuditService
from .coercion import CoercionError, coerce
from .view_cache import ViewCache


class CrudService:
    def __init__(
        self,
        config: MonitorConfig,
        registry: TableRegistry,
        repository: TableRepository,
        audit: Optional[AuditService] = None,
        view_cache: Optional[ViewCache] = None,
    ) -> None:
        self.config = config
        self.registry = registry
        self.repo = repository
        self.audit = audit
        self.view_cache = view_cache

    def _invalidate_views(self) -> None:
        """書き込み後にビュー共有キャッシュを無効化し、鮮度を保つ(#18)。"""
        if self.view_cache is not None:
            self.view_cache.clear()

    def _require_table(self, table_name: str) -> None:
        if not self.registry.has(table_name):
            raise TableNotFoundError(f"テーブル '{table_name}' は定義されていません")

    def _clean_payload(self, table_name: str, data: Dict[str, Any], *, drop_pk: bool):
        """設定の列だけ残し、論理型に変換する。未知の列は捨てる。"""
        tdef = self.config.tables[table_name]
        pk = tdef.primary_key
        allowed = set(tdef.column_names)
        cleaned: Dict[str, Any] = {}
        for key, value in data.items():
            if key not in allowed:
                continue
            if drop_pk and key == pk:
                continue
            try:
                cleaned[key] = coerce(value, tdef.column_type(key))
            except CoercionError as exc:
                raise InvalidPayloadError(str(exc)) from exc
        if not cleaned:
            raise InvalidPayloadError("有効な列が 1 つもありません")
        return cleaned

    def ingest_records(self, table_name: str, records: List[Dict[str, Any]]) -> int:
        """センサ/PLC 等からの複数レコードを一括投入する(インジェスト用)。

        各行は設定の列だけに絞って型変換する。主キーは保持する(送信元が時刻や
        連番を持つ時系列データを想定するため)。
        """
        self._require_table(table_name)
        cleaned = [
            self._clean_payload(table_name, rec, drop_pk=False) for rec in records
        ]
        inserted = self.repo.insert_many(table_name, cleaned)
        self._invalidate_views()
        return inserted

    def list_records(
        self, table_name: str, limit: Optional[int] = None, offset: int = 0
    ) -> List[Dict[str, Any]]:
        self._require_table(table_name)
        return self.repo.list(table_name, limit=limit, offset=offset)

    def get_record(self, table_name: str, record_id: Any) -> Dict[str, Any]:
        self._require_table(table_name)
        row = self.repo.get(table_name, record_id)
        if row is None:
            raise RecordNotFoundError(f"id={record_id} のレコードが見つかりません")
        return row

    def _audit(self, *args, **kwargs) -> None:
        if self.audit:
            self.audit.record(*args, **kwargs)

    def create_record(
        self, table_name: str, data: Dict[str, Any], actor: str = "system"
    ) -> Dict[str, Any]:
        self._require_table(table_name)
        values = self._clean_payload(table_name, data, drop_pk=True)
        row = self.repo.insert(table_name, values)
        self._invalidate_views()
        pk = self.config.tables[table_name].primary_key
        self._audit(table_name, row.get(pk), "create", after=row, actor=actor)
        return row

    def update_record(
        self,
        table_name: str,
        record_id: Any,
        data: Dict[str, Any],
        actor: str = "system",
    ) -> Dict[str, Any]:
        self._require_table(table_name)
        values = self._clean_payload(table_name, data, drop_pk=True)
        before = self.repo.get(table_name, record_id)
        row = self.repo.update(table_name, record_id, values)
        if row is None:
            raise RecordNotFoundError(f"id={record_id} のレコードが見つかりません")
        self._invalidate_views()
        self._audit(
            table_name, record_id, "update", before=before, after=row, actor=actor
        )
        return row

    def delete_record(
        self, table_name: str, record_id: Any, actor: str = "system"
    ) -> None:
        self._require_table(table_name)
        before = self.repo.get(table_name, record_id)
        if not self.repo.delete(table_name, record_id):
            raise RecordNotFoundError(f"id={record_id} のレコードが見つかりません")
        self._invalidate_views()
        self._audit(table_name, record_id, "delete", before=before, actor=actor)

    def schema(self) -> Dict[str, Any]:
        """全テーブルのスキーマ情報を返す。"""
        out: Dict[str, Any] = {}
        for name, tdef in self.config.tables.items():
            out[name] = {
                "columns": tdef.column_names,
                "primary_key": tdef.primary_key,
                "foreign_keys": tdef.foreign_keys,
            }
        return out

    # 論理型 → HTML input タイプ
    _INPUT_TYPES = {
        "int": "number",
        "float": "number",
        "date": "date",
        "bool": "checkbox",
        "str": "text",
    }

    def form_schema(self, table_name: str) -> Dict[str, Any]:
        """作業者入力フォーム(フェーズ3・F)のフィールド定義を返す。

        対象列・表示名・入力タイプ・選択肢を解決する。フォーム未定義のテーブルも
        主キー以外の全列を入力対象として既定生成する。
        """
        self._require_table(table_name)
        tdef = self.config.tables[table_name]
        form = tdef.form
        fields_src = (
            form.fields
            if form and form.fields is not None
            else [c for c in tdef.column_names if c != tdef.primary_key]
        )
        labels = form.labels if form else {}
        choices = form.choices if form else {}

        fields: List[Dict[str, Any]] = []
        for col in fields_src:
            logical = tdef.column_type(col)
            fields.append(
                {
                    "name": col,
                    "label": labels.get(col, col),
                    "input": self._INPUT_TYPES.get(logical, "text"),
                    "step": "any" if logical == "float" else None,
                    "choices": choices.get(col),
                }
            )
        return {
            "table": table_name,
            "submit_label": form.submit_label if form else "登録",
            "fields": fields,
        }
