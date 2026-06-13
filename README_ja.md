# Monitor App 🚀

[![PyPI version](https://img.shields.io/pypi/v/monitor-app.svg)](https://pypi.org/project/monitor-app/)
[![Python versions](https://img.shields.io/pypi/pyversions/monitor-app.svg)](https://pypi.org/project/monitor-app/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/mikawa-bushi/monitor-app/blob/main/LICENSE)

**CSV を置いて設定ファイルを 1 つ書くだけで、リアルタイム更新の Web UI と REST API が手に入る。**

Monitor App は製造現場・実験室・小規模チーム向けの FastAPI 製データ監視ツールです。テーブルやビューを `config.py` に宣言的に書くと(起動時に Pydantic が検証)、リアルタイムダッシュボード・Swagger ドキュメント付き CRUD REST API・閾値アラート・Andon 大型表示が動き出します。フロントエンドのビルドも Node.js も不要で、オフラインの工場ネットワークでも動作します。

English documentation: [README.md](https://github.com/mikawa-bushi/monitor-app/blob/main/README.md)

---

## ✨ 特徴

- **3 コマンドで CSV が Web アプリに** — CSV を置いてサーバーを起動するだけ
- **型安全な宣言的設定** — テーブル・ビュー・スタイル・アラートを 1 つの `config.py` に。typo は起動時に分かりやすいメッセージで検出(`monitor-app check`)
- **リアルタイム更新** — Server-Sent Events で差分配信、ポーリングへ自動フォールバック
- **フル REST API** — テーブルごとの CRUD、バルク取り込み、`/docs` に Swagger UI を自動生成
- **SQLite / MySQL / PostgreSQL** — 環境変数 `DATABASE_URL` 1 本で切り替え
- **条件付きセル色分け** — 閾値による色分けをコードなしで設定
- **フロントエンドツールチェーン不要** — バニラ JS + 同梱アセットでオフライン動作

### 🏭 製造現場向け機能(v2.1)

| 機能 | 内容 | 有効化 |
|---|---|---|
| 自動データ取り込み | `csv/` の変更を監視、センサ/PLC/MES からは `POST /api/ingest/{table}` | `MONITOR_INGEST_WATCH` / API は常時 |
| 閾値アラート | 画面バナー+音、Webhook(Slack/Teams)・LINE・メール通知。エッジ検出で連続通知を抑制 | `alerts=[AlertRule(...)]` |
| Andon 大型表示 | `/kiosk` で複数ビューを自動ローテーション、緑/黄/赤の信号灯 | `kiosk=KioskConfig(...)` |
| トレンド / SPC グラフ | UCL/LCL/目標線つき折れ線・棒グラフ、管理限界外の点を強調 | `ViewDef(chart=ChartDef(...))` |
| ダッシュボード | ホームをミニチャートグリッド(列数 1〜4)主体に刷新。グラフなしビューはリンク一覧として下部に表示 | `dashboard=DashboardConfig(columns=2)` |
| グループナビ | サイドバーとビュー一覧でグループ見出しを表示 | `ViewDef(group="...")` |
| ダークテーマ | ヘッダーのトグルボタン(🌙/☀️)でライト/ダーク切替。`localStorage["monitor-theme"]` に保存 | テーマトグルボタン |
| KPI カード | 生産数・良品率・OEE などをホーム上部に色分け表示 | `kpis={...}` |
| 作業者入力フォーム | スキーマから自動生成するタッチ向け入力画面 `/form/{table}` | `TableDef(form=FormDef(...))` |
| 監査ログ | 全書き込みの変更履歴(誰が・いつ・何を) | `MONITOR_AUDIT_ENABLED` |
| エクスポート | ビューを CSV / Excel でダウンロード | `/api/views/{view}/export` |

---

## 🚀 インストール

```sh
pip install monitor-app
```

Excel エクスポートを使う場合:

```sh
pip install "monitor-app[xlsx]"
```

## 🔧 クイックスタート

```sh
# 1. プロジェクト作成(config.py とサンプル CSV が生成され、すぐ動く)
monitor-app startproject myproject
cd myproject

# 2. CSV を取り込んでサーバー起動
monitor-app runserver --import-csv

# 3. http://127.0.0.1:9990 を開く(Swagger UI は /docs)
```

### CLI コマンド

| コマンド | 説明 |
|---|---|
| `monitor-app startproject <name>` | プロジェクト雛形を生成 |
| `monitor-app runserver [--import-csv] [--reload] [--host H] [--port P]` | Web サーバーを起動 |
| `monitor-app import-csv [--keep]` | `csv/` を DB へ取り込み(`--keep` で追記) |
| `monitor-app check` | サーバーを起動せず `config.py` を検証 |

## 📝 設定

設定はすべて 1 つの `config.py` に書きます。間違いは実行時ではなく起動時に報告されます:

```python
from monitor_app import (
    AlertRule, CellStyle, ChartDef, FormDef, KioskConfig,
    KpiCard, MonitorConfig, TableDef, Threshold, ViewDef,
)

config = MonitorConfig(
    app_title="ライン監視",
    refresh_interval_ms=2000,
    # CRUD テーブル — 列型は推論(明示も可)
    tables={
        "measurements": TableDef(
            columns={"id": "int", "ts": "str", "temp": "float"},
            form=FormDef(labels={"temp": "温度"}),   # 作業者入力フォーム
        ),
    },
    # 表示ビュー — JOIN・集計を含む任意の SELECT
    views={
        "temp_trend": ViewDef(
            query="SELECT ts, temp FROM measurements ORDER BY id",
            title="温度トレンド",
            chart=ChartDef(type="line", x="ts", y="temp", ucl=80, lcl=20),
            styles={"temp": CellStyle(
                greater_than=Threshold(value=80, **{"class": "bg-danger text-white"}),
            )},
        ),
    },
    # 閾値アラート + 通知
    alerts=[
        AlertRule(view="temp_trend", column="temp", op=">", value=80,
                  level="critical", notify=["webhook"]),
    ],
    # KPI カードと Andon 大型表示
    kpis={"count": KpiCard(title="サンプル数", query="SELECT COUNT(*) FROM measurements")},
    kiosk=KioskConfig(rotate_seconds=15, theme="dark"),
)
```

環境ごとに変わる値(`config.py` には書かない)は `.env` に置きます:

```sh
MONITOR_DATABASE_URL=postgresql+pg8000://user:pass@db:5432/monitor   # 既定は SQLite
MONITOR_API_KEY=change-me            # 書き込み系 API に X-API-Key 認証を有効化
MONITOR_INGEST_WATCH=true            # csv/ の変更を自動取り込み
MONITOR_AUDIT_ENABLED=true           # 変更履歴を記録
MONITOR_WEBHOOK_URL=https://hooks.slack.com/services/...
```

## 🌐 エンドポイント

| パス | 説明 |
|---|---|
| `/` | ホーム: ミニチャートダッシュボード、KPI カード、ビュー一覧 |
| `/table/{view}` | リアルタイム表 / グラフ |
| `/kiosk` | Andon 大型表示(全画面・自動ローテーション) |
| `/form/{table}` | 作業者入力フォーム |
| `/docs`, `/redoc` | OpenAPI ドキュメント |
| `/api/tables/{table}` | CRUD(GET/POST/PUT/DELETE) |
| `/api/ingest/{table}` | センサ/PLC 向けバルク取り込み |
| `/api/views/{view}` | ビューデータ(`/stream` で SSE、`/export` で CSV/Excel) |
| `/api/kpis`, `/api/alerts`, `/api/audit`, `/api/schema`, `/api/health` | KPI・アラート・監査ログ・スキーマ・死活監視 |

## 🔌 外部ツール連携(integrations、v2.2)

procdiag・netdiag・network-checker を数行で Monitor App に接続できます。外部ツール側の変更は不要です(読み取り専用・pull 型)。

```python
from monitor_app.integrations import procdiag, netdiag, network_checker

config = procdiag.attach(config, db_path="/opt/procdiag/data/procdiag.db")
config = netdiag.attach(config)
config = network_checker.attach(config, hosts=["plc-01", "gateway"])
```

各 `attach()` は既存の config にテーブル・ビュー・KPI・アラートを合成して返します。`monitor-app sync-sources` で手動同期、`MONITOR_INGEST_WATCH=1` で常時ポーリングが有効になります。

詳細: [docs/integrations_v2_2.md](https://github.com/mikawa-bushi/monitor-app/blob/main/docs/integrations_v2_2.md)

---

## 🔄 v0.x からの移行

v2 で FastAPI + Pydantic に再構築しました。旧 dict 形式の `config.py`
(`ALLOWED_TABLES` / `VIEW_TABLES` / `TABLE_CELL_STYLES`)も互換ローダーで**そのまま動きます**が、新規プロジェクトでは `MonitorConfig` を推奨します。主な変更:

- `python app.py runserver` → `monitor-app runserver`(アプリ本体はプロジェクト内に複製されません)
- DB 設定(`DB_TYPE`・パスワード)→ 環境変数 `MONITOR_DATABASE_URL`
- API パス: `/api/<table>` → `/api/tables/{table}`、`/api/view/<v>` → `/api/views/{view}`

詳細: [再設計ドキュメント](https://github.com/mikawa-bushi/monitor-app/blob/main/docs/redesign_v2.md) ·
[v2.1 機能設計](https://github.com/mikawa-bushi/monitor-app/blob/main/docs/feature_expansion_v2.1.md) ·
[v2.2 外部連携](https://github.com/mikawa-bushi/monitor-app/blob/main/docs/integrations_v2_2.md) ·
[v2.3 UI 刷新](https://github.com/mikawa-bushi/monitor-app/blob/main/docs/ui_redesign_v2_3.md)

## 🐳 Docker

```sh
docker build -t monitor-app -f docker/Dockerfile .
docker run -v $(pwd)/myproject:/project -p 9990:9990 monitor-app
```

## 🛠 開発

```sh
git clone https://github.com/mikawa-bushi/monitor-app.git
cd monitor-app
poetry install
poetry run pytest
```

コントリビューションは [CONTRIBUTING.md](https://github.com/mikawa-bushi/monitor-app/blob/main/CONTRIBUTING.md) を参照してください。

## 📜 ライセンス

[MIT](https://github.com/mikawa-bushi/monitor-app/blob/main/LICENSE)
