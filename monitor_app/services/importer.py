"""CSV → DB インポート。標準ライブラリ ``csv`` を使い、スキーマの真実を
``config.py`` 側(:class:`TableDef`)に一本化する。

pandas の ``to_sql(if_exists="replace")`` のようにデータ側の推論でスキーマを
作り直さないため、宣言した型・外部キーが取り込みで失われない。
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Tuple

from sqlalchemy import delete, insert

from ..settings.declarative import MonitorConfig
from ..db.engine import Database
from ..db.registry import TableRegistry
from .coercion import CoercionError, coerce

logger = logging.getLogger("monitor_app.importer")


@dataclass
class TableImportResult:
    table: str
    inserted: int = 0
    skipped: int = 0
    warnings: List[str] = field(default_factory=list)


@dataclass
class ImportReport:
    results: List[TableImportResult] = field(default_factory=list)

    @property
    def total_inserted(self) -> int:
        return sum(r.inserted for r in self.results)

    @property
    def total_skipped(self) -> int:
        return sum(r.skipped for r in self.results)


class CsvImporter:
    def __init__(
        self,
        config: MonitorConfig,
        registry: TableRegistry,
        db: Database,
        csv_dir: Path,
        encoding: str = "utf-8-sig",
    ) -> None:
        self.config = config
        self.registry = registry
        self.db = db
        self.csv_dir = Path(csv_dir)
        self.encoding = encoding

    def import_all(self, *, keep: bool = False) -> ImportReport:
        """``csv_dir`` 内の各 CSV を、対応するテーブルへ取り込む。

        ``keep=False`` なら取り込み前に既存行を全削除(置換)。
        ``keep=True`` なら追記。CSV のファイル名(拡張子なし)がテーブル名。
        """
        self.registry.create_all(self.db)
        report = ImportReport()
        for table_name in self.config.tables:
            csv_path = self.csv_dir / f"{table_name}.csv"
            if not csv_path.exists():
                continue
            report.results.append(self.import_table(table_name, csv_path, keep=keep))
        return report

    def read_rows(
        self, table_name: str, csv_path: Path
    ) -> Tuple[List[Dict[str, object]], int, List[str]]:
        """CSV を読み、設定列に絞って論理型へ変換した行リストを返す。

        戻り値は ``(rows, skipped, warnings)``。未知列は破棄。型変換に失敗した
        行はスキップして警告に積む。インポート/増分追記の双方が使う。
        """
        tdef = self.config.tables[table_name]
        allowed = set(tdef.column_names)
        rows: List[Dict[str, object]] = []
        skipped = 0
        warnings: List[str] = []
        with csv_path.open(newline="", encoding=self.encoding) as fh:
            reader = csv.DictReader(fh)
            for lineno, raw in enumerate(reader, start=2):
                record: Dict[str, object] = {}
                failed = False
                for key, value in raw.items():
                    if key not in allowed:
                        continue
                    try:
                        record[key] = coerce(value, tdef.column_type(key))
                    except CoercionError as exc:
                        skipped += 1
                        warnings.append(f"{csv_path.name} {lineno}行目: {exc}")
                        failed = True
                        break
                if not failed and record:
                    rows.append(record)
        return rows, skipped, warnings

    def import_table(
        self, table_name: str, csv_path: Path, *, keep: bool
    ) -> TableImportResult:
        """1 テーブル分の CSV を取り込む(置換 or 追記)。

        ``keep=False``(置換)でも、有効行が 0 件のときは既存行を削除しない
        (空・破損 CSV を誤って置いたときの全消去を防ぐ — #16)。
        """
        table = self.registry.get(table_name)
        rows, skipped, warnings = self.read_rows(table_name, csv_path)
        result = TableImportResult(table=table_name, skipped=skipped, warnings=warnings)

        with self.db.connect() as conn:
            if keep:
                if rows:
                    conn.execute(insert(table), rows)
            elif rows:
                conn.execute(delete(table))
                conn.execute(insert(table), rows)
            else:
                msg = f"{csv_path.name}: 有効行 0 件のため置換を中止(既存データを保持)"
                logger.warning(msg)
                result.warnings.append(msg)
                return result

        result.inserted = len(rows)
        logger.info(
            "imported %s: %d rows (skipped %d)",
            table_name,
            result.inserted,
            result.skipped,
        )
        return result

    def append_since(
        self, table_name: str, csv_path: Path, *, start_row: int
    ) -> Tuple[int, int]:
        """追記監視用: 既読 ``start_row`` 行を超えた分のみ挿入する(#9)。

        戻り値は ``(inserted, total_valid_rows)``。``total`` を呼び出し側が
        カーソルとして保持し、次回 ``start_row`` に渡すことで、CSV が変更される
        たびに全行を重複取り込みする問題を防ぐ。ファイル縮小・差し替え時
        (``start_row > total``)は先頭から取り込み直す。
        """
        table = self.registry.get(table_name)
        rows, _skipped, warnings = self.read_rows(table_name, csv_path)
        for w in warnings:
            logger.warning("%s", w)
        total = len(rows)
        if start_row > total:  # 縮小/差し替え → 先頭から
            start_row = 0
        new_rows = rows[start_row:]
        if new_rows:
            with self.db.connect() as conn:
                conn.execute(insert(table), new_rows)
            logger.info(
                "appended %s: %d new rows (total %d)", table_name, len(new_rows), total
            )
        return len(new_rows), total

    def count_rows(self, table_name: str, csv_path: Path) -> int:
        """有効行数のみ返す(追記カーソルの初期化用)。"""
        rows, _skipped, _warnings = self.read_rows(table_name, csv_path)
        return len(rows)
