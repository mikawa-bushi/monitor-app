"""csv/ フォルダを監視し、変更されたファイルを自動取り込みする(フェーズ1・C)。

依存を増やさないため watchdog ではなく mtime ポーリングを使う。FastAPI の
lifespan から asyncio タスクとして起動・停止する。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Dict

from .importer import CsvImporter

logger = logging.getLogger("monitor_app.ingest")


class IngestWatcher:
    def __init__(self, importer: CsvImporter, interval: float, keep: bool) -> None:
        self.importer = importer
        self.interval = max(interval, 0.5)
        self.keep = keep
        self.csv_dir: Path = importer.csv_dir
        self._mtimes: Dict[str, float] = {}
        self._task: asyncio.Task | None = None

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

    def poll_once(self, *, import_on_change: bool = True) -> list[str]:
        """1 回だけ変更を検出する。テスト用に同期で呼べる。"""
        changed = self._changed_tables()
        if changed and import_on_change:
            for table_name in changed:
                path = self.csv_dir / f"{table_name}.csv"
                self.importer._import_one(table_name, path, keep=self.keep)
            logger.info("auto-imported changed tables: %s", ", ".join(changed))
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
