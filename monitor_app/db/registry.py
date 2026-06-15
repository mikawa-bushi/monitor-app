"""宣言的設定から SQLAlchemy Core の ``Table`` オブジェクトを構築・保持する。

ここで生成した ``Table`` / ``Column`` オブジェクトだけがクエリ構築に使われる。
テーブル名・列名を文字列として SQL に埋め込まないことで、インジェクションを
「コード規約」ではなく「型構造」で構造的に防ぐ。
"""

from __future__ import annotations

from typing import Dict

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
)

from ..settings.declarative import ColumnType, MonitorConfig, TableDef
from .engine import Database

_SQL_TYPES = {
    "int": Integer,
    "float": Float,
    "str": lambda: String(255),
    "bool": Boolean,
    "date": Date,
}


def _sql_type(logical: ColumnType):
    factory = _SQL_TYPES[logical]
    return factory()


def _build_table(name: str, tdef: TableDef, metadata: MetaData) -> Table:
    columns = []
    fks = tdef.foreign_keys
    for col in tdef.column_names:
        col_type = _sql_type(tdef.column_type(col))
        args = [col, col_type]
        if col in fks:
            args.append(ForeignKey(fks[col]))
        columns.append(Column(*args, primary_key=(col == tdef.primary_key)))
    table = Table(name, metadata, *columns)

    # 外部キー列と宣言インデックスへ索引を張る(#V6)。JOIN や時系列ビューの
    # WHERE/ORDER BY がフルスキャンになるのを避ける。名前で重複排除する。
    index_cols: Dict[str, list] = {}
    for col in fks:
        index_cols[f"ix_{name}_{col}"] = [col]
    for cols in tdef.indexes:
        index_cols[f"ix_{name}_{'_'.join(cols)}"] = list(cols)
    for idx_name, cols in index_cols.items():
        Index(idx_name, *[table.c[c] for c in cols])
    return table


class TableRegistry:
    """設定上の全テーブルを ``Table`` オブジェクトとして保持する。"""

    def __init__(self, config: MonitorConfig) -> None:
        self.metadata = MetaData()
        self.tables: Dict[str, Table] = {}
        self.primary_keys: Dict[str, str] = {}
        for name, tdef in config.tables.items():
            self.tables[name] = _build_table(name, tdef, self.metadata)
            self.primary_keys[name] = tdef.primary_key

    def has(self, table_name: str) -> bool:
        return table_name in self.tables

    def get(self, table_name: str) -> Table:
        return self.tables[table_name]

    def primary_key(self, table_name: str) -> str:
        return self.primary_keys[table_name]

    def create_all(self, db: Database) -> None:
        """未作成のテーブルを物理 DB に作成する。"""
        self.metadata.create_all(db.engine)
