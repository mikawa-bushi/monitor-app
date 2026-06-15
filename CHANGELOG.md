# Changelog

## [2.3.0] - 2026-06-15

> 2.1.0 以降の v2.2(外部ツール連携)・v2.3(UI 刷新・ダークテーマ・ダッシュボード)
> と、コードレビュー起点の堅牢化/高速化/セキュリティ修正をまとめて公開。下の
> 「2.2.0」節の integrations 機能も本リリースに同梱(PyPI は 2.1.0 → 2.3.0)。

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

### Security
- **埋め込み JSON の `</script>` 混入による XSS を防止(#S1)** — `<script
  type="application/json">` に埋め込む dashboard/kiosk/chart の JSON を、`<` `>`
  `&` と行区切り(U+2028/U+2029)を `\uXXXX` へ置換して無害化。ビュータイトルや
  チャートラベル等の config 由来文字列に `</script>` を含めても script タグを
  閉じられない(`JSON.parse` 後の値は不変)
- **基本的なセキュリティヘッダを付与(#S2)** — 全レスポンスへ
  `X-Content-Type-Options: nosniff` / `X-Frame-Options: SAMEORIGIN` /
  `Referrer-Policy: same-origin` を付与(MIME スニッフィング XSS・クリック
  ジャッキングの基本対策)。厳格な CSP は将来対応
- **network_checker のホスト名を検証(#S3)** — SQL へ直接埋め込むホスト名を
  許可文字(英数字 `. _ - :`)で検証し、不正な値は早期に拒否。手動エスケープ依存を解消

### Fixed
- **パスパラメータの主キーを型変換(#E8)** — URL 上の主キーを主キー列の論理型へ
  coerce してから比較。PostgreSQL/MySQL での `integer = '5'(text)` 型不一致を防止
- **fetch 失敗の可視化(#E9)** — ダッシュボード/KPI/kiosk が取得失敗時に黙って
  古い値を保持していたのを、stale 表示(最終更新時刻・灰の点滅)で明示
- **入力フォームの二重送信ガード(#U3)** — 送信中は submit ボタンを無効化し、連打
  による重複登録を防止
- **ブラウザ通知をオプトインに(#U4)** — ロード時に許可ダイアログを出さず、
  「通知を有効化」ボタンのユーザー操作を起点に要求
- **KPI 評価の N+1 解消(#V4)** — 全 KPI を 1 本の読み取り専用コネクションで評価。
  1 件の失敗は rollback で分離
- **取り込みのチャンク挿入(#V5)** — 一括 INSERT を 1000 件ずつに分割し、パラメータ
  上限超過と巨大文によるメモリ圧迫を回避
- **インデックス自動生成(#V6)** — 外部キー列へ索引を自動付与し、`TableDef.indexes` で
  時刻/ウォーターマーク列に索引を宣言可能に。integrations の時系列ビューにも適用
- **SSE が瞬断で恒久的にポーリングへ降格する問題を修正(#U2)** — これまで
  `EventSource.onerror` で即 `close()` していたため、ネットワークの一瞬の切断でも
  二度と SSE に戻らずポーリング固定になっていた。ブラウザ標準の自動再接続を活かし、
  連続失敗が閾値(6 回)を超えたときだけポーリングへ降格するよう変更。接続回復時は
  自動で差分配信(SSE)に戻る

### Changed
- **ruff による import 整列を導入(#M3)** — import 順の不統一を ruff(isort 相当)で
  自動整列し、CI で強制(`code-quality-check`)。差分ノイズを根絶
- **integrations の SQLite 方言依存を明記(#M4)** — procdiag/netdiag/network_checker の
  生成するビュー/KPI が SQLite 方言(`datetime('now',...)` 等)に依存することを docstring
  と integrations ドキュメントに明記(アプリ DB が SQLite 前提)

### Removed
- **LINE Notify 通知チャネルを削除** — LINE Notify は 2025-03-31 にサービス終了済みで、
  常に失敗する死んだ統合だったため除去。`notify=["line"]` チャネル・`MONITOR_LINE_TOKEN`
  設定・`LineNotifier` を削除した。現場スマホへのプッシュが必要な場合は `webhook`
  (汎用 JSON POST)経由を推奨

## [2.2.0] - 2026-06-13 — 2.3.0 に同梱(PyPI 未公開)

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
