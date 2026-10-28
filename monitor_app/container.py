"""サービスの組み立て(依存の配線)を 1 か所にまとめる。

:func:`build_services` が DB・テーブル定義・各サービス・バックグラウンドタスクを
生成し、:class:`Services` が lifespan での起動/停止と DB の後片付けを受け持つ。
``main.create_app`` は HTTP 層(ルーター・ミドルウェア・テンプレート)の配線だけを
行うため、テストや CLI からも HTTP を介さずに同じ配線を使える。

使う設定は、config.py の設定に画面で変えた差分(DB の ``_settings`` 表)を重ねたもの
(:class:`~monitor_app.services.settings_service.SettingsEditor`)。画面で保存したら
:meth:`Services.sync_config` で、サーバーを止めずに差し替える。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.pool import StaticPool

from .db.change_token import change_token_for
from .db.engine import Database
from .db.ingest_state import IngestStateStore
from .db.registry import TableRegistry
from .db.repository import TableRepository
from .db.schema import inspect_schema
from .db.settings_store import SettingsStore
from .services.alert_scheduler import AlertScheduler
from .services.alert_service import AlertEngine
from .services.audit_service import AuditService
from .services.backup_task import build_backup_task
from .services.connectors import SourceConnector, build_connectors
from .services.crud_service import CrudService
from .services.importer import CsvImporter
from .services.ingest_watcher import IngestWatcher
from .services.kpi_service import KpiService
from .services.periodic import PeriodicTask
from .services.retention import build_retention_task
from .services.settings_service import SettingsEditor
from .services.view_cache import ViewCache
from .services.view_service import ViewService
from .settings.declarative import MonitorConfig
from .settings.overrides import config_revision
from .settings.runtime import AppSettings

logger = logging.getLogger("monitor_app.db")

#: lifespan の終了処理(バックグラウンドタスクの停止とアラート通知の送り切り)に使う
#: 最大秒数。uvicorn の停止の猶予(5 秒)と合わせても ``docker stop`` の既定の猶予
#: (10 秒)に収まるようにする。
SHUTDOWN_TIMEOUT = 4.5
#: 送り切れなくても、アラート通知の送信に最低限あてる秒数。
_MIN_NOTIFY_DRAIN = 1.0


@dataclass(eq=False)
class Services:
    """アプリが使うサービス一式と、lifespan で起動/停止するバックグラウンドタスク。

    稼働中の資源(DB エンジン・タスク)を持つため、比較は同一性で行う(``eq=False``)。
    """

    settings: AppSettings
    config: MonitorConfig
    db: Database
    registry: TableRegistry
    repository: TableRepository
    #: ソース名 → コネクタ。``config.sources`` が空なら None。
    connectors: dict[str, SourceConnector] | None
    view_cache: ViewCache
    view_service: ViewService
    alert_engine: AlertEngine
    audit_service: AuditService
    crud_service: CrudService
    kpi_service: KpiService
    #: 画面での設定の編集(config.py の設定と、画面で変えた差分)。
    settings_editor: SettingsEditor
    #: 起動順に並んだバックグラウンドタスク(フォルダ監視・アラート評価・
    #: 保持期間の削除・定期バックアップ)。
    background_tasks: list[PeriodicTask] = field(default_factory=list)
    #: 使う設定が変わったときに呼ぶ関数(HTTP の層がアプリのタイトルなどを合わせる)。
    config_listeners: list[Callable[[MonitorConfig], None]] = field(
        default_factory=list
    )
    #: 使う設定の版。応答の ``X-Monitor-Config`` ヘッダで画面に知らせ、変わったら
    #: 開いている画面が読み直す。
    config_revision: str = field(init=False)
    #: バックグラウンドタスクを起動済みか(lifespan の開始から終了まで)。
    _started: bool = field(default=False, repr=False)

    def __post_init__(self) -> None:
        self.config_revision = config_revision(self.config)

    async def start(self) -> None:
        """バックグラウンドタスクを起動する(lifespan の開始時に呼ぶ)。"""
        self._started = True
        for task in self.background_tasks:
            task.start()

    def sync_config(self) -> None:
        """画面で保存した設定(:attr:`settings_editor` の設定)を、サーバーを止めずに反映する。

        イベントループの上で呼ぶ(アラートの評価を始めるときに asyncio のタスクを作るため)。
        同じ設定なら何もしない。保存の結果ではなく SettingsEditor の今の設定を使うので、
        続けて 2 回保存されて反映の順が入れ替わっても、最後に保存した設定になる。
        """
        config = self.settings_editor.config
        if config is not self.config:
            self.apply_config(config)

    def apply_config(self, config: MonitorConfig) -> None:
        """設定を差し替える(:meth:`sync_config` から呼ぶ)。

        テーブル・ソースの定義は画面で変えられないため、DB・取り込み・保持期間の削除・
        CRUD はそのまま。ビュー・KPI・アラートの設定を差し替え、ビューか KPI が変わったら
        ビューのキャッシュを捨てる(計算中だった古い定義の結果はキャッシュに残らない)。
        アラートが初めて定義されたら評価のスケジューラを足す(起動済みなら、すぐ動かす)。
        """
        old, self.config = self.config, config
        self.view_service.config = config
        self.kpi_service.config = config
        self.alert_engine.reconfigure(config)
        scheduler = next(
            (t for t in self.background_tasks if isinstance(t, AlertScheduler)), None
        )
        if scheduler is not None:
            scheduler.set_interval(alert_interval(config))
        elif config.alerts:
            scheduler = AlertScheduler(
                self.alert_engine, self.view_service, alert_interval(config)
            )
            self.background_tasks.append(scheduler)
            if self._started:
                scheduler.start()
        if config.views != old.views or config.kpis != old.kpis:
            self.view_cache.clear()
        self.config_revision = config_revision(config)
        for listener in self.config_listeners:
            listener(config)

    def csv_importer(self, folder: Path) -> CsvImporter:
        """``folder`` の CSV を取り込む CsvImporter(画面からのアップロードで使う)。"""
        return build_csv_importer(
            self.config, self.registry, self.db, self.settings, folder, self.view_cache
        )

    async def stop(self, timeout: float = SHUTDOWN_TIMEOUT) -> None:
        """バックグラウンドタスクを止め、アラート通知を送り切る(何度呼んでもよい)。

        停止の指示は起動と逆順に出し、実行中の処理(取り込み・削除・評価)の終了は全タスク
        まとめて待つ。待つのは全体で最大 ``timeout`` 秒(各タスクの ``stop_timeout`` も
        超えない)で、残りの時間でキューに残ったアラート通知を送り切る。
        """
        self._started = False
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        await asyncio.gather(
            *(
                task.stop(min(getattr(task, "stop_timeout", timeout), timeout))
                for task in reversed(self.background_tasks)
            )
        )
        remaining = max(deadline - loop.time(), _MIN_NOTIFY_DRAIN)
        # 待ちはスレッドで行い、ループを止めない。
        await asyncio.to_thread(self.alert_engine.close, remaining)

    def close(self) -> None:
        """DB エンジンのコネクションプールを破棄する(lifespan の終了時に呼ぶ)。

        インメモリ SQLite(StaticPool の単一コネクションがデータベースそのもの)は
        破棄しない。破棄すると表ごと消え、lifespan の終了後や 2 回目の lifespan で
        アプリの DB が使えなくなるため(従来どおりアプリと同じ寿命にする)。
        """
        self.view_cache.close()  # 変更検知用の SQLite コネクションを閉じる
        if isinstance(self.db.engine.pool, StaticPool):
            return
        self.db.dispose()


def build_source_connectors(
    config: MonitorConfig,
    registry: TableRegistry,
    db: Database,
    *,
    on_change: Callable[[], None] | None = None,
) -> dict[str, SourceConnector] | None:
    """``config.sources`` があれば取り込みカーソル表を用意してコネクタを組み立てる。

    ソースが 1 つも無ければ None を返す(``_ingest_state`` 表も作らない)。
    ``on_change`` は各コネクタが行を書き込んだ後に呼ぶ。"""
    if not config.sources:
        return None
    state = IngestStateStore(db)
    state.create()
    return build_connectors(config, registry, db, state, on_change=on_change)


def build_csv_importer(
    config: MonitorConfig,
    registry: TableRegistry,
    db: Database,
    settings: AppSettings,
    folder: Path,
    view_cache: ViewCache,
) -> CsvImporter:
    """CSV の取り込み(フォルダ監視・画面からのアップロード)。

    文字コードは .env の設定に従う。取り込みで行が変わったらビューのキャッシュを捨て、
    PostgreSQL / MySQL でも新しいデータをすぐ表示する。
    """
    return CsvImporter(
        config,
        registry,
        db,
        folder,
        encoding=settings.csv_encoding,
        encoding_fallback=settings.csv_encoding_fallback,
        on_change=view_cache.clear,
    )


def alert_interval(config: MonitorConfig) -> float:
    """アラートをバックグラウンドで評価する間隔(秒)。画面の更新間隔と同じ(最短 1 秒)。"""
    return max(config.refresh_interval_ms, 1000) / 1000.0


def _warn_schema_drift(db: Database, registry: TableRegistry) -> None:
    """config.py と DB の表定義のずれを 1 件ずつログに出す(起動は止めない)。"""
    try:
        inspect_schema(db, registry).log(logger)
    except Exception as exc:  # noqa: BLE001 - 確認の失敗で起動を止めない
        logger.warning("データベースの表定義を確認できませんでした: %s", exc)


def build_services(
    config: MonitorConfig,
    settings: AppSettings,
    *,
    init_db: bool = True,
) -> Services:
    """設定からサービス一式を組み立てる(バックグラウンドタスクは未起動)。

    ``init_db=False`` なら設定上のテーブルを作成しない(既存 DB をそのまま使う)。
    サーバー DB(MySQL/PostgreSQL)は ``db_wait_timeout`` 秒まで起動を待ってから
    テーブルを作る。作成後に config.py と DB の表定義のずれを警告する。

    ``config`` は config.py の設定。画面で変えた差分が DB にあれば重ねて使う
    (:class:`SettingsEditor`)。
    """
    db = Database.from_settings(settings)
    try:
        logger.info("データベース: %s", db.describe())
        if db.backend != "sqlite" and settings.db_wait_timeout > 0:
            db.wait_until_ready(settings.db_wait_timeout)
        audit_service = AuditService(db, settings.audit_enabled)
        settings_editor = SettingsEditor(
            config, SettingsStore(db), settings, audit=audit_service
        )
        config = settings_editor.config  # 画面で変えた差分を重ねた、使う設定
        registry = TableRegistry(config)
        if init_db:
            registry.create_all(db)
        _warn_schema_drift(db, registry)
        repository = TableRepository(db, registry)

        # 共有サービス(バックグラウンドタスクからも参照するため先に生成する)。
        view_cache = ViewCache(
            settings.view_cache_ttl_ms,
            change_token=change_token_for(db),
            max_age_ms=settings.view_cache_max_age_ms,
            max_bytes=settings.view_cache_max_mb * 1024 * 1024,
        )
        view_service = ViewService(config, db, cache=view_cache)
        alert_engine = AlertEngine(config, settings)

        # ソースコネクタ(config.sources が定義されているとき常に組み立てる)。
        # 取り込み(CSV・ソース)で行が変わったらビューのキャッシュを捨て、
        # PostgreSQL / MySQL でも新しいデータをすぐ表示する。
        connectors = build_source_connectors(
            config, registry, db, on_change=view_cache.clear
        )

        background_tasks: list[PeriodicTask] = []
        # フォルダ監視(フェーズ1・C)。
        if settings.ingest_watch:
            importer = build_csv_importer(
                config, registry, db, settings, settings.csv_dir, view_cache
            )
            background_tasks.append(
                IngestWatcher(
                    importer,
                    settings.ingest_interval,
                    keep=settings.ingest_mode == "append",
                    connectors=connectors,
                )
            )
        # アラートのバックグラウンド評価(#13)。alerts があれば常に起動し、
        # 画面を誰も開いていなくても閾値を評価・通知する。
        if config.alerts:
            background_tasks.append(
                AlertScheduler(alert_engine, view_service, alert_interval(config))
            )
        # 保持期間(TableDef.retention)を過ぎた行の定期削除。削除したらビューの
        # キャッシュを捨てる。
        retention_task = build_retention_task(
            config, registry, db, settings, on_change=view_cache.clear
        )
        if retention_task is not None:
            background_tasks.append(retention_task)
        # SQLite の定期バックアップ(MONITOR_BACKUP_DIR を設定したとき)。
        backup_task = build_backup_task(db, settings)
        if backup_task is not None:
            background_tasks.append(backup_task)

        crud_service = CrudService(
            config, registry, repository, audit_service, view_cache=view_cache
        )
        kpi_service = KpiService(config, db, cache=view_cache)
    except BaseException:
        # 組み立て途中の失敗(DB に接続できない等)でもコネクションを残さない。
        db.dispose()
        raise

    return Services(
        settings=settings,
        config=config,
        db=db,
        registry=registry,
        repository=repository,
        connectors=connectors,
        view_cache=view_cache,
        view_service=view_service,
        alert_engine=alert_engine,
        audit_service=audit_service,
        crud_service=crud_service,
        kpi_service=kpi_service,
        settings_editor=settings_editor,
        background_tasks=background_tasks,
    )
