"""CRUD のビジネスロジック。列フィルタ・型変換・存在チェックを行う。"""

from __future__ import annotations

from typing import Any, ClassVar

from sqlalchemy.exc import DBAPIError

from ..db.bulk import ON_CONFLICT_MODES, OnConflict
from ..db.errors import CONFLICT, classify
from ..db.registry import TableRegistry
from ..db.repository import TableRepository
from ..exceptions import (
    ConflictError,
    InvalidPayloadError,
    RecordNotFoundError,
    TableNotFoundError,
)
from ..settings.declarative import MonitorConfig
from .audit_service import AuditService
from .coercion import CoercionError, coerce
from .view_cache import ViewCache


class CrudService:
    def __init__(
        self,
        config: MonitorConfig,
        registry: TableRegistry,
        repository: TableRepository,
        audit: AuditService | None = None,
        view_cache: ViewCache | None = None,
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

    def _coerce_pk(self, table_name: str, record_id: Any) -> Any:
        """URL 上の主キー(常に文字列)を主キー列の論理型へ変換する(#E8)。

        本文は ``_clean_payload`` で coerce されるのに主キーだけ未変換だと、
        SQLite 以外(PostgreSQL/MySQL)で ``integer = '5'(text)`` の型不一致に
        なり得る。型に合わない id は該当レコードが存在し得ないため、見つからな
        かったものとして扱う。
        """
        tdef = self.config.tables[table_name]
        try:
            return coerce(record_id, tdef.column_type(tdef.primary_key))
        except CoercionError:
            raise RecordNotFoundError(
                f"id={record_id} のレコードが見つかりません"
            ) from None

    def _clean_payload(self, table_name: str, data: dict[str, Any], *, drop_pk: bool):
        """設定の列だけ残し、論理型に変換する。未知の列は捨てる。"""
        tdef = self.config.tables[table_name]
        pk = tdef.primary_key
        allowed = set(tdef.column_names)
        cleaned: dict[str, Any] = {}
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

    def ingest_records(self, table_name: str, records: list[dict[str, Any]]) -> int:
        """センサ/PLC 等からの複数レコードを一括投入する(インジェスト用)。

        各行は設定の列だけに絞って型変換する。主キーは保持する(送信元が時刻や
        連番を持つ時系列データを想定するため)。主キーが既存の行と重なればバッチ
        全体を取り消す(:meth:`ingest_batch` の ``on_conflict="error"``)。
        """
        return self.ingest_batch(table_name, records)["inserted"]

    def ingest_batch(
        self,
        table_name: str,
        records: list[dict[str, Any]],
        *,
        on_conflict: OnConflict = "error",
    ) -> dict[str, int]:
        """:meth:`ingest_records` に主キー衝突時の扱いを加えたもの(CRIT-08)。

        - ``"error"`` … バッチ全体を取り消し、重複した主キーを示す ConflictError(409)。
          戻り値は ``{"inserted": n}``。
        - ``"ignore"`` … 既存の主キーの行を読み飛ばす。``{"inserted": n, "skipped": m}``
          (再送されたバッチの新しい行だけが入る)。
        - ``"update"`` … 既存の主キーの行を上書きする。``{"inserted": n, "updated": m}``
          (``updated`` は新規でなかった行の数)。

        型変換できない値はどのモードでもバッチ全体を 422 で拒否する(従来どおり)。
        """
        if on_conflict not in ON_CONFLICT_MODES:
            raise InvalidPayloadError(
                f"on_conflict は {', '.join(ON_CONFLICT_MODES)} のいずれかです"
            )
        self._require_table(table_name)
        cleaned = [
            self._clean_payload(table_name, rec, drop_pk=False) for rec in records
        ]
        try:
            inserted = self.repo.insert_many(
                table_name, cleaned, on_conflict=on_conflict
            )
        except DBAPIError as exc:
            if on_conflict == "error" and classify(exc) == CONFLICT:
                self._raise_duplicate_keys(table_name, cleaned, exc)
            raise
        self._invalidate_views()
        if on_conflict == "ignore":
            return {"inserted": inserted, "skipped": len(cleaned) - inserted}
        if on_conflict == "update":
            return {"inserted": inserted, "updated": len(cleaned) - inserted}
        return {"inserted": inserted}

    #: 409 のメッセージに並べる重複キーの最大数。
    _MAX_LISTED_KEYS = 10

    def _raise_duplicate_keys(
        self, table_name: str, rows: list[dict[str, Any]], exc: Exception
    ) -> None:
        """衝突が主キーの重複なら、重複したキーを示す ConflictError を送出する。

        主キーの重複でなければ(外部キー違反など)何もしない(呼び出し側が元の例外を
        送出し、DB エラーの 409 として返る)。
        """
        pk = self.config.tables[table_name].primary_key
        keys = [row.get(pk) for row in rows if row.get(pk) is not None]
        seen: set = set()
        dup_in_batch: set = set()
        for key in keys:
            if key in seen:
                dup_in_batch.add(key)
            seen.add(key)
        existing = self.repo.existing_keys(table_name, keys)
        # 重複したキーを、バッチ内で最初に現れた順に並べる
        dups = [k for k in dict.fromkeys(keys) if k in existing or k in dup_in_batch]
        if not dups:
            return
        listed = ", ".join(str(k) for k in dups[: self._MAX_LISTED_KEYS])
        if len(dups) > self._MAX_LISTED_KEYS:
            listed += f" ほか {len(dups) - self._MAX_LISTED_KEYS} 件"
        raise ConflictError(
            f"主キー {pk} が既存の行(または同じバッチ内の行)と重複しています: {listed}。"
            "バッチ全体を取り消しました"
            "(on_conflict=ignore で重複を読み飛ばし、on_conflict=update で上書きできます)"
        ) from exc

    def list_records(
        self, table_name: str, limit: int | None = None, offset: int = 0
    ) -> list[dict[str, Any]]:
        self._require_table(table_name)
        return self.repo.list(table_name, limit=limit, offset=offset)

    def get_record(self, table_name: str, record_id: Any) -> dict[str, Any]:
        self._require_table(table_name)
        record_id = self._coerce_pk(table_name, record_id)
        row = self.repo.get(table_name, record_id)
        if row is None:
            raise RecordNotFoundError(f"id={record_id} のレコードが見つかりません")
        return row

    def _audit(self, *args, **kwargs) -> None:
        if self.audit:
            self.audit.record(*args, **kwargs)

    def create_record(
        self, table_name: str, data: dict[str, Any], actor: str = "system"
    ) -> dict[str, Any]:
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
        data: dict[str, Any],
        actor: str = "system",
    ) -> dict[str, Any]:
        self._require_table(table_name)
        record_id = self._coerce_pk(table_name, record_id)
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
        record_id = self._coerce_pk(table_name, record_id)
        before = self.repo.get(table_name, record_id)
        if not self.repo.delete(table_name, record_id):
            raise RecordNotFoundError(f"id={record_id} のレコードが見つかりません")
        self._invalidate_views()
        self._audit(table_name, record_id, "delete", before=before, actor=actor)

    def schema(self) -> dict[str, Any]:
        """全テーブルのスキーマ情報を返す。"""
        out: dict[str, Any] = {}
        for name, tdef in self.config.tables.items():
            out[name] = {
                "columns": tdef.column_names,
                "primary_key": tdef.primary_key,
                "foreign_keys": tdef.foreign_keys,
            }
        return out

    # 論理型 → HTML input タイプ
    _INPUT_TYPES: ClassVar[dict[str, str]] = {
        "int": "number",
        "float": "number",
        "date": "date",
        "datetime": "datetime-local",
        "bool": "checkbox",
        "str": "text",
    }

    def form_schema(self, table_name: str) -> dict[str, Any]:
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

        fields: list[dict[str, Any]] = []
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
