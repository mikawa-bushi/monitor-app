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
from typing import Dict, List

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
    ) -> None:
        self.config = config
        self.registry = registry
        self.db = db
        self.csv_dir = Path(csv_dir)

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
            report.results.append(self._import_one(table_name, csv_path, keep=keep))
        return report

    def _import_one(
        self, table_name: str, csv_path: Path, *, keep: bool
    ) -> TableImportResult:
        tdef = self.config.tables[table_name]
        table = self.registry.get(table_name)
        allowed = set(tdef.column_names)
        result = TableImportResult(table=table_name)

        rows: List[Dict[str, object]] = []
        with csv_path.open(newline="", encoding="utf-8-sig") as fh:
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
                        result.skipped += 1
                        result.warnings.append(f"{csv_path.name} {lineno}行目: {exc}")
                        failed = True
                        break
                if not failed and record:
                    rows.append(record)

        with self.db.connect() as conn:
            if not keep:
                conn.execute(delete(table))
            if rows:
                conn.execute(insert(table), rows)
        result.inserted = len(rows)
        logger.info(
            "imported %s: %d rows (skipped %d)",
            table_name,
            result.inserted,
            result.skipped,
        )
        return result
