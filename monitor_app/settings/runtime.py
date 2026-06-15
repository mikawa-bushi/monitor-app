"""実行時設定。環境変数 ``MONITOR_*`` または ``.env`` で与える。

DB 接続情報やポートなど「環境ごとに変わる値」をここに集約し、宣言的設定
(:class:`~monitor_app.settings.declarative.MonitorConfig`)から切り離す。
これにより DB パスワードを ``config.py`` に直書きせずに済む。
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MONITOR_", env_file=".env", extra="ignore"
    )

    #: SQLAlchemy 接続 URL。例:
    #:   sqlite:///instances/database.db
    #:   mysql+pymysql://user:pass@localhost:3306/monitor_app
    #:   postgresql+pg8000://user:pass@localhost:5432/monitor_app
    database_url: str = "sqlite:///instances/database.db"

    host: str = "127.0.0.1"
    port: int = 9990

    #: 設定されている場合のみ API キー認証が有効になる。
    api_key: str | None = None
    #: True なら読み取り系エンドポイントも API キーで保護する。
    protect_reads: bool = False

    #: CORS を許可するオリジン。空ならクロスオリジンを許可しない(同一オリジンのみ)。
    cors_origins: list[str] = Field(default_factory=list)

    log_level: str = "INFO"
    csv_dir: Path = Path("csv")
    #: CSV 取り込み時の文字エンコーディング。現場の Excel 由来 CSV は
    #: "cp932"(Shift-JIS)が多い。既定は BOM 付き UTF-8 も読める "utf-8-sig"。
    csv_encoding: str = "utf-8-sig"

    # --- ビュー共有キャッシュ(#18)---
    #: ビュー結果を共有キャッシュする TTL(ミリ秒)。SSE/ダッシュボードの
    #: 多重クエリを抑える。書き込み時は無効化される。0 でキャッシュ無効。
    view_cache_ttl_ms: int = 1000

    # --- データ取り込み(フェーズ1・C)---
    #: csv/ フォルダを監視し、変更されたファイルを自動で取り込む。
    ingest_watch: bool = False
    #: フォルダ監視のポーリング間隔(秒)。
    ingest_interval: float = 5.0
    #: 自動取り込みのモード。replace=置換 / append=追記(時系列向き)。
    ingest_mode: Literal["replace", "append"] = "replace"

    # --- 監査ログ(フェーズ4・G)---
    audit_enabled: bool = False

    # --- 通知チャネル(フェーズ1・A)。未設定のチャネルは無効 ---
    webhook_url: str | None = None  # Slack/Teams/汎用 JSON POST
    line_token: str | None = None  # LINE Notify トークン
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    alert_email_to: str | None = None  # カンマ区切り可
