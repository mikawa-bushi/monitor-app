"""画面で変えた設定(config.py との差分)を DB に保存するストア(内部テーブル ``_settings``)。

DB に置くので、SQLite の定期バックアップ・Docker のボリュームにそのまま含まれ、
PostgreSQL / MySQL でも同じように残る。表は最初に保存したときに作る(画面で何も
変えていない DB には作らない)。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Column, MetaData, String, Table, Text, delete, insert, select
from sqlalchemy import inspect as sa_inspect

from .engine import Database

#: 内部テーブルの名前(CSV のテーブル名にできない — settings/infer.py)。
SETTINGS_TABLE = "_settings"


@dataclass(frozen=True)
class StoredSettings:
    """保存した 1 件(値と、いつ・誰が保存したか)。"""

    value: dict[str, Any]
    #: 保存した時刻(UTC の ISO 8601)。
    updated_at: str
    #: 保存した人(監査ログと同じ ``ui@127.0.0.1`` の形)。
    updated_by: str


class SettingsStore:
    """名前 → JSON の値を ``_settings`` 表に保存する。"""

    def __init__(self, db: Database) -> None:
        self._db = db
        self._metadata = MetaData()
        # MySQL は VARCHAR に長さが必須(長さなしでは CREATE TABLE できない — CRIT-01)。
        self._table = Table(
            SETTINGS_TABLE,
            self._metadata,
            Column("name", String(255), primary_key=True),
            Column("value", Text, nullable=False),
            Column("updated_at", String(64), nullable=False),
            Column("updated_by", String(255), nullable=False),
        )

    def load(self, name: str) -> StoredSettings | None:
        """保存した値を返す(表か行が無ければ None)。値が JSON の辞書でなければ ValueError。"""
        if not sa_inspect(self._db.engine).has_table(SETTINGS_TABLE):
            return None
        stmt = select(
            self._table.c.value, self._table.c.updated_at, self._table.c.updated_by
        ).where(self._table.c.name == name)
        with self._db.readonly() as conn:
            row = conn.execute(stmt).fetchone()
        if row is None:
            return None
        value = json.loads(row[0])
        if not isinstance(value, dict):
            raise ValueError(f"{SETTINGS_TABLE} の '{name}' が辞書ではありません")
        return StoredSettings(value, row[1], row[2])

    def save(self, name: str, value: dict[str, Any], actor: str) -> StoredSettings:
        """値を保存する(置き換え)。表が無ければ作る。"""
        self._metadata.create_all(self._db.engine)
        stored = StoredSettings(
            value, datetime.now(UTC).isoformat(timespec="seconds"), actor
        )
        with self._db.connect() as conn:
            conn.execute(delete(self._table).where(self._table.c.name == name))
            conn.execute(
                insert(self._table).values(
                    name=name,
                    value=json.dumps(value, ensure_ascii=False),
                    updated_at=stored.updated_at,
                    updated_by=actor,
                )
            )
        return stored

    def delete(self, name: str) -> None:
        """保存した値を消す(無ければ何もしない)。"""
        if not sa_inspect(self._db.engine).has_table(SETTINGS_TABLE):
            return
        with self._db.connect() as conn:
            conn.execute(delete(self._table).where(self._table.c.name == name))
