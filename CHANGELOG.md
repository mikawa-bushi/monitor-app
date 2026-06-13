# Changelog

## [Unreleased] — v2.3

### Added (UI redesign — dashboard & dark theme)
- **ダッシュボードホームページ** — ホームを `chart` 付きビューのミニチャートグリッド主体に刷新。
  グラフなしビューはリンク一覧として下部に表示。グリッド列数は `DashboardConfig.columns`(1〜4)で変更可
- **`DashboardConfig`** — `MonitorConfig.dashboard` に `views`(表示するビューの明示指定)と
  `columns`(列数)を追加。`dashboard.views` に `chart` なしビューを指定すると起動時エラー
- **`ViewDef.group`** — ナビ・ビュー一覧でのセクション名フィールドを追加。サイドバーと
  ホームのビュー一覧でグループ見出しが表示される
- **ダークテーマ** — `theme.js` が `localStorage["monitor-theme"]` を読んで `<html data-theme>` を設定(FOUC なし)。
  ヘッダーのトグルボタン(🌙/☀️)でライト / ダーク切替。`monitor:themechange` イベントで Chart.js の色も即時更新
- **CSS 変数 `--chart-grid` / `--chart-tick`** — グリッド線・目盛り色をテーマ変数で制御。
  ライト既定値はそれぞれ `rgba(100,116,139,0.18)` / `#475569`
- **ビューページのテーブル折りたたみ** — `chart` 付きビュー(`/table/<view>`)でチャートを最上部に配置し、
  データテーブルを `<details>` で折りたたみ表示。`#row-count` バッジで行数を表示
- **`MonitorChart.create()` compact オプション** — `{ compact: true }` で凡例・軸タイトルを省略し
  目盛りを絞ったミニ表示(ダッシュボードカード用)

## [Unreleased] — v2.2

### Added (external tool integrations)
- **`SourceDef` / declarative sources** — `MonitorConfig.sources` で SQLite / JSONL / JSON
  ファイルを宣言的に取り込み先テーブルへ紐付けられるようになりました
  (`kind`, `path`, `table`, `watermark_column`, `mapping`, `mode` など)
- **3 コネクタ** — `SqliteSourceConnector`(watermark / mtime 差分)・
  `JsonlSourceConnector`(バイトオフセット差分)・`JsonSourceConnector`(mtime 全置換)
  を `monitor_app/services/connectors.py` に実装
- **`sync-sources` CLI** — `monitor-app sync-sources` で全ソースを一括同期(サーバー不要)
- **3 プリセット** — ツール無改造・読み取り専用で接続:
  - `monitor_app.integrations.procdiag` — CPU / メモリ / PSI / 診断所見
  - `monitor_app.integrations.netdiag` — netdiag watch アラートログ(JSONL)
  - `monitor_app.integrations.network_checker` — ping 結果 SQLite

## [2.1.0] - 2026-06-10

Complete rewrite on **FastAPI + Pydantic** (from Flask), plus manufacturing-focused features.

### Added (shop-floor features)
- **Auto data ingest** — `csv/` folder watching (`MONITOR_INGEST_WATCH`) and bulk
  `POST /api/ingest/{table}` for sensors / PLC / MES
- **Threshold alerts** — declarative `AlertRule`s, edge-triggered notifications
  (console / Webhook / LINE / e-mail), on-screen banner with sound, `/api/alerts`
- **Andon wallboard** — `/kiosk` full-screen rotating views with green/amber/red status light
- **Trend / SPC charts** — `ChartDef` with UCL/LCL/target lines (bundled Chart.js, offline-capable)
- **KPI cards** — scalar-SQL `KpiCard`s with target comparison, `/api/kpis`
- **Operator entry forms** — touch-friendly forms generated from table schema at `/form/{table}`
- **Audit log** — before/after change history with actor (`MONITOR_AUDIT_ENABLED`), `/api/audit`
- **Export** — `/api/views/{view}/export?format=csv|xlsx` (`pip install monitor-app[xlsx]`)

### Changed (v2 architecture)
- Flask → **FastAPI**; flasgger's hand-written Swagger YAML → automatic OpenAPI (`/docs`, `/redoc`)
- dict-based config → **type-safe `MonitorConfig`** (Pydantic). Typos are caught at startup;
  `monitor-app check` validates without starting the server. The legacy v1 dict format
  (`ALLOWED_TABLES` etc.) still loads via a compatibility shim
- Raw f-string SQL → **SQLAlchemy 2.0 Core expression builder** (structural injection safety)
- pandas CSV import → stdlib `csv` with config-driven schema (no more schema clobbering;
  pandas dependency removed)
- `startproject` no longer copies application source — only `config.py`, `.env.example`
  and sample CSVs; the app updates via `pip install -U monitor-app`
- DB settings (`DB_TYPE`, credentials) → `MONITOR_DATABASE_URL` environment variable / `.env`
- API paths: `/api/<table>` → `/api/tables/{table}`, `/api/view/<v>` → `/api/views/{view}`,
  schema list → `/api/schema`
- Polling-only refresh → **Server-Sent Events** with polling fallback
- Vue (CDN) frontend → vanilla JS + bundled assets; modern minimal UI
- click CLI → **Typer** (`runserver`, `import-csv`, `check`, `startproject`)
- Optional API-key auth (`MONITOR_API_KEY`, `X-API-Key` header) for write endpoints;
  CORS now defaults to same-origin (configurable)

### Removed
- Flask, flask-sqlalchemy, flask-cors, flasgger, pandas, toml dependencies
- jQuery / DataTables assets

## [0.1.9] - earlier
- Final Flask-based release.
