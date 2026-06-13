"""ソースごとの取り込みカーソルを永続化するストア。"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Column, MetaData, String, Table, delete, insert, select

from .engine import Database


class IngestStateStore:
    """ソースごとの取り込みカーソルを monitor-app 側 DB に永続化する。"""

    def __init__(self, db: Database) -> None:
        self._db = db
        self._metadata = MetaData()
        self._table = Table(
            "_ingest_state",
            self._metadata,
            Column("source", String, primary_key=True),
            Column("cursor", String, nullable=False),
            Column("updated_at", String, nullable=False),
        )

    def create(self) -> None:
        """_ingest_state テーブルを必要なら作成する(冪等)。"""
        self._metadata.create_all(self._db.engine)

    def get(self, source: str) -> str | None:
        """カーソルを返す。未登録なら None。"""
        with self._db.connect() as conn:
            row = conn.execute(
                select(self._table.c.cursor).where(self._table.c.source == source)
            ).fetchone()
        return row[0] if row is not None else None

    def set(self, source: str, cursor: str) -> None:
        """カーソルを保存する(UPSERT 相当: delete → insert)。"""
        updated_at = datetime.now(timezone.utc).isoformat()
        with self._db.connect() as conn:
            conn.execute(delete(self._table).where(self._table.c.source == source))
            conn.execute(
                insert(self._table).values(
                    source=source,
                    cursor=cursor,
                    updated_at=updated_at,
                )
            )
