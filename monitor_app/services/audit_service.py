"""変更履歴 / 監査ログ(フェーズ4・G)。

内部メタテーブル ``_audit`` に CRUD の書き込みを記録する。トレーサビリティ
(誰が・いつ・何を変えたか)のための機能で、ユーザー定義テーブルとは独立に管理する。
``audit_enabled`` が False のときは何もしない(no-op)。
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
from typing import Any, Dict, List, Optional

from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    desc,
    insert,
    select,
)

from ..db.engine import Database

logger = logging.getLogger("monitor_app.audit")


class AuditService:
    def __init__(self, db: Database, enabled: bool) -> None:
        self.db = db
        self.enabled = enabled
        self._metadata = MetaData()
        self.table = Table(
            "_audit",
            self._metadata,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("ts", DateTime),
            Column("table_name", String(255)),
            Column("record_id", String(255)),
            Column("action", String(16)),  # create / update / delete
            Column("before", Text),
            Column("after", Text),
            Column("actor", String(255)),
        )
        if enabled:
            self._metadata.create_all(db.engine)

    @staticmethod
    def _dump(value: Optional[Dict[str, Any]]) -> Optional[str]:
        return (
            None
            if value is None
            else json.dumps(value, default=str, ensure_ascii=False)
        )

    def record(
        self,
        table_name: str,
        record_id: Any,
        action: str,
        *,
        before: Optional[Dict[str, Any]] = None,
        after: Optional[Dict[str, Any]] = None,
        actor: str = "system",
    ) -> None:
        if not self.enabled:
            return
        try:
            with self.db.connect() as conn:
                conn.execute(
                    insert(self.table).values(
                        ts=_dt.datetime.now(),
                        table_name=table_name,
                        record_id=None if record_id is None else str(record_id),
                        action=action,
                        before=self._dump(before),
                        after=self._dump(after),
                        actor=actor,
                    )
                )
        except Exception:  # noqa: BLE001 - 監査の失敗で業務処理を止めない
            logger.exception("監査ログの記録に失敗しました")

    def list_recent(
        self, table_name: Optional[str] = None, limit: int = 100
    ) -> List[Dict[str, Any]]:
        if not self.enabled:
            return []
        stmt = select(self.table).order_by(desc(self.table.c.id)).limit(limit)
        if table_name:
            stmt = stmt.where(self.table.c.table_name == table_name)
        with self.db.readonly() as conn:
            rows = conn.execute(stmt).mappings().all()
        result = []
        for r in rows:
            d = dict(r)
            d["before"] = json.loads(d["before"]) if d["before"] else None
            d["after"] = json.loads(d["after"]) if d["after"] else None
            result.append(d)
        return result
