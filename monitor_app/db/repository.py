"""テーブルに対する CRUD。クエリはすべて SQLAlchemy Core の式ビルダで構築する。

文字列連結による SQL を一切作らないため、列名・テーブル名は :class:`TableRegistry`
に登録済みのオブジェクト経由でしか参照できない。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy import delete, func, insert, select, update

from .engine import Database
from .registry import TableRegistry


class TableRepository:
    def __init__(self, db: Database, registry: TableRegistry) -> None:
        self.db = db
        self.registry = registry

    def list(
        self,
        table_name: str,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        table = self.registry.get(table_name)
        stmt = select(table)
        if limit is not None:
            stmt = stmt.limit(limit).offset(max(offset, 0))
        with self.db.readonly() as conn:
            result = conn.execute(stmt)
            return [dict(row) for row in result.mappings()]

    def get(self, table_name: str, record_id: Any) -> Optional[Dict[str, Any]]:
        table = self.registry.get(table_name)
        pk = table.c[self.registry.primary_key(table_name)]
        with self.db.readonly() as conn:
            result = conn.execute(select(table).where(pk == record_id))
            row = result.mappings().first()
            return dict(row) if row else None

    def insert_many(self, table_name: str, rows: List[Dict[str, Any]]) -> int:
        """複数レコードを一括 INSERT し、挿入件数を返す(インジェスト用)。"""
        if not rows:
            return 0
        table = self.registry.get(table_name)
        with self.db.connect() as conn:
            conn.execute(insert(table), rows)
        return len(rows)

    def insert(self, table_name: str, values: Dict[str, Any]) -> Dict[str, Any]:
        table = self.registry.get(table_name)
        pk_name = self.registry.primary_key(table_name)
        with self.db.connect() as conn:
            result = conn.execute(insert(table).values(**values))
            new_id = values.get(pk_name)
            if new_id is None and result.inserted_primary_key:
                new_id = result.inserted_primary_key[0]
            row = (
                conn.execute(select(table).where(table.c[pk_name] == new_id))
                .mappings()
                .first()
            )
            return dict(row) if row else {pk_name: new_id, **values}

    def update(
        self, table_name: str, record_id: Any, values: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        table = self.registry.get(table_name)
        pk = table.c[self.registry.primary_key(table_name)]
        with self.db.connect() as conn:
            result = conn.execute(update(table).where(pk == record_id).values(**values))
            if result.rowcount == 0:
                return None
            row = conn.execute(select(table).where(pk == record_id)).mappings().first()
            return dict(row) if row else None

    def delete(self, table_name: str, record_id: Any) -> bool:
        table = self.registry.get(table_name)
        pk = table.c[self.registry.primary_key(table_name)]
        with self.db.connect() as conn:
            result = conn.execute(delete(table).where(pk == record_id))
            return result.rowcount > 0

    def exists(self, table_name: str, record_id: Any) -> bool:
        table = self.registry.get(table_name)
        pk = table.c[self.registry.primary_key(table_name)]
        with self.db.readonly() as conn:
            count = conn.execute(
                select(func.count()).select_from(table).where(pk == record_id)
            ).scalar()
            return bool(count)
