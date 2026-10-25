"""SQLite データベースの定期バックアップ(``MONITOR_BACKUP_DIR`` を設定したとき)。

前回のバックアップ(保存先にある最新のファイルの日時)から ``backup_interval_hours``
が過ぎていたらバックアップする、という判定を数分おきに行う。単純に「起動から
24 時間ごと」にすると、毎晩電源を落とす現場の PC では一度もバックアップされない
ため。保存先と世代数の扱いは :func:`~monitor_app.db.backup.backup_database` を参照。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from ..db.backup import (
    backup_database,
    database_owner,
    latest_backup_time,
    remove_stale_temp_files,
)
from ..db.engine import Database
from .periodic import PeriodicTask

logger = logging.getLogger("monitor_app.backup")

#: バックアップが必要かを確かめる最大の間隔(秒)。
CHECK_INTERVAL = 600.0
#: 今より後の日時のバックアップを「時計が進んでいたときのもの」とみなす余裕。
#: 夏時間の終わり(現地時刻が 1 時間戻る)では、直前のバックアップが最大 1 時間先の
#: 日時になる。
FUTURE_TOLERANCE = timedelta(hours=1)


class BackupTask(PeriodicTask):
    """前回から ``interval_hours`` 経っていればバックアップする定期タスク。"""

    name = "backup"
    tick_label = "backup"
    _log = logger

    def __init__(
        self,
        db: Database,
        dest_dir: Path,
        *,
        interval_hours: float,
        keep: int = 7,
        check_interval: float = CHECK_INTERVAL,
        clock: Callable[[], datetime] = datetime.now,
    ) -> None:
        self.period = timedelta(hours=interval_hours)
        super().__init__(min(self.period.total_seconds(), check_interval))
        self.db = db
        self.dest_dir = Path(dest_dir)
        self.keep = keep
        self._clock = clock
        self._future_warned = False

    def due(self) -> bool:
        """前回のバックアップから ``interval_hours`` 以上経ったか(初回は常に True)。

        今より先の日時のバックアップ(時計が進んでいたときに作られたもの)は前回と
        みなさない。そうしないと、時計を直した後はその日時まで一度もバックアップ
        されない(D3-11)。同じフォルダにある別のデータベースのバックアップも前回と
        みなさない(D3-16)。
        """
        path = self.db.sqlite_path
        if path is None:
            return False
        now = self._clock()
        owner = database_owner(path)
        last = latest_backup_time(
            self.dest_dir, path.stem, owner=owner, not_after=now + FUTURE_TOLERANCE
        )
        if not self._future_warned:
            newest = latest_backup_time(self.dest_dir, path.stem, owner=owner)
            if newest is not None and newest > now + FUTURE_TOLERANCE:
                self._future_warned = True
                logger.warning(
                    "%s に今(%s)より後の日時(%s)のバックアップがあります。時計が"
                    "進んでいたときに作られたものとみなし、前回のバックアップには数え"
                    "ません(PC の時計を確認してください)",
                    self.dest_dir,
                    now.strftime("%Y-%m-%d %H:%M"),
                    newest.strftime("%Y-%m-%d %H:%M"),
                )
        return last is None or now - last >= self.period

    def setup(self) -> None:
        # 前回、強制終了で書きかけのまま残った一時ファイルを消す(D4-02)。
        path = self.db.sqlite_path
        if path is not None:
            try:
                remove_stale_temp_files(self.dest_dir, path.stem)
            except OSError as exc:
                self._log.warning("一時ファイルを確認できません: %s", exc)
        # 起動直後にも 1 回確かめる(前回から間隔が空いていればすぐに取る)。ここで
        # 失敗しても定期実行は続ける(次の確認で再試行する)。結果は定期実行と同じく
        # last_tick / last_error に残す(/api/health が次の確認を待たずに表示する)。
        try:
            self.tick()
        except Exception as exc:  # noqa: BLE001 - 起動を止めない
            self.record_result(exc)
            self._log.exception("backup at startup failed; will retry")
        else:
            self.record_result()

    def tick(self) -> None:
        if self.due():
            backup_database(self.db, self.dest_dir, keep=self.keep, now=self._clock())


def build_backup_task(db: Database, settings) -> BackupTask | None:
    """設定に応じて :class:`BackupTask` を作る(対象外なら None)。

    ``backup_dir`` が未設定か ``backup_interval_hours`` が 0 以下なら作らない。
    SQLite のファイル以外の DB では警告して作らない(pg_dump / mysqldump を案内する)。
    """
    if settings.backup_dir is None or settings.backup_interval_hours <= 0:
        return None
    if db.sqlite_path is None:
        logger.warning(
            "MONITOR_BACKUP_DIR による定期バックアップは SQLite のファイルだけに対応して"
            "います(%s)。サーバー DB は pg_dump / mysqldump でバックアップしてください",
            db.describe(),
        )
        return None
    logger.info(
        "定期バックアップ: %s へ %.1f 時間ごと(%d 世代)",
        settings.backup_dir,
        settings.backup_interval_hours,
        settings.backup_keep,
    )
    return BackupTask(
        db,
        Path(settings.backup_dir),
        interval_hours=settings.backup_interval_hours,
        keep=settings.backup_keep,
    )
