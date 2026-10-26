"""ソースごとの取り込みカーソルを永続化するストア。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    Column,
    MetaData,
    String,
    Table,
    delete,
    insert,
    inspect,
    select,
    text,
)
from sqlalchemy.engine import Connection

from .engine import Database

#: 型付きカーソル(JSON)の目印。v2.3.1 までのカーソルは値をそのまま文字列にした
#: もので、この形では始まらない(ウォーターマーク列の値そのものが JSON の場合を除く)。
_TYPED_PREFIX = '{"v":'
_BYTES_PREFIX = '{"x":'


def encode_cursor(value: Any) -> str:
    """ウォーターマークの値を、型を保ったまま保存できる文字列(JSON)にする。

    int / float / str は ``{"v": 値}``、bytes は ``{"x": "16進"}``。文字列にして
    しまうと、型の無い列(``CREATE TABLE s (id, ts)``)で整数と文字列の比較になり、
    以後の行を取り込めなくなるため(ARCH-03)。
    """
    if isinstance(value, (bytes, bytearray, memoryview)):
        return json.dumps({"x": bytes(value).hex()}, separators=(",", ":"))
    return json.dumps({"v": value}, ensure_ascii=False, separators=(",", ":"))


def decode_cursor(raw: str | None) -> Any:
    """:func:`encode_cursor` の逆。v2.3.1 までの文字列のカーソルはそのまま返す。"""
    if raw is None or not raw.startswith((_TYPED_PREFIX, _BYTES_PREFIX)):
        return raw
    try:
        data = json.loads(raw)
    except ValueError:
        return raw
    if not isinstance(data, dict) or len(data) != 1:
        return raw
    if "v" in data:
        return data["v"]
    if "x" in data and isinstance(data["x"], str):
        try:
            return bytes.fromhex(data["x"])
        except ValueError:
            return raw
    return raw


#: 型付きカーソル(``{"v": 値}``)を v2.3.x の形(値をそのまま文字列にしたもの)へ
#: 戻す SQL(2.3.x に戻す前に実行する)。行を消すと 2.3.x が最初から取り込み直し、
#: 主キーの重複で毎回失敗したり行が重複したりする(D5-02)。
LEGACY_CURSOR_SQL = {
    "sqlite": (
        "UPDATE _ingest_state SET cursor = json_extract(cursor, '$.v') "
        "WHERE cursor LIKE '{\"v\":%'"
    ),
    "postgresql": (
        "UPDATE _ingest_state SET cursor = (cursor::json ->> 'v') "
        "WHERE cursor LIKE '{\"v\":%'"
    ),
    "mysql": (
        "UPDATE _ingest_state SET `cursor` = JSON_UNQUOTE(JSON_EXTRACT(`cursor`, '$.v')) "
        "WHERE `cursor` LIKE '{\"v\":%'"
    ),
}
LEGACY_CURSOR_SQL["mariadb"] = LEGACY_CURSOR_SQL["mysql"]


def unwrap_typed_cursors(db: Database | str) -> int:
    """取り込みカーソルを v2.3.x が読める形に戻し、戻した行数を返す(2.3.x に戻す前用)。

    ``db`` は :class:`Database` か接続 URL(例 ``sqlite:///instances/database.db``)。
    2.4 の型付きカーソル ``{"v": 12}`` を ``12`` に戻す(2.3.x はこの形のまま比べる
    ため、戻さないと以後の行を取り込めない)。``_ingest_state`` 表が無ければ 0。
    2.4 は戻した形もそのまま読める(戻した後に 2.4 を使い続けても問題ない)。
    """
    own = isinstance(db, str)
    database = Database(db) if own else db
    try:
        sql = LEGACY_CURSOR_SQL.get(database.dialect)
        if sql is None:
            raise ValueError(f"{database.dialect} には対応していません")
        if not inspect(database.engine).has_table("_ingest_state"):
            return 0
        with database.connect() as conn:
            return max(conn.execute(text(sql)).rowcount or 0, 0)
    finally:
        if own:
            database.dispose()


class IngestStateStore:
    """ソースごとの取り込みカーソルを monitor-app 側 DB に永続化する。"""

    def __init__(self, db: Database) -> None:
        self._db = db
        self._metadata = MetaData()
        # MySQL は VARCHAR に長さが必須(長さなしでは CREATE TABLE できない — CRIT-01)。
        # 作成済みの表(SQLite の VARCHAR など)はそのまま使える。
        self._table = Table(
            "_ingest_state",
            self._metadata,
            Column("source", String(255), primary_key=True),
            Column("cursor", String(255), nullable=False),
            Column("updated_at", String(255), nullable=False),
        )

    def create(self) -> None:
        """_ingest_state テーブルを必要なら作成する(冪等)。"""
        self._metadata.create_all(self._db.engine)

    def get(
        self,
        source: str,
        *,
        conn: Connection | None = None,
        for_update: bool = False,
    ) -> str | None:
        """カーソル(保存した文字列そのもの)を返す。未登録なら None。

        ``conn`` を渡すとそのトランザクションの中で読む。``for_update=True`` は
        PostgreSQL / MySQL で行をロックする(読んでから書くまでに、ほかのプロセスが
        同じカーソルを進めないように — D4-12)。
        """
        stmt = select(self._table.c.cursor).where(self._table.c.source == source)
        if for_update:
            stmt = stmt.with_for_update()
        if conn is None:
            with self._db.connect() as own:
                row = own.execute(stmt).fetchone()
        else:
            row = conn.execute(stmt).fetchone()
        return row[0] if row is not None else None

    def set(self, source: str, cursor: str, *, conn: Connection | None = None) -> None:
        """カーソルを保存する(UPSERT 相当: delete → insert)。

        ``conn`` を渡すと、そのトランザクションの中で書く。取り込んだ行とカーソルを
        同じトランザクションで確定させると、途中で止まっても「行は入ったがカーソルは
        古いまま」(次回に同じ行を取り込み直して主キー重複が続く)にならない(ARCH-03)。
        """
        if conn is None:
            with self._db.connect() as own:
                self._write(own, source, cursor)
        else:
            self._write(conn, source, cursor)

    def get_value(self, source: str, *, conn: Connection | None = None) -> Any:
        """型付きで保存したカーソル(:meth:`set_value`)を元の型で返す。

        v2.3.1 までの文字列のカーソルは文字列のまま返す(従来どおり使える)。
        """
        return decode_cursor(self.get(source, conn=conn))

    def delete(self, source: str, *, conn: Connection | None = None) -> None:
        """カーソルを消す(無ければ何もしない)。"""
        stmt = delete(self._table).where(self._table.c.source == source)
        if conn is None:
            with self._db.connect() as own:
                own.execute(stmt)
        else:
            conn.execute(stmt)

    def set_value(
        self, source: str, value: Any, *, conn: Connection | None = None
    ) -> None:
        """カーソルを型付き(JSON)で保存する(:func:`encode_cursor`)。"""
        self.set(source, encode_cursor(value), conn=conn)

    def _write(self, conn: Connection, source: str, cursor: str) -> None:
        updated_at = datetime.now(UTC).isoformat()
        conn.execute(delete(self._table).where(self._table.c.source == source))
        conn.execute(
            insert(self._table).values(
                source=source,
                cursor=cursor,
                updated_at=updated_at,
            )
        )
