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

    def poll_once(self, *, import_on_change: bool = True) -> List[str]:
        """変更を検出した CSV テーブル+取り込みが発生したソース名のリストを返す。"""
        changed = self._changed_tables()
        if changed and import_on_change:
            for table_name in changed:
                path = self.csv_dir / f"{table_name}.csv"
                self.importer._import_one(table_name, path, keep=self.keep)
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
        logger.info("ingest watcher started (interval=%.1fs)", self.interval)
        try:
            while True:
                await asyncio.sleep(self.interval)
                await asyncio.to_thread(self.poll_once)
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
