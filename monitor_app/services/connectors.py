"""外部データソースコネクタ (sqlite / jsonl / json)。

各コネクタは ``poll_once()`` を呼ばれるたびに増分または全件を monitor-app 側 DB に
取り込み、取り込んだ行数を返す。監視ループから繰り返し呼ばれることを前提にしており、
ファイル未存在・DB ロックなどのエラーは warning ログに留めて 0 を返す(ループを殺さない)。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, Iterator, List

from sqlalchemy import delete, insert

from ..db.engine import Database
from ..db.ingest_state import IngestStateStore
from ..db.registry import TableRegistry
from ..settings.declarative import MonitorConfig, SourceDef
from .coercion import CoercionError, coerce

logger = logging.getLogger("monitor_app.connectors")


# ---------------------------------------------------------------------------
# 共通ユーティリティ
# ---------------------------------------------------------------------------


def _dot_get(obj: Any, key: str) -> Any:
    """ドット記法キーで obj を走査する。数値トークンはリストインデックス。"""
    parts = key.split(".")
    cur = obj
    for part in parts:
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                raise KeyError(key)
        elif isinstance(cur, dict):
            if part not in cur:
                raise KeyError(key)
            cur = cur[part]
        else:
            raise KeyError(key)
    return cur


def _flatten(value: Any) -> Any:
    """list/dict の平坦化: list → 空白区切り文字列、dict → JSON 文字列。"""
    if isinstance(value, list):
        return " ".join(str(v) for v in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return value


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
    ) -> None:
        self.name = name
        self.sdef = sdef
        self.config = config
        self.registry = registry
        self.db = db
        self.state = state
        self.path = Path(sdef.path).expanduser()

    @abstractmethod
    def poll_once(self) -> int:
        """データを取り込み、挿入した行数を返す。"""

    # ------------------------------------------------------------------
    # 共通: raw レコード列挙 → 変換 → DB 挿入
    # ------------------------------------------------------------------

    def _transform_and_insert(
        self, raw_records: Iterator[Dict[str, Any]], *, replace: bool
    ) -> int:
        """raw レコードを変換して DB に挿入する。

        replace=True なら挿入前に既存行を全削除する。
        変換失敗行はスキップして warning。
        未知列は破棄。
        """
        tdef = self.config.tables[self.sdef.table]
        table = self.registry.get(self.sdef.table)
        allowed = set(tdef.column_names)
        mapping = self.sdef.mapping  # {dest_col: src_key_dotted}

        rows: List[Dict[str, Any]] = []
        for raw in raw_records:
            record: Dict[str, Any] = {}
            failed = False

            if mapping:
                # mapping あり: dest_col → src_key でドット記法走査
                for dest_col, src_key in mapping.items():
                    if dest_col not in allowed:
                        continue
                    try:
                        raw_val = _dot_get(raw, src_key)
                    except KeyError:
                        raw_val = None
                    raw_val = _flatten(raw_val)
                    try:
                        record[dest_col] = coerce(raw_val, tdef.column_type(dest_col))
                    except CoercionError as exc:
                        logger.warning(
                            "source '%s': 型変換失敗(列 %s): %s — 行スキップ",
                            self.name,
                            dest_col,
                            exc,
                        )
                        failed = True
                        break
            else:
                # mapping なし: ソースキーをそのまま列名に使う
                for src_key, raw_val in raw.items():
                    if src_key not in allowed:
                        continue
                    raw_val = _flatten(raw_val)
                    try:
                        record[src_key] = coerce(raw_val, tdef.column_type(src_key))
                    except CoercionError as exc:
                        logger.warning(
                            "source '%s': 型変換失敗(列 %s): %s — 行スキップ",
                            self.name,
                            src_key,
                            exc,
                        )
                        failed = True
                        break

            if not failed and record:
                rows.append(record)

        with self.db.connect() as conn:
            if replace and not rows:
                # 有効行 0 件での置換は既存データの全消去になるため中止する(#16)。
                logger.warning(
                    "source '%s': 有効行 0 件のため置換を中止(既存データを保持)",
                    self.name,
                )
            else:
                if replace:
                    conn.execute(delete(table))
                if rows:
                    conn.execute(insert(table), rows)

        logger.info(
            "source '%s': %d 行取り込み (replace=%s)",
            self.name,
            len(rows),
            replace,
        )
        return len(rows)


# ---------------------------------------------------------------------------
# SqliteSourceConnector
# ---------------------------------------------------------------------------


class SqliteSourceConnector(SourceConnector):
    """sqlite ファイルからの増分/全件取り込みコネクタ。"""

    def poll_once(self) -> int:
        if not self.path.exists():
            return 0

        wc = self.sdef.watermark_column

        # watermark なし: mtime:size を cursor に使う
        if wc is None and self.sdef.query is None:
            stat = self.path.stat()
            cursor_val = f"{stat.st_mtime_ns}:{stat.st_size}"
            saved = self.state.get(self.name)
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
            return self._poll_sqlite(conn_src)
        finally:
            conn_src.close()

    def _poll_sqlite(self, conn_src: sqlite3.Connection) -> int:
        wc = self.sdef.watermark_column
        st = self.sdef.source_table

        # SQL 決定
        if self.sdef.query:
            sql = self.sdef.query
            params: Dict[str, Any] = {"wm": self.state.get(self.name)}
        elif wc:
            sql = f"SELECT * FROM {st} WHERE (:wm IS NULL OR {wc} > :wm) ORDER BY {wc}"
            params = {"wm": self.state.get(self.name)}
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

        raw_dicts = (dict(r) for r in rows_raw)

        replace = self.sdef.mode == "replace" or (wc is None and not self.sdef.query)
        inserted = self._transform_and_insert(raw_dicts, replace=replace)

        if wc:
            # watermark 最大値を保存
            max_wm = str(max(r[wc] for r in rows_raw if r[wc] is not None))
            self.state.set(self.name, max_wm)
        elif not self.sdef.query:
            # mtime:size cursor を保存
            stat = self.path.stat()
            self.state.set(self.name, f"{stat.st_mtime_ns}:{stat.st_size}")

        return inserted


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

        raw_records: List[Dict[str, Any]] = []
        new_offset = offset

        try:
            with self.path.open("rb") as fh:
                fh.seek(offset)
                buf = fh.read()

            # 末尾に改行がなければ最後の未完了行を消費しない
            lines = buf.split(b"\n")
            if buf.endswith(b"\n"):
                # 最後が空要素になるので除去
                lines = lines[:-1]
                consume_all = True
            else:
                # 最後の行は未完了: 除外して持ち越し
                lines = lines[:-1]
                consume_all = False  # noqa: F841 (未使用変数だが意図明示のため残す)

            pos = offset
            for line in lines:
                line_len = len(line) + 1  # +1 for \n
                line_stripped = line.strip()
                if not line_stripped:
                    pos += line_len
                    continue
                try:
                    obj = json.loads(line_stripped)
                    if isinstance(obj, dict):
                        raw_records.append(obj)
                    else:
                        logger.warning(
                            "source '%s': JSONL 行が object でない — スキップ",
                            self.name,
                        )
                except json.JSONDecodeError as exc:
                    logger.warning(
                        "source '%s': JSON パース失敗: %s — 行スキップ", self.name, exc
                    )
                pos += line_len

            new_offset = pos

        except OSError as exc:
            logger.warning("source '%s': 読み取り失敗: %s", self.name, exc)
            return 0

        if not raw_records:
            if new_offset > offset:
                self.state.set(self.name, str(new_offset))
            return 0

        inserted = self._transform_and_insert(iter(raw_records), replace=False)
        self.state.set(self.name, str(new_offset))
        return inserted


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
        if cursor_val == self.state.get(self.name):
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

        replace = self.sdef.mode == "replace"
        inserted = self._transform_and_insert(iter(data), replace=replace)
        self.state.set(self.name, cursor_val)
        return inserted


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
) -> Dict[str, SourceConnector]:
    """config.sources を回して kind に応じたコネクタを生成して返す。"""
    connectors: Dict[str, SourceConnector] = {}
    for name, sdef in config.sources.items():
        cls = _KIND_MAP[sdef.kind]
        connectors[name] = cls(
            name=name,
            sdef=sdef,
            config=config,
            registry=registry,
            db=db,
            state=state,
        )
    return connectors
