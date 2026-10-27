"""csv/ フォルダを監視し、変更されたファイルを自動取り込みする(フェーズ1・C)。

依存を増やさないため watchdog ではなく mtime とサイズのポーリングを使う。FastAPI の
lifespan から asyncio タスクとして起動・停止する(ループと起動/停止は
:class:`~monitor_app.services.periodic.PeriodicTask` が担う)。

追記モード(``keep=True``)は、CSV ごとに前回どこまで読んだか(バイト位置)を覚えて
おき、変更のたびに続きの完全な行だけを取り込む(:class:`TailState` — PERF-05)。
読み取り位置は DB(``_ingest_state``)に保存し、再起動後もその続きから読む(停止中に
追記された行も取り込む — D3-03)。起動前の取り込みを
:meth:`IngestWatcher.import_existing` で行えば、その読み取り位置を監視がそのまま
引き継ぐ(起動の途中に追記された行も、取りこぼさず二重にもしない)。
置換モードで同じ回に複数の CSV が変わったら、1 トランザクションでまとめて置き換える
(参照し合うテーブルの CSV を揃えて差し替えても外部キーに違反しない)。

取り込みに失敗した CSV(DB のロック待ちの時間切れなど)は、成功するまで間隔を
伸ばしながら取り込み直し、そのあいだ ``/api/health`` に失敗を出し続ける(D4-07)。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import select

from .importer import CsvImporter, TailState
from .periodic import PeriodicTask

if TYPE_CHECKING:
    from .connectors import SourceConnector

logger = logging.getLogger("monitor_app.ingest")

#: 取り込みに失敗した CSV を取り込み直すまでの最大の間隔(秒)。
MAX_RETRY_DELAY = 60.0


@dataclass
class _Failure:
    """取り込みに失敗した CSV(成功するまで間隔を伸ばしながら取り込み直す)。"""

    #: 失敗したときのファイルの (mtime, サイズ)(変われば待たずに取り込み直す)。
    mtime: tuple[int, int] | None
    #: 続けて失敗した回数。
    count: int
    #: 次に取り込み直す時刻(time.monotonic())。
    retry_at: float
    error: BaseException


class IngestWatcher(PeriodicTask):
    name = "ingest watcher"
    tick_label = "poll"
    _log = logger

    def __init__(
        self,
        importer: CsvImporter,
        interval: float,
        keep: bool,
        connectors: dict[str, SourceConnector] | None = None,
        *,
        on_change: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(max(interval, 0.5))
        self.importer = importer
        self.keep = keep
        self.csv_dir: Path = importer.csv_dir
        #: 取り込み済みの CSV の (mtime, サイズ)(取り込みに成功してから記録する — D4-07)。
        self._mtimes: dict[str, tuple[int, int]] = {}
        #: 今回のポーリングで見つけた変更の (mtime, サイズ)(取り込みに成功したら _mtimes へ)。
        self._seen: dict[str, tuple[int, int]] = {}
        #: 取り込みに失敗した CSV(テーブル名 → 失敗の記録)。
        self._failed: dict[str, _Failure] = {}
        # 追記(keep=True)監視の読み取り済みデータ行数(参考値 — #9)。読み取り位置の
        # 本体は _tails(バイト位置)。
        self._row_counts: dict[str, int] = {}
        self._tails: dict[str, TailState] = {}
        #: :meth:`import_existing` が作った読み取り位置(:meth:`setup` が引き継ぐ)。
        self._resumed: set = set()
        self.connectors = connectors
        #: 1 回のポーリングで行が変わったときに 1 度呼ぶ(ビューのキャッシュを捨てる等)。
        self.on_change = on_change

    def _changed_tables(self) -> list[str]:
        """前回の取り込み以降に mtime かサイズが変わった CSV のテーブル名を返す。

        mtime の刻みが粗いファイルシステム(Windows はシステム時計の約 16 ms、FAT は
        2 秒)では、続けて書き込むと mtime が変わらないことがある。追記や書き換えで
        サイズが変われば、mtime が同じでも変更として扱う。

        取り込みに失敗した CSV は、取り込み直す時刻になったら(ファイルが変わったら
        すぐに)もう一度返す。見つけた値は :meth:`_mark_done` で記録する。
        """
        changed = []
        now = time.monotonic()
        for table_name in self.importer.config.tables:
            path = self.csv_dir / f"{table_name}.csv"
            try:
                st = path.stat()
            except OSError:
                self._failed.pop(table_name, None)
                continue
            mtime = (st.st_mtime_ns, st.st_size)
            failure = self._failed.get(table_name)
            if failure is not None:
                if failure.mtime == mtime and now < failure.retry_at:
                    continue  # 失敗した直後: 間隔をあけて取り込み直す
            elif self._mtimes.get(table_name) == mtime:
                continue
            self._seen[table_name] = mtime
            changed.append(table_name)
        return changed

    def _mark_done(self, table_name: str) -> None:
        """変更を取り込んだ(または取り込みが不要な)CSV の (mtime, サイズ) を記録する。"""
        mtime = self._seen.pop(table_name, None)
        if mtime is not None:
            self._mtimes[table_name] = mtime
        self._failed.pop(table_name, None)

    def _mark_failed(self, table_name: str, exc: BaseException) -> _Failure:
        """取り込みに失敗した CSV を、間隔を伸ばしながら取り込み直す対象にする(D4-07)。"""
        previous = self._failed.get(table_name)
        count = previous.count + 1 if previous is not None else 1
        # 1 回目の失敗は次のポーリングで、続けて失敗したら間隔を倍に伸ばす
        delay = (
            0.0
            if count == 1
            else min(self.interval * 2 ** (count - 2), MAX_RETRY_DELAY)
        )
        mtime = self._seen.pop(table_name, None)
        if mtime is None:
            mtime = self._mtimes.get(table_name)
        failure = _Failure(mtime, count, time.monotonic() + delay, exc)
        self._failed[table_name] = failure
        self.note_error(exc)
        return failure

    def _log_failure(self, table_name: str, failure: _Failure) -> None:
        wait = failure.retry_at - time.monotonic()
        when = "次の確認で" if wait < 1 else f"{wait:.0f} 秒後以降に"
        logger.warning(
            "CSV 取り込み失敗 '%s': %s(%d 回目。%sもう一度取り込みます)",
            table_name,
            failure.error,
            failure.count,
            when,
        )

    def _init_row_counts(self) -> None:
        """追記監視の起動時、既存 CSV の末尾を読み取り位置の初期値にする(#9)。

        起動前に取り込み済み(例: ``--import-csv``)の行を、起動後の最初の変更で
        再取り込みしないため。ファイル全体の解析はせず、完全な行の終わりを探すだけ
        (大きな CSV でも起動が遅くならない — PERF-05)。"""
        if not self.keep:
            return
        for table_name in self.importer.config.tables:
            path = self.csv_dir / f"{table_name}.csv"
            if not path.exists() or table_name in self._resumed:
                continue
            try:
                state, resumed = self._start_tail(table_name, path)
            except Exception as exc:  # noqa: BLE001 - 起動を止めない
                logger.warning("row-count 初期化失敗 %s: %s", table_name, exc)
                continue
            self._tails[table_name] = state
            self._row_counts[table_name] = state.records
            if resumed:
                # 前回の続きから読む: 停止中に追記された行を起動時に取り込む(D3-03)
                self._resumed.add(table_name)

    def _start_tail(self, table_name: str, path: Path) -> tuple[TailState, bool]:
        """保存した読み取り位置があればその続きから、無ければファイル末尾から読む。"""
        start = getattr(self.importer, "start_tail", None)
        if start is None:  # 互換: start_tail を持たない取り込み器
            return self.importer.init_tail(table_name, path), False
        return start(table_name, path)

    def _new_tail(self, table_name: str) -> TailState:
        """先頭から読む読み取り位置(保存先のキーつき)。"""
        key = getattr(self.importer, "tail_key", None)
        return TailState(key=key(table_name) if key is not None else None)

    def _import_changed(self, table_name: str) -> bool:
        """変更された 1 つの CSV を取り込む。keep に応じて追記/置換。行が変われば True。"""
        path = self.csv_dir / f"{table_name}.csv"
        if self.keep:
            state = self._tails.get(table_name)
            if state is None:
                state = self._tails[table_name] = self._new_tail(table_name)
            inserted = self.importer.tail_append(table_name, path, state)
            self._row_counts[table_name] = state.records
            return inserted > 0
        result = self.importer.import_table(table_name, path, keep=False)
        return bool(getattr(result, "inserted", 0))

    def _import_together(self, changed: list[str]) -> bool | None:
        """置換の監視で複数の CSV が同時に変わったら、1 トランザクションでまとめて置き換える。

        外部キーで参照し合うテーブルの CSV を揃えて差し替えたとき、1 つずつでは
        PostgreSQL / MySQL で途中の状態が外部キーに違反するため。行が変われば True。
        対象外(追記・1 ファイルだけ)や失敗したときは None を返し、呼び出し側が従来どおり
        1 つずつ取り込む(1 ファイルの失敗でほかを巻き込まない — #10)。
        """
        if self.keep or len(changed) < 2:
            return None
        try:
            results = self.importer.import_tables(changed, keep=False)
        except Exception as exc:  # noqa: BLE001 - 1 つずつの取り込みに切り替える
            logger.info(
                "変更された CSV(%s)をまとめて取り込めないため 1 つずつ取り込みます: %s",
                ", ".join(changed),
                exc,
            )
            return None
        return any(r.inserted for r in results)

    def poll_once(self, *, import_on_change: bool = True) -> list[str]:
        """変更を検出した CSV テーブル+取り込みが発生したソース名のリストを返す。

        1 ファイル・1 ソースの失敗は warning に留め、他を巻き込まない(#10)。"""
        changed = self._changed_tables()
        rows_changed = False
        if changed and import_on_change:
            together = self._import_together(changed)
            if together is not None:
                rows_changed = together
                for table_name in changed:
                    self._mark_done(table_name)
            else:
                for table_name in changed:
                    try:
                        rows_changed = self._import_changed(table_name) or rows_changed
                    except Exception as exc:  # noqa: BLE001
                        # 1 ファイルの失敗で全体を止めない。成功するまで取り込み直す
                        self._log_failure(
                            table_name, self._mark_failed(table_name, exc)
                        )
                    else:
                        self._mark_done(table_name)
            logger.info("auto-imported changed tables: %s", ", ".join(changed))
        else:
            for table_name in changed:
                self._mark_done(table_name)
        # 取り込み直しを待っている CSV があれば、成功するまで失敗として表示する
        for failure in self._failed.values():
            self.note_error(failure.error)

        # 各コネクタを呼び出し、取り込みがあったソース名を結果に加える
        if self.connectors:
            for name, connector in self.connectors.items():
                try:
                    n = connector.poll_once()
                    if n > 0:
                        changed.append(name)
                        rows_changed = True
                        logger.info("source '%s': %d 行取り込み", name, n)
                except Exception as exc:  # noqa: BLE001 - 1 ソースずつ失敗を扱う
                    self.note_error(exc)
                    logger.warning("source '%s': poll_once 失敗: %s", name, exc)

        if rows_changed and self.on_change is not None:
            try:
                self.on_change()
            except Exception:  # 通知の失敗で監視を止めない
                logger.warning(
                    "取り込み後の通知(on_change)に失敗しました", exc_info=True
                )
        return changed

    def import_existing(self) -> dict[str, int | None]:
        """追記の監視を始める前に、CSV の既存の行を読み取り位置つきで取り込む。

        ``serve --append`` が起動前に呼ぶ。行の無いテーブルには CSV の先頭から完全な
        行をすべて取り込み、行のあるテーブルは CSV の末尾を読み取り位置にする(取り込み
        済みとみなす。``--import-csv-if-empty`` と同じ考え方)。:meth:`setup` はこの
        読み取り位置を引き継いですぐに続きを読むため、ここから監視の開始までに追記された
        行も取りこぼさず、二重にも取り込まない(書きかけの最後の行は、改行が届いてから
        取り込む)。

        戻り値はテーブル名 → 取り込んだ行数(行が既にあって取り込まなかったテーブルは
        None)。CSV の無いテーブルは含まない。
        """
        if not self.keep:
            raise ValueError("import_existing() は追記の監視(keep=True)専用です")
        importer = self.importer
        importer.registry.create_all(importer.db)
        counts: dict[str, int | None] = {}
        for table_name in importer.dependency_order():  # 親のテーブルから
            path = self.csv_dir / f"{table_name}.csv"
            if not path.exists():
                continue
            if self._has_rows(table_name):
                # 保存した読み取り位置があればその続き(停止中の追記も取り込む — D3-03)
                state, _resumed = self._start_tail(table_name, path)
                counts[table_name] = None
            else:
                state = self._new_tail(table_name)
                counts[table_name] = importer.tail_append(table_name, path, state)
            self._tails[table_name] = state
            self._row_counts[table_name] = state.records
            self._resumed.add(table_name)
        return counts

    def _has_rows(self, table_name: str) -> bool:
        table = self.importer.registry.get(table_name)
        with self.importer.db.readonly() as conn:
            return (
                conn.execute(select(1).select_from(table).limit(1)).first() is not None
            )

    def _catch_up(self) -> None:
        """:meth:`import_existing` の読み取り位置から、監視の開始までに追記された行を取り込む。

        :meth:`setup` が mtime を記録した後に読むため、読み終えた後の追記は次の
        ポーリングが拾う(読み取り位置は 1 つなので、同じ行を二重には取り込まない)。
        """
        resumed, self._resumed = self._resumed, set()
        rows_changed = False
        for table_name in self.importer.dependency_order():
            if table_name not in resumed:
                continue
            try:
                rows_changed = self._import_changed(table_name) or rows_changed
            except Exception as exc:  # noqa: BLE001 - 1 ファイルの失敗で全体を止めない
                # 次のポーリングから取り込み直す(D4-07)
                self._log_failure(table_name, self._mark_failed(table_name, exc))
        if rows_changed and self.on_change is not None:
            try:
                self.on_change()
            except Exception:  # 通知の失敗で監視を止めない
                logger.warning(
                    "取り込み後の通知(on_change)に失敗しました", exc_info=True
                )

    def setup(self) -> None:
        """起動時の初期状態を記録する(既存ファイルで即発火させない)。

        PeriodicTask がスレッドで 1 回だけ実行するため、追記監視の読み取り位置の
        初期化(大きな CSV の走査)で起動直後のイベントループを止めない。
        :meth:`import_existing` で取り込んだ CSV は、その読み取り位置から続きを読む。"""
        self.importer.registry.create_all(self.importer.db)
        for table_name in self._changed_tables():
            self._mark_done(table_name)
        self._init_row_counts()
        self._catch_up()
        if self._noted_error is not None:
            self.record_result()  # 引き継いだ CSV の取り込みの失敗を /api/health に出す
        self._warn_replacing_forms()

    def _warn_replacing_forms(self) -> None:
        """置換の監視は入力フォームのあるテーブルの入力行も消すため、起動時に 1 度知らせる
        (DX-01)。"""
        if self.keep:
            return
        tables = getattr(getattr(self.importer, "config", None), "tables", {})
        forms = [name for name, tdef in tables.items() if tdef.form is not None]
        if forms:
            logger.warning(
                "フォルダ監視(MONITOR_INGEST_MODE=replace)は、CSV が変わるたびに"
                "テーブルを CSV の内容で置き換えます。入力フォームのある %s では、"
                "フォーム・画面・API から入力した行も消えます(残すには"
                " MONITOR_INGEST_MODE=append を使ってください)",
                "、".join(forms),
            )

    def tick(self) -> None:
        # 1 回の失敗はループ側でログに残して継続する(#10)。
        self.poll_once()
