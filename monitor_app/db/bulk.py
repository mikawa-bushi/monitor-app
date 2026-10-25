"""方言に応じた一括 INSERT(主キー衝突の扱いつき)と PostgreSQL の連番の追従。

CSV 取り込み・ソースコネクタ・インジェスト API が共有する。

- ``on_conflict="error"`` … 普通の INSERT。主キーが既存の行と重なれば
  ``IntegrityError``(呼び出し側のトランザクションごと取り消される)。
- ``on_conflict="ignore"`` … 主キーが既にある行を読み飛ばす(再送されたバッチを
  無害にする — CRIT-08)。
- ``on_conflict="update"`` … 主キーが既にある行を上書きする(UPSERT)。

衝突の判定そのものは DB の構文(SQLite/PostgreSQL の ``ON CONFLICT``、MySQL の
``ON DUPLICATE KEY UPDATE``)で原子的に行う。挿入件数はドライバの rowcount が
信用できない(psycopg の executemany は -1、MySQL の UPSERT は更新を 2 と数える)
ため、同じトランザクション内で既存の主キーを先に調べて数える(同じキーを同時に
書き込む別の接続があるときだけ件数がずれ得る)。

MySQL の ``INSERT IGNORE`` は使わない。重複キー以外のエラー(文字列長超過・数値の
範囲外・外部キー違反)まで警告に格下げして、値を切り詰めたり行を黙って捨てたり
するため。代わりに主キーを自分自身で更新する(実質なにもしない)UPSERT を使う。
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from itertools import islice
from typing import Any, Literal

from sqlalchemy import Integer, Table, bindparam, insert, select, text, update
from sqlalchemy.engine import Connection

OnConflict = Literal["error", "ignore", "update"]
ON_CONFLICT_MODES = ("error", "ignore", "update")

#: 既存の主キーを調べる IN 句 1 回あたりのキー数。古い SQLite のパラメータ上限
#: (999)を超えないようにする。
_LOOKUP_CHUNK = 500

Row = dict[str, Any]


def primary_key_name(table: Table) -> str:
    """単一列の主キー名。主キーが無い・複合のときは ValueError。"""
    cols = list(table.primary_key.columns)
    if len(cols) != 1:
        raise ValueError(f"テーブル '{table.name}' に単一列の主キーがありません")
    return cols[0].name


def existing_keys(conn: Connection, table: Table, pk: str, keys: Iterable[Any]) -> set:
    """``keys`` のうち、テーブルに既にある主キーの集合。"""
    col = table.c[pk]
    wanted = list(dict.fromkeys(k for k in keys if k is not None))
    found: set = set()
    for i in range(0, len(wanted), _LOOKUP_CHUNK):
        part = wanted[i : i + _LOOKUP_CHUNK]
        found.update(conn.execute(select(col).where(col.in_(part))).scalars())
    return found


def advance_sequences(conn: Connection, table: Table, pk: str | None = None) -> None:
    """PostgreSQL で、主キーを明示して挿入した後に連番(SERIAL)を最大値へ進める。

    CSV や送信元が持つ id を入れると連番が 1 のまま残り、次の画面・フォームからの
    追加が ``duplicate key`` で失敗する(CRIT-01)。連番を持たない列では
    ``pg_get_serial_sequence`` が NULL を返し、何もしない。ほかの DB は自動で追従
    するため何もしない。

    連番は戻さない(既に払い出した値と表の最大値の大きい方へ進めるだけ)。同時に
    動く別のトランザクションが採番した未確定の id より小さくすると、後でその id と
    衝突するため。表の id がすべて 0 以下でも失敗しない(連番の下限は 1)。
    """
    if conn.dialect.name != "postgresql":
        return
    pk = pk or primary_key_name(table)
    if not isinstance(table.c[pk].type, Integer):
        return
    preparer = conn.dialect.identifier_preparer
    qualified = preparer.format_table(table)
    column = preparer.quote(pk)
    conn.execute(
        text(
            "SELECT setval(CAST(x.seq AS regclass), GREATEST(x.top, 1), x.top >= 1) "
            "FROM (SELECT s.seq, GREATEST(COALESCE(m.v, 0), COALESCE("
            "pg_sequence_last_value(CAST(s.seq AS regclass)), 0)) AS top "
            "FROM (SELECT pg_get_serial_sequence(:table, :pk) AS seq) AS s, "
            f"(SELECT MAX({column}) AS v FROM {qualified}) AS m "
            "WHERE s.seq IS NOT NULL) AS x"
        ),
        {"table": qualified, "pk": pk},
    )


def sequence_lag(
    conn: Connection, table: Table, pk: str | None = None
) -> tuple[int, int] | None:
    """PostgreSQL で連番が表の最大値より遅れていれば (連番の最後の値, 表の最大値)。

    遅れていない・連番を持たない列・PostgreSQL 以外では None(読み取りのみ)。
    v2.3.1 までの CSV 取り込みは id を指定して入れても連番を進めなかったため、その
    データベースでは画面・API からの追加が主キーの重複で失敗する(D5-01)。
    """
    if conn.dialect.name != "postgresql":
        return None
    pk = pk or primary_key_name(table)
    if not isinstance(table.c[pk].type, Integer):
        return None
    preparer = conn.dialect.identifier_preparer
    qualified = preparer.format_table(table)
    seq = conn.execute(
        text("SELECT pg_get_serial_sequence(:table, :pk)"),
        {"table": qualified, "pk": pk},
    ).scalar()
    if seq is None:
        return None
    row = conn.execute(
        text(
            "SELECT COALESCE(pg_sequence_last_value(CAST(:seq AS regclass)), 0), "
            f"(SELECT MAX({preparer.quote(pk)}) FROM {qualified})"
        ),
        {"seq": seq},
    ).one()
    last, top = int(row[0]), row[1]
    if top is None or int(top) <= last:
        return None
    return last, int(top)


def sequence_fix_sql(conn_or_dialect: Any, table: Table, pk: str) -> str:
    """遅れた連番を表の最大値へ進める SQL(案内の表示用。PostgreSQL)。"""
    dialect = getattr(conn_or_dialect, "dialect", conn_or_dialect)
    preparer = dialect.identifier_preparer
    qualified = preparer.format_table(table)
    literal = qualified.replace("'", "''")
    pk_literal = pk.replace("'", "''")
    return (
        f"SELECT setval(pg_get_serial_sequence('{literal}', '{pk_literal}'), "
        f"(SELECT MAX({preparer.quote(pk)}) FROM {qualified}))"
    )


def repair_sequence(
    conn: Connection, table: Table, pk: str | None = None
) -> tuple[int, int] | None:
    """PostgreSQL で遅れた連番を表の最大値へ進め、直す前の (連番, 最大値) を返す。

    遅れていなければ何もしない(None。連番を書き換える権限が無くても失敗しない)。
    何度呼んでもよく、連番を戻すことは無い(:func:`advance_sequences`)。
    """
    lag = sequence_lag(conn, table, pk)
    if lag is not None:
        advance_sequences(conn, table, pk)
    return lag


def _chunks(rows: Iterable[Row], size: int) -> Iterator[list[Row]]:
    it = iter(rows)
    while True:
        chunk = list(islice(it, size))
        if not chunk:
            return
        yield chunk


def _runs(rows: Sequence[Row]) -> Iterator[list[Row]]:
    """同じ列の組を持つ連続した行のまとまり(順序は保つ)。

    executemany は 1 行目の列だけで文を組み立てるため、列の組が違う行を混ぜると
    2 行目以降の余分な列が黙って捨てられたり、足りない列でエラーになったりする。
    """
    run: list[Row] = []
    keys = None
    for row in rows:
        row_keys = row.keys()
        if run and row_keys != keys:
            yield run
            run = []
        if not run:
            keys = row_keys
        run.append(row)
    if run:
        yield run


class _Writer:
    """1 回の :func:`insert_rows` の状態(方言・主キー・PostgreSQL の連番)。"""

    def __init__(self, conn: Connection, table: Table, pk: str | None) -> None:
        self.conn = conn
        self.table = table
        self.pk = pk
        self.dialect = conn.dialect.name
        self.auto_key = self._auto_key(table, pk)
        self._explicit_since_advance = False

    @staticmethod
    def _auto_key(table: Table, pk: str | None) -> str | None:
        """DB が採番する整数の主キー列名(該当しなければ None)。"""
        if pk is None:
            cols = list(table.primary_key.columns)
            if len(cols) != 1:
                return None
            pk = cols[0].name
        col = table.c[pk]
        return pk if col.primary_key and isinstance(col.type, Integer) else None

    def prepare(self, rows: list[Row]) -> list[Row]:
        """主キーが None の行から主キーを外す(DB に採番させる)。

        PostgreSQL は SERIAL 列へ明示した NULL を NOT NULL 違反にする(SQLite・MySQL
        は採番する)。どの DB でも「主キーなし = 採番」になるよう揃える。
        """
        key = self.auto_key
        if key is None or not any(key in r and r[key] is None for r in rows):
            return rows
        return [
            (
                {k: v for k, v in r.items() if k != key}
                if key in r and r[key] is None
                else r
            )
            for r in rows
        ]

    def execute(self, stmt: Any, run: list[Row]) -> None:
        """同じ列の組の ``run`` を executemany する。

        PostgreSQL では、主キーを明示した行を入れた後に採番の行を入れる前に連番を
        追従させる(同じバッチ内でも採番が既存の id と衝突しない)。
        """
        key = self.auto_key
        if key is not None and self.dialect == "postgresql":
            if key in run[0]:
                self._explicit_since_advance = True
            else:
                self.finish()
        self.conn.execute(stmt, run)

    def finish(self) -> None:
        """主キーを明示した行を入れていれば、PostgreSQL の連番を最大値へ進める。"""
        if self._explicit_since_advance:
            advance_sequences(self.conn, self.table, self.auto_key)
            self._explicit_since_advance = False

    # ------------------------------------------------------------------
    def _dialect_insert(self):
        if self.dialect == "sqlite":
            from sqlalchemy.dialects.sqlite import insert as dialect_insert
        elif self.dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert as dialect_insert
        else:
            from sqlalchemy.dialects.mysql import insert as dialect_insert
        return dialect_insert(self.table)

    def supports_upsert(self) -> bool:
        if self.dialect == "sqlite":
            # UPSERT 構文は SQLite 3.24 以降。
            version = getattr(self.conn.dialect, "server_version_info", None) or (0,)
            return tuple(version) >= (3, 24)
        return self.dialect in ("postgresql", "mysql", "mariadb")

    def _mysql_noop(self, stmt):
        """MySQL: 衝突したら何も変えない UPSERT(``pk = 表.pk``)。

        ``pk = VALUES(pk)`` にすると、大文字小文字を区別しない照合順序(MySQL 8 の既定
        ``utf8mb4_0900_ai_ci``)で ``'abc'`` が既存の ``'ABC'`` と衝突したとき、既存の
        行の主キーが ``'abc'`` に書き換わってしまう。
        """
        return stmt.on_duplicate_key_update({self.pk: self.table.c[self.pk]})

    def ignore_statement(self):
        """主キーの衝突だけを読み飛ばす INSERT(先に調べた後の競合への保険)。"""
        stmt = self._dialect_insert()
        if self.dialect in ("mysql", "mariadb"):
            return self._mysql_noop(stmt)
        return stmt.on_conflict_do_nothing(index_elements=[self.pk])

    def upsert_statement(self, columns: Iterable[str]):
        """主キーが衝突したら ``columns`` を新しい値で上書きする INSERT。"""
        stmt = self._dialect_insert()
        others = [c for c in columns if c != self.pk]
        if self.dialect in ("mysql", "mariadb"):
            if not others:  # 主キーしか無い行は更新するものがない
                return self._mysql_noop(stmt)
            return stmt.on_duplicate_key_update({c: stmt.inserted[c] for c in others})
        if not others:  # 主キーしか無い行は更新するものがない
            return stmt.on_conflict_do_nothing(index_elements=[self.pk])
        return stmt.on_conflict_do_update(
            index_elements=[self.pk], set_={c: stmt.excluded[c] for c in others}
        )

    # ------------------------------------------------------------------
    def insert_plain(self, rows: Sequence[Row]) -> None:
        for run in _runs(rows):
            self.execute(insert(self.table), run)

    def insert_ignore(self, rows: list[Row]) -> int:
        pk = self.pk
        seen = set(existing_keys(self.conn, self.table, pk, (r.get(pk) for r in rows)))
        fresh: list[Row] = []
        for row in rows:
            key = row.get(pk)
            if key is None:  # 主キーは DB が採番する(衝突し得ない)
                fresh.append(row)
            elif key not in seen:  # バッチ内の重複は最初の 1 行だけ
                seen.add(key)
                fresh.append(row)
        if not fresh:
            return 0
        if self.supports_upsert():
            for run in _runs(fresh):
                stmt = self.ignore_statement() if pk in run[0] else insert(self.table)
                self.execute(stmt, run)
        else:
            self.insert_plain(fresh)
        return len(fresh)

    def insert_update(self, rows: list[Row]) -> int:
        pk = self.pk
        # バッチ内で同じ主キーが複数回あれば 1 行にまとめる(PostgreSQL は 1 文の中で
        # 同じ行を 2 度更新できない)。前から順に上書きした結果にし(後の行の値が勝つ)、
        # 前の行にだけある列の値も残す(順に UPSERT したのと同じ)。位置は最初の出現。
        merged: dict[Any, Row] = {}
        deduped: list[Row] = []
        for row in rows:
            key = row.get(pk)
            if key is None:
                deduped.append(row)
            elif key in merged:
                merged[key].update(row)
            else:
                merged[key] = dict(row)
                deduped.append(merged[key])
        existing = existing_keys(self.conn, self.table, pk, merged)
        inserted = sum(1 for r in deduped if r.get(pk) is None or r[pk] not in existing)
        if self.supports_upsert():
            for run in _runs(deduped):
                if pk in run[0]:
                    self.execute(self.upsert_statement(run[0].keys()), run)
                else:  # 主キーなし(採番)の行は普通の INSERT
                    self.execute(insert(self.table), run)
            return inserted
        # UPSERT 構文の無い DB: 既存の行は UPDATE、それ以外は INSERT。
        for run in _runs(deduped):
            updates = [r for r in run if r.get(pk) is not None and r[pk] in existing]
            inserts = [r for r in run if r.get(pk) is None or r[pk] not in existing]
            others = [c for c in run[0] if c != pk]
            if updates and others:
                # 列名と同じ名前の bindparam は SET 句の自動パラメータと衝突するため
                # 接頭辞を付ける。
                stmt = (
                    update(self.table)
                    .where(self.table.c[pk] == bindparam("_pk"))
                    .values({c: bindparam(f"_v_{c}") for c in others})
                )
                self.conn.execute(
                    stmt,
                    [
                        {"_pk": r[pk], **{f"_v_{c}": r[c] for c in others}}
                        for r in updates
                    ],
                )
            if inserts:
                self.insert_plain(inserts)
        return inserted


def insert_rows(
    conn: Connection,
    table: Table,
    rows: Iterable[Row],
    *,
    on_conflict: OnConflict = "error",
    pk: str | None = None,
    chunk: int = 1000,
) -> int:
    """``rows`` を ``chunk`` 行ずつ executemany で挿入し、新しく入った行数を返す。

    ``on_conflict`` は主キーが既存の行(または同じバッチの前の行)と重なったときの
    扱い(モジュールの説明を参照)。``"ignore"`` で読み飛ばした行・``"update"`` で
    上書きした行は戻り値に数えない。``pk`` を省略するとテーブルの主キーを使う。
    ``rows`` は反復子でもよい(``chunk`` 行ずつしか保持しない)。トランザクションは
    呼び出し側が持つ。

    列の組が行ごとに違ってもよい(列を落とさない)。主キーが None の行は DB に採番
    させる。主キーを明示した行を入れたら、PostgreSQL の連番を最大値へ進めてから
    返す(:func:`advance_sequences`。次の採番が衝突しない)。
    """
    if on_conflict not in ON_CONFLICT_MODES:
        raise ValueError(
            f"on_conflict は {', '.join(ON_CONFLICT_MODES)} のいずれかです: "
            f"{on_conflict!r}"
        )
    if chunk < 1:
        raise ValueError("chunk は 1 以上です")
    if on_conflict != "error" and pk is None:
        pk = primary_key_name(table)
    writer = _Writer(conn, table, pk)
    total = 0
    for part in _chunks(rows, chunk):
        part = writer.prepare(part)
        if on_conflict == "error":
            writer.insert_plain(part)
            total += len(part)
        elif on_conflict == "ignore":
            total += writer.insert_ignore(part)
        else:
            total += writer.insert_update(part)
    writer.finish()
    return total
