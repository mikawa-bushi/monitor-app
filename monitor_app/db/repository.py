"""テーブルに対する CRUD。クエリはすべて SQLAlchemy Core の式ビルダで構築する。

文字列連結による SQL を一切作らないため、列名・テーブル名は :class:`TableRegistry`
に登録済みのオブジェクト経由でしか参照できない。
"""

from __future__ import annotations

import builtins
import logging
from collections.abc import Iterable
from typing import Any

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError

from ..exceptions import SchemaMismatchError
from .bulk import OnConflict, existing_keys, insert_rows, repair_sequence
from .engine import Database
from .gate import fetch_all
from .registry import TableRegistry

logger = logging.getLogger("monitor_app.db")


class TableRepository:
    def __init__(self, db: Database, registry: TableRegistry) -> None:
        self.db = db
        self.registry = registry

    def list(
        self,
        table_name: str,
        limit: int | None = None,
        offset: int = 0,
    ) -> builtins.list[dict[str, Any]]:
        table = self.registry.get(table_name)
        stmt = select(table)
        if limit is not None:
            # ページ分けは主キー順で区切る。順序が無いと PostgreSQL では更新した行が
            # 物理的に後ろへ移り、ページをまたいで行が抜けたり重複したりする(D3-15)。
            pk = table.c[self.registry.primary_key(table_name)]
            stmt = stmt.order_by(pk).limit(limit).offset(max(offset, 0))
        with self.db.readonly() as conn:
            result = conn.execute(stmt)
            # mappings() より約 2 倍速い(PERF-10)。出力は同じ。
            keys = list(result.keys())
            # SQLite では行の取り出しをゲートで順番に行い、大きな表の同時取得による
            # GIL コンボイで軽い読み取りまで遅くなるのを防ぐ(PERF-01)。
            rows = fetch_all(result, self.db)
        return [dict(zip(keys, row)) for row in rows]

    def get(self, table_name: str, record_id: Any) -> dict[str, Any] | None:
        table = self.registry.get(table_name)
        pk = table.c[self.registry.primary_key(table_name)]
        with self.db.readonly() as conn:
            result = conn.execute(select(table).where(pk == record_id))
            row = result.mappings().first()
            return dict(row) if row else None

    def insert_many(
        self,
        table_name: str,
        rows: builtins.list[dict[str, Any]],
        *,
        on_conflict: OnConflict = "error",
    ) -> int:
        """複数レコードを 1 トランザクションで一括 INSERT し、新しく入った行数を返す
        (インジェスト用)。

        ``on_conflict`` は主キーが既存の行と重なったときの扱い
        (:func:`~monitor_app.db.bulk.insert_rows` を参照)。主キーを明示した行を
        入れたら PostgreSQL の連番も追従する(次の画面からの追加が衝突しない)。
        """
        if not rows:
            return 0
        table = self.registry.get(table_name)
        pk = self.registry.primary_key(table_name)
        with self.db.connect() as conn:
            return insert_rows(conn, table, rows, on_conflict=on_conflict, pk=pk)

    def existing_keys(self, table_name: str, keys: Iterable[Any]) -> set:
        """``keys`` のうち、テーブルに既にある主キーの集合。"""
        table = self.registry.get(table_name)
        pk = self.registry.primary_key(table_name)
        with self.db.readonly() as conn:
            return existing_keys(conn, table, pk, keys)

    def insert(self, table_name: str, values: dict[str, Any]) -> dict[str, Any]:
        try:
            return self._insert(table_name, values)
        except IntegrityError:
            # PostgreSQL で、ほかのプロセスが id を指定して入れた後などに連番が遅れて
            # いると、採番した id が既存の行と重なる。連番を最大値へ進めて 1 回だけ
            # やり直す(遅れていなければ元のエラーのまま — D5-01)。
            if not self._repair_sequence_for(table_name, values):
                raise
        return self._insert(table_name, values)

    def _repair_sequence_for(self, table_name: str, values: dict[str, Any]) -> bool:
        """採番の追加が失敗したとき、遅れた連番を直したか(PostgreSQL のみ)。"""
        pk_name = self.registry.primary_key(table_name)
        if self.db.dialect != "postgresql" or values.get(pk_name) is not None:
            return False
        table = self.registry.get(table_name)
        try:
            with self.db.connect() as conn:
                lag = repair_sequence(conn, table, pk_name)
        except Exception:  # 直せなければ元のエラーを返す
            logger.warning(
                "テーブル '%s' の連番を直せませんでした", table_name, exc_info=True
            )
            return False
        if lag is None:
            return False
        logger.warning(
            "テーブル '%s' の連番(%s)が表の最大値より遅れていたため、%d → %d に"
            "進めて追加をやり直します",
            table_name,
            pk_name,
            lag[0],
            lag[1],
        )
        return True

    def _insert(self, table_name: str, values: dict[str, Any]) -> dict[str, Any]:
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
            if row is None:
                # 追加した行を主キーで読み戻せない = 主キーの無い古い表(v0.x の
                # pandas 製)など。存在しない id を返さず、取り消して知らせる(CRIT-05)。
                raise SchemaMismatchError(
                    f"テーブル '{table_name}' に追加した行を主キー '{pk_name}' で"
                    "読み戻せませんでした(主キーの無い古い表の可能性があります)。"
                    "monitor-app check で表定義を確認してください"
                )
            return dict(row)

    def update(
        self, table_name: str, record_id: Any, values: dict[str, Any]
    ) -> dict[str, Any] | None:
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
