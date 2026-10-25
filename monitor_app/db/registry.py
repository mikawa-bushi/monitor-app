"""宣言的設定から SQLAlchemy Core の ``Table`` オブジェクトを構築・保持する。

ここで生成した ``Table`` / ``Column`` オブジェクトだけがクエリ構築に使われる。
テーブル名・列名を文字列として SQL に埋め込まないことで、インジェクションを
「コード規約」ではなく「型構造」で構造的に防ぐ。
"""

from __future__ import annotations

import logging

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Date,
    Double,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
)

from ..settings.declarative import ColumnType, MonitorConfig, TableDef
from .bulk import repair_sequence, sequence_fix_sql
from .engine import Database
from .errors import driver_message
from .types import datetime_type

logger = logging.getLogger("monitor_app.db")

#: 論理型 → SQL 型。float は倍精度の ``Double``(MySQL の FLOAT は単精度で、
#: 1234567.89 が 1234570.0 になる — CRIT-01)。PostgreSQL・SQLite では従来の Float と
#: 同じ倍精度。型を変えても作成済みの表は変わらない(新しく作る表だけに効く)。
#: datetime は DB ごとに保存形式をそろえた型(:mod:`monitor_app.db.types`)。
_SQL_TYPES = {
    "int": Integer,
    "float": Double,
    "str": lambda: String(255),
    "bool": Boolean,
    "date": Date,
    "datetime": datetime_type,
}


def _sql_type(logical: ColumnType):
    factory = _SQL_TYPES[logical]
    return factory()


def _wide_int() -> Integer:
    """主キー・外部キー以外の整数列の型(PostgreSQL / MySQL では 64 ビットの BIGINT)。

    Unix 時刻のミリ秒(1729500000000)など 32 ビットを超える整数を、SQLite と同じく
    そのまま保存できるようにする(INTEGER の列では、その行を警告して読み飛ばす —
    D3-06)。主キー(連番)・外部キー・外部キーの参照先の列は INTEGER のまま
    (MySQL は外部キーと参照先の型が同じでないと表を作れない)。SQLite は従来どおり
    INTEGER(64 ビット)。作成済みの表は変わらない(新しく作る表だけに効く)。
    """
    return Integer().with_variant(BigInteger(), "postgresql", "mysql", "mariadb")


def _referenced_columns(config: MonitorConfig) -> set[tuple[str, str]]:
    """外部キーの参照先(``"users.id"``)の (テーブル, 列)。"""
    found: set[tuple[str, str]] = set()
    for tdef in config.tables.values():
        for col, ref in tdef.foreign_keys.items():
            table, _, column = ref.partition(".")
            # 列名を省いた参照は、SQLAlchemy と同じく同じ名前の列とみなす
            found.add((table, column or col))
    return found


def _build_table(
    name: str,
    tdef: TableDef,
    metadata: MetaData,
    referenced: set[tuple[str, str]],
) -> Table:
    columns = []
    fks = tdef.foreign_keys
    for col in tdef.column_names:
        logical = tdef.column_type(col)
        keyed = col == tdef.primary_key or col in fks or (name, col) in referenced
        col_type = _wide_int() if logical == "int" and not keyed else _sql_type(logical)
        args = [col, col_type]
        if col in fks:
            args.append(ForeignKey(fks[col]))
        columns.append(Column(*args, primary_key=(col == tdef.primary_key)))
    table = Table(name, metadata, *columns)

    # 外部キー列と宣言インデックスへ索引を張る(#V6)。JOIN や時系列ビューの
    # WHERE/ORDER BY がフルスキャンになるのを避ける。名前で重複排除する。
    index_cols: dict[str, list] = {}
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
        self.tables: dict[str, Table] = {}
        self.primary_keys: dict[str, str] = {}
        #: 連番を確かめたエンジン(DB ごとに最初の create_all で 1 回だけ確かめる)。
        self._sequences_checked: set[int] = set()
        referenced = _referenced_columns(config)
        for name, tdef in config.tables.items():
            self.tables[name] = _build_table(name, tdef, self.metadata, referenced)
            self.primary_keys[name] = tdef.primary_key

    def has(self, table_name: str) -> bool:
        return table_name in self.tables

    def get(self, table_name: str) -> Table:
        return self.tables[table_name]

    def primary_key(self, table_name: str) -> str:
        return self.primary_keys[table_name]

    def create_all(self, db: Database) -> None:
        """未作成のテーブルを物理 DB に作成する。

        PostgreSQL では、DB ごとに最初の 1 回だけ遅れた連番も直す
        (:meth:`repair_sequences`)。
        サーバーの起動・CSV の取り込み・ソースの同期はすべてここを通る。
        """
        self.metadata.create_all(db.engine)
        if id(db.engine) not in self._sequences_checked:
            self._sequences_checked.add(id(db.engine))
            self.repair_sequences(db)

    def repair_sequences(self, db: Database) -> dict[str, tuple[int, int]]:
        """PostgreSQL で、表の最大値より遅れた id の連番を最大値へ進める(D5-01)。

        v2.3.1 までの CSV 取り込みは id を指定して入れても連番を進めなかったため、
        更新したデータベースでは画面・API からの追加が主キーの重複(409)で失敗して
        いた。遅れていない連番には触れない(書き換える権限が無くても起動できる)。
        戻り値はテーブル名 → 直す前の (連番, 最大値)。直せなかった表は警告して飛ばす。
        PostgreSQL 以外では何もしない(MySQL・SQLite は自動で追従する)。
        """
        if db.dialect != "postgresql":
            return {}
        fixed: dict[str, tuple[int, int]] = {}
        for name, table in self.tables.items():
            pk = self.primary_keys[name]
            if not isinstance(table.c[pk].type, Integer):
                continue
            try:
                with db.connect() as conn:
                    lag = repair_sequence(conn, table, pk)
            except Exception as exc:  # noqa: BLE001 - 直せなくても起動は止めない
                logger.warning(
                    "テーブル '%s' の連番(%s)を確認・修正できませんでした: %s"
                    "(主キーの重複で追加が失敗するときは、管理者で次の SQL を"
                    "実行してください: %s)",
                    name,
                    pk,
                    driver_message(exc),
                    sequence_fix_sql(db.engine, table, pk),
                )
                continue
            if lag is not None:
                fixed[name] = lag
                logger.info(
                    "テーブル '%s' の連番(%s)が表の最大値より遅れていたため、%d → %d に"
                    "進めました(以前の版で id を指定して取り込んだ表)",
                    name,
                    pk,
                    lag[0],
                    lag[1],
                )
        return fixed
