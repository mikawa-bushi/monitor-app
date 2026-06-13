"""csv/ フォルダを監視し、変更されたファイルを自動取り込みする(フェーズ1・C)。

依存を増やさないため watchdog ではなく mtime ポーリングを使う。FastAPI の
lifespan から asyncio タスクとして起動・停止する。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional

from .importer import CsvImporter

if TYPE_CHECKING:
    from .connectors import SourceConnector

logger = logging.getLogger("monitor_app.ingest")


class IngestWatcher:
    def __init__(
        self,
        importer: CsvImporter,
        interval: float,
        keep: bool,
        connectors: Optional[Dict[str, "SourceConnector"]] = None,
    ) -> None:
        self.importer = importer
        self.interval = max(interval, 0.5)
        self.keep = keep
        self.csv_dir: Path = importer.csv_dir
        self._mtimes: Dict[str, float] = {}
        # 追記(keep=True)監視で「どこまで取り込んだか」を表す有効行数カーソル(#9)。
        self._row_counts: Dict[str, int] = {}
        self._task: asyncio.Task | None = None
        self.connectors = connectors

    def _changed_tables(self) -> list[str]:
        """前回チェック以降に mtime が変わった CSV のテーブル名を返す。"""
        changed = []
        for table_name in self.importer.config.tables:
            path = self.csv_dir / f"{table_name}.csv"
            if not path.exists():
                continue
            mtime = path.stat().st_mtime
            if self._mtimes.get(table_name) != mtime:
                self._mtimes[table_name] = mtime
                changed.append(table_name)
        return changed

    def _init_row_counts(self) -> None:
        """追記監視の起動時、既存 CSV の現在行数をカーソル初期値にする(#9)。

        起動前に取り込み済み(例: ``--import-csv``)の行を、起動後の最初の変更で
        再取り込みしないため。"""
        if not self.keep:
            return
        for table_name in self.importer.config.tables:
            path = self.csv_dir / f"{table_name}.csv"
            if not path.exists():
                continue
            try:
                self._row_counts[table_name] = self.importer.count_rows(
                    table_name, path
                )
            except Exception as exc:  # noqa: BLE001 - 起動を止めない
                logger.warning("row-count 初期化失敗 %s: %s", table_name, exc)

    def _import_changed(self, table_name: str) -> None:
        """変更された 1 つの CSV を取り込む。keep に応じて追記/置換。"""
        path = self.csv_dir / f"{table_name}.csv"
        if self.keep:
            start = self._row_counts.get(table_name, 0)
            _inserted, total = self.importer.append_since(
                table_name, path, start_row=start
            )
            self._row_counts[table_name] = total
        else:
            self.importer.import_table(table_name, path, keep=False)

    def poll_once(self, *, import_on_change: bool = True) -> List[str]:
        """変更を検出した CSV テーブル+取り込みが発生したソース名のリストを返す。

        1 ファイル・1 ソースの失敗は warning に留め、他を巻き込まない(#10)。"""
        changed = self._changed_tables()
        if changed and import_on_change:
            for table_name in changed:
                try:
                    self._import_changed(table_name)
                except (
                    Exception
                ) as exc:  # noqa: BLE001 - 1 ファイルの失敗で全体を止めない
                    logger.warning("CSV 取り込み失敗 '%s': %s", table_name, exc)
            logger.info("auto-imported changed tables: %s", ", ".join(changed))

        # 各コネクタを呼び出し、取り込みがあったソース名を結果に加える
        if self.connectors:
            for name, connector in self.connectors.items():
                try:
                    n = connector.poll_once()
                    if n > 0:
                        changed.append(name)
                        logger.info("source '%s': %d 行取り込み", name, n)
                except Exception as exc:
                    logger.warning("source '%s': poll_once 失敗: %s", name, exc)

        return changed

    async def _run(self) -> None:
        # 起動時の初期状態を記録(既存ファイルで即発火させない)。
        self.importer.registry.create_all(self.importer.db)
        self._changed_tables()
        self._init_row_counts()
        logger.info("ingest watcher started (interval=%.1fs)", self.interval)
        try:
            while True:
                await asyncio.sleep(self.interval)
                try:
                    await asyncio.to_thread(self.poll_once)
                except Exception:  # noqa: BLE001 - ループを恒久停止させない(#10)
                    logger.exception("ingest watcher poll failed; continuing")
        except asyncio.CancelledError:
            logger.info("ingest watcher stopped")
            raise

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
