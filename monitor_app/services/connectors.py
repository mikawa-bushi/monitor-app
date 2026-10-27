"""外部データソースコネクタ (sqlite / jsonl / json)。

各コネクタは ``poll_once()`` を呼ばれるたびに増分または全件を monitor-app 側 DB に
取り込み、取り込んだ行数を返す。監視ループから繰り返し呼ばれることを前提にしており、
ファイル未存在・DB ロックなどのエラーは warning ログに留めて 0 を返す(ループを殺さない)。

取り込んだ行と取り込みカーソル(どこまで読んだか)は同じトランザクションで確定する。
途中で止まっても(電源断など)「行は入ったがカーソルは古いまま」にならず、次回に同じ
行を取り込み直して主キーの重複が続くことがない(ARCH-03)。書き込む前に、そのトランザク
ションの中でカーソルが読んだときのままかを確かめ、ほかのプロセス(cron の
``sync-sources`` とフォルダ監視など)が先に取り込んでいれば今回は書き込まない(D4-12)。

ファイル全体を毎回読み直すソース(``kind="json"``、watermark の無い ``query`` の
``sqlite``)を追記(既定)で取り込むときは、主キーが既にある行を読み飛ばす(変わる
たびに同じ行を入れ直して主キーの重複で失敗し続けない — D3-08)。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from sqlalchemy import delete

from ..db.engine import Database
from ..db.ingest_state import IngestStateStore, decode_cursor
from ..db.registry import TableRegistry
from ..settings.declarative import MonitorConfig, SourceDef
from .importer import insert_in_chunks
from .rows import ColumnError, RowConverter
from .rows import dot_get as _dot_get
from .rows import flatten as _flatten  # noqa: F401 - 従来の名前で import できるように
from .value_limits import ValueLimits, reflected_limits

logger = logging.getLogger("monitor_app.connectors")

#: カーソルをまだ読んでいないことを表す値(保存したカーソルが無い None と区別する)。
_UNREAD = object()


def _sqlite_order(value: Any) -> tuple[int, Any]:
    """SQLite の並び順(数値 < 文字列 < BLOB)で比べるためのキー。"""
    if isinstance(value, (int, float)):
        return (0, value)
    if isinstance(value, str):
        return (1, value)
    return (2, bytes(value))


# ---------------------------------------------------------------------------
# 基底クラス
# ---------------------------------------------------------------------------


class SourceConnector(ABC):
    """外部データソースコネクタの基底クラス。"""

    def __init__(
        self,
        name: str,
        sdef: SourceDef,
        config: MonitorConfig,
        registry: TableRegistry,
        db: Database,
        state: IngestStateStore,
        *,
        on_change: Callable[[], None] | None = None,
    ) -> None:
        self.name = name
        self.sdef = sdef
        self.config = config
        self.registry = registry
        self.db = db
        self.state = state
        self.path = Path(sdef.path).expanduser()
        #: 行が変わる書き込みを確定した後に呼ぶ(ビューのキャッシュを捨てる等)。
        self.on_change = on_change
        self._converter: RowConverter | None = None
        #: DB の列の制限(サーバー DB だけ。最初に書き込むときに 1 度だけ作る — D3-06)。
        self._limits: ValueLimits | None = None
        self._limits_ready = False

    @abstractmethod
    def poll_once(self) -> int:
        """データを取り込み、挿入した行数を返す。"""

    # ------------------------------------------------------------------
    # 共通: raw レコード列挙 → 変換 → DB 挿入
    # ------------------------------------------------------------------

    @property
    def converter(self) -> RowConverter:
        """取り込み先テーブルの変換規則(最初に使うときに 1 度だけ作る)。"""
        if self._converter is None:
            self._converter = RowConverter(
                self.config.tables[self.sdef.table], mapping=self.sdef.mapping
            )
        return self._converter

    @property
    def snapshot(self) -> bool:
        """変わるたびにファイル全体を読み直すソースか(json・watermark の無い query)。"""
        if self.sdef.kind == "json":
            return True
        return (
            self.sdef.kind == "sqlite"
            and self.sdef.query is not None
            and self.sdef.watermark_column is None
        )

    def _convert(self, raw_records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        """raw レコードを列の論理型へ変換する。

        変換失敗行はスキップして warning。未知列は破棄(mapping があれば mapping の列
        だけを読む)。
        """
        converter = self.converter
        rows: list[dict[str, Any]] = []
        for raw in raw_records:
            try:
                record = converter.from_record(raw)
            except ColumnError as exc:
                logger.warning(
                    "source '%s': 型変換失敗(列 %s): %s — 行スキップ",
                    self.name,
                    exc.column,
                    exc,
                )
                continue
            if record:
                rows.append(record)
        return rows

    def _within_limits(self, conn, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """DB の列に入らない値(長すぎる文字列・範囲外の整数)の行を除く(D3-06)。"""
        if not self._limits_ready:
            self._limits = reflected_limits(conn, self.registry.get(self.sdef.table))
            self._limits_ready = True
        limits = self._limits
        if limits is None:
            return rows
        kept = []
        for row in rows:
            for col, value in row.items():
                reason = limits.violation(col, value)
                if reason is not None:
                    logger.warning("source '%s': %s — 行スキップ", self.name, reason)
                    break
            else:
                kept.append(row)
        return kept

    def _skip_existing(self, rows: list[dict[str, Any]]) -> bool:
        """追記で、主キーが既にある行を読み飛ばすか(ファイル全体を毎回読むソース)。

        行に主キーが無い(DB が採番する)ときは読み飛ばせない(``check`` が警告する)。
        """
        if self.sdef.mode != "append" or not self.snapshot:
            return False
        pk = self.registry.primary_key(self.sdef.table)
        return bool(rows) and all(row.get(pk) is not None for row in rows)

    def _write(
        self,
        rows: list[dict[str, Any]],
        *,
        replace: bool,
        cursor: str | None = None,
        typed_cursor: Any = None,
        expected: Any = None,
        check_cursor: bool = False,
    ) -> int:
        """行と取り込みカーソルを 1 トランザクションで書き込み、挿入した行数を返す。

        replace=True なら挿入前に既存行を全削除する(有効行 0 件なら削除しない — #16)。
        ``cursor`` は文字列のまま、``typed_cursor`` は型付きで保存する(None なら保存
        しない)。``check_cursor=True`` なら、書き込む前に保存済みのカーソルが
        ``expected``(読み始めたときの値)のままかを同じトランザクションの中で確かめ、
        ほかのプロセスが先に進めていれば何も書かずに 0 を返す(D4-12)。
        """
        table = self.registry.get(self.sdef.table)
        changed = False
        inserted = 0
        with self.db.write_transaction() as conn:
            if check_cursor:
                current = self.state.get(self.name, conn=conn, for_update=True)
                if current != expected:
                    logger.info(
                        "source '%s': ほかのプロセスが先に取り込んだため、今回は"
                        "取り込みません",
                        self.name,
                    )
                    return 0
            rows = self._within_limits(conn, rows)
            if replace and not rows:
                # 有効行 0 件での置換は既存データの全消去になるため中止する(#16)。
                logger.warning(
                    "source '%s': 有効行 0 件のため置換を中止(既存データを保持)",
                    self.name,
                )
            else:
                if replace:
                    conn.execute(delete(table))
                    changed = True
                if rows:
                    if self._skip_existing(rows):
                        inserted = insert_in_chunks(
                            conn, table, rows, on_conflict="ignore"
                        )
                    else:
                        inserted = insert_in_chunks(conn, table, rows)
                    changed = changed or inserted > 0
            if typed_cursor is not None:
                self.state.set_value(self.name, typed_cursor, conn=conn)
            elif cursor is not None:
                self.state.set(self.name, cursor, conn=conn)

        logger.info(
            "source '%s': %d 行取り込み (replace=%s)",
            self.name,
            inserted,
            replace,
        )
        if changed and self.on_change is not None:
            try:
                self.on_change()
            except Exception:  # 通知の失敗で取り込みを失敗にしない
                logger.warning(
                    "source '%s': 取り込み後の通知に失敗しました",
                    self.name,
                    exc_info=True,
                )
        return inserted


# ---------------------------------------------------------------------------
# SqliteSourceConnector
# ---------------------------------------------------------------------------


class SqliteSourceConnector(SourceConnector):
    """sqlite ファイルからの増分/全件取り込みコネクタ。"""

    def poll_once(self) -> int:
        if not self.path.exists():
            return 0

        wc = self.sdef.watermark_column
        saved = self.state.get(self.name)

        # watermark なし: mtime:size を cursor に使う
        if wc is None and self.sdef.query is None:
            stat = self.path.stat()
            cursor_val = f"{stat.st_mtime_ns}:{stat.st_size}"
            if cursor_val == saved:
                return 0

        try:
            uri = f"file:{self.path}?mode=ro"
            conn_src = sqlite3.connect(uri, uri=True)
            conn_src.row_factory = sqlite3.Row
        except sqlite3.Error as exc:
            logger.warning("source '%s': sqlite 接続失敗: %s", self.name, exc)
            return 0

        try:
            return self._poll_sqlite(conn_src, saved)
        finally:
            conn_src.close()

    def _poll_sqlite(self, conn_src: sqlite3.Connection, saved: Any = _UNREAD) -> int:
        wc = self.sdef.watermark_column
        st = self.sdef.source_table
        if saved is _UNREAD:
            saved = self.state.get(self.name)

        # SQL 決定。ウォーターマークは保存したときの型のまま渡す(文字列にすると、型の
        # 無い列で整数と文字列の比較になり、以後の行を取り込めない — ARCH-03)。
        # v2.3.1 までの文字列のカーソルは文字列のまま渡す(従来どおり)。
        if self.sdef.query:
            sql = self.sdef.query
            params: dict[str, Any] = {"wm": decode_cursor(saved)}
        elif wc:
            # ウォーターマークが NULL の行はカーソルで追えない(毎回取り込み直して
            # 重複する)ため取り込まない。
            sql = (
                f"SELECT * FROM {st} WHERE {wc} IS NOT NULL"
                f" AND (:wm IS NULL OR {wc} > :wm) ORDER BY {wc}"
            )
            params = {"wm": decode_cursor(saved)}
        else:
            sql = f"SELECT * FROM {st}"
            params = {}

        try:
            cur = conn_src.execute(sql, params)
            rows_raw = cur.fetchall()
        except sqlite3.Error as exc:
            logger.warning("source '%s': クエリ失敗: %s", self.name, exc)
            return 0

        if not rows_raw:
            # watermark なし: cursor を更新して 0 件
            if wc is None and not self.sdef.query:
                stat = self.path.stat()
                self.state.set(self.name, f"{stat.st_mtime_ns}:{stat.st_size}")
            return 0

        replace = self.sdef.mode == "replace" or (wc is None and not self.sdef.query)

        if wc:
            # 新しいカーソル(ウォーターマークの最大値)は挿入の前に決める。
            try:
                marked = [(r, r[wc]) for r in rows_raw]
            except (IndexError, KeyError):
                logger.warning(
                    "source '%s': query の結果に watermark 列 '%s' がありません",
                    self.name,
                    wc,
                )
                return 0
            usable = [r for r, value in marked if value is not None]
            if len(usable) < len(marked):
                logger.warning(
                    "source '%s': watermark 列 '%s' が NULL の %d 行は取り込みません",
                    self.name,
                    wc,
                    len(marked) - len(usable),
                )
            if not usable:
                return 0
            new_wm = max((r[wc] for r in usable), key=_sqlite_order)
            rows = self._convert(dict(r) for r in usable)
            return self._write(
                rows,
                replace=replace,
                typed_cursor=new_wm,
                expected=saved,
                check_cursor=True,
            )

        rows = self._convert(dict(r) for r in rows_raw)
        if self.sdef.query:
            return self._write(rows, replace=replace)
        # mtime:size cursor を保存
        stat = self.path.stat()
        return self._write(
            rows,
            replace=replace,
            cursor=f"{stat.st_mtime_ns}:{stat.st_size}",
            expected=saved,
            check_cursor=True,
        )


# ---------------------------------------------------------------------------
# JsonlSourceConnector
# ---------------------------------------------------------------------------


class JsonlSourceConnector(SourceConnector):
    """JSONL ファイルの追記を増分取り込みするコネクタ。"""

    def poll_once(self) -> int:
        if not self.path.exists():
            return 0

        try:
            stat = self.path.stat()
        except OSError as exc:
            logger.warning("source '%s': stat 失敗: %s", self.name, exc)
            return 0

        size = stat.st_size
        saved = self.state.get(self.name)
        offset = int(saved) if saved is not None else 0

        # ローテーション/truncate 検出
        if size < offset:
            logger.warning(
                "source '%s': ファイルが縮小 (offset=%d, size=%d) — 先頭から再読込",
                self.name,
                offset,
                size,
            )
            offset = 0

        if size == offset:
            return 0

        raw_records: list[dict[str, Any]] = []

        try:
            with self.path.open("rb") as fh:
                fh.seek(offset)
                buf = fh.read()
        except OSError as exc:
            logger.warning("source '%s': 読み取り失敗: %s", self.name, exc)
            return 0

        # 改行で終わった行だけを消費する(末尾の書きかけの行は次回に持ち越す)。
        lines = buf.split(b"\n")[:-1]
        pos = offset
        for line in lines:
            pos += len(line) + 1  # +1 for \n
            line_stripped = line.strip()
            if not line_stripped:
                continue
            try:
                obj = json.loads(line_stripped)
            except json.JSONDecodeError as exc:
                logger.warning(
                    "source '%s': JSON パース失敗: %s — 行スキップ", self.name, exc
                )
                continue
            if isinstance(obj, dict):
                raw_records.append(obj)
            else:
                logger.warning(
                    "source '%s': JSONL 行が object でない — スキップ",
                    self.name,
                )

        if not raw_records:
            if pos > offset:
                self.state.set(self.name, str(pos))
            return 0

        return self._write(
            self._convert(raw_records),
            replace=False,
            cursor=str(pos),
            expected=saved,
            check_cursor=True,
        )


# ---------------------------------------------------------------------------
# JsonSourceConnector
# ---------------------------------------------------------------------------


class JsonSourceConnector(SourceConnector):
    """JSON ファイルを全件取り込むコネクタ。変化なしはスキップ。"""

    def poll_once(self) -> int:
        if not self.path.exists():
            return 0

        try:
            stat = self.path.stat()
        except OSError as exc:
            logger.warning("source '%s': stat 失敗: %s", self.name, exc)
            return 0

        cursor_val = f"{stat.st_mtime_ns}:{stat.st_size}"
        saved = self.state.get(self.name)
        if cursor_val == saved:
            return 0

        try:
            with self.path.open(encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("source '%s': JSON 読み込み失敗: %s", self.name, exc)
            return 0

        # record_path を辿って list を取得
        record_path = self.sdef.record_path
        if record_path:
            try:
                data = _dot_get(data, record_path)
            except KeyError:
                logger.warning(
                    "source '%s': record_path '%s' が見つかりません",
                    self.name,
                    record_path,
                )
                return 0

        if isinstance(data, dict):
            data = [data]

        if not isinstance(data, list):
            logger.warning(
                "source '%s': record_path '%s' の値が list/dict でありません",
                self.name,
                record_path,
            )
            return 0

        records = [r for r in data if isinstance(r, dict)]
        if len(records) < len(data):
            logger.warning(
                "source '%s': object でないレコード %d 件をスキップ",
                self.name,
                len(data) - len(records),
            )
        replace = self.sdef.mode == "replace"
        return self._write(
            self._convert(records),
            replace=replace,
            cursor=cursor_val,
            expected=saved,
            check_cursor=True,
        )


# ---------------------------------------------------------------------------
# ファクトリ
# ---------------------------------------------------------------------------

_KIND_MAP = {
    "sqlite": SqliteSourceConnector,
    "jsonl": JsonlSourceConnector,
    "json": JsonSourceConnector,
}


def build_connectors(
    config: MonitorConfig,
    registry: TableRegistry,
    db: Database,
    state: IngestStateStore,
    *,
    on_change: Callable[[], None] | None = None,
) -> dict[str, SourceConnector]:
    """config.sources を回して kind に応じたコネクタを生成して返す。

    ``on_change`` は各コネクタが行を書き込んだ後に呼ぶ(ビューのキャッシュを捨てる等)。
    """
    connectors: dict[str, SourceConnector] = {}
    for name, sdef in config.sources.items():
        cls = _KIND_MAP[sdef.kind]
        connectors[name] = cls(
            name=name,
            sdef=sdef,
            config=config,
            registry=registry,
            db=db,
            state=state,
            on_change=on_change,
        )
    return connectors
