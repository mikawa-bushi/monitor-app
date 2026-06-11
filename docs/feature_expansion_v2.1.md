# Monitor App v2.1 機能拡張 設計ドキュメント(製造現場向け)

現状の monitor-app(CSV→ビュー/CRUD/条件付き色分け/SSE)を、製造現場で使える
「ライブ監視 + 現場入力 + トレーサビリティ」へ拡張する。設計思想(設定だけで動く・
依存最小・オフライン可・初心者向け)は維持する。

実装は4フェーズに分ける。各フェーズは独立して価値を出し、後段は前段の基盤を再利用する。

| Ph | 機能 | 現場価値 |
|----|------|----------|
| 1 | C 自動取り込み / A 閾値アラート / B Andon表示 | CSVビューア → 常時監視システム |
| 2 | D トレンド/SPCグラフ / E KPIカード | 工程監視の可視化 |
| 3 | F 作業者入力フォーム | 紙日報・チェックシートの置き換え |
| 4 | G 監査ログ / H エクスポート | 品質保証・トレーサビリティ |

---

## 共通方針

- 設定はすべて Pydantic モデルで `settings/declarative.py` に追加し、`MonitorConfig` に
  ぶら下げる。typo は起動時に検出。v1 後方互換ローダーには影響しない(新項目は任意)。
- 秘密情報・環境依存値(SMTP・Webhook URL・トークン・監視ON/OFF)は `AppSettings`
  (環境変数 `MONITOR_*`)に置く。
- 書き込み・取り込み系は既存の `require_write_auth` で保護する。
- 新しい背景処理(フォルダ監視・アラート評価)は FastAPI の lifespan で起動/停止する。
- 依存追加は最小限。チャートは Chart.js を**同梱**(オフライン現場のため)。xlsx は任意。

---

## フェーズ1

### C. 自動データ取り込み(継続インジェスト)

**目的**: 現場データは連続発生。手動 `import-csv` を不要にする。

**設定 (`AppSettings`)**
```
ingest_watch: bool = False          # csv/ フォルダ監視を有効化
ingest_interval: float = 5.0        # 監視/スケジュール取り込みの間隔(秒)
ingest_mode: Literal["replace","append"] = "replace"
```

**API**: `POST /api/ingest/{table}` — センサ/PLC/MES がレコードを直接投入。
- body: 単一オブジェクトまたは配列。`CrudService` の列フィルタ・型変換を通す。
- バルクは `TableRepository` に `insert_many` を追加して一括 INSERT。
- `require_write_auth` で保護。レスポンスは `{inserted: n}`。

**バックグラウンド監視**: `services/ingest_watcher.py`
- 依存を増やさないため `watchdog` ではなく **mtime ポーリング**(`ingest_interval` 毎)。
- csv/ 各ファイルの mtime を記録し、変化したテーブルだけ `CsvImporter` で再取り込み。
- `main.py` の lifespan で `asyncio` タスクとして起動・キャンセル。

**モジュール**: `services/importer.py`(append/増分は既存 `--keep` を活用)、
`db/repository.py`(`insert_many`)、`api/routers/ingest.py`(新規)、`main.py`(lifespan)。

### A. 閾値アラート & 通知

**目的**: 異常への気づきの遅れを防ぐ。閾値違反を即通知。

**設定 (`AlertRule`、`MonitorConfig.alerts: list[AlertRule]`)**
```python
class AlertRule(BaseModel):
    view: str                    # 評価対象ビュー名
    column: str                  # 対象列
    op: Literal[">", ">=", "<", "<=", "==", "!="]
    value: float
    level: Literal["info", "warning", "critical"] = "warning"
    message: str = ""            # 空ならルールから自動生成
    notify: list[str] = []       # 通知チャネル名(["webhook","email","line"])
```

**評価**: `services/alert_service.py` の `AlertEngine`
- ビューデータ(`ViewService.get_view`)の各行に対しルールを評価し、違反行を集計。
- **エッジ検出**: 発火中アラートのキー集合を保持し、新規発火(OFF→ON)と復帰(ON→OFF)
  のときだけ通知。連続通知を抑制。
- アクティブアラート一覧をメモリ保持(`/api/alerts` と SSE で配信)。

**通知 (`services/notifiers/`)**: 共通インターフェース `Notifier.send(event)`。
- `console`(既定・ログ出力)、`webhook`(Slack/Teams/汎用 JSON POST)、
  `email`(SMTP)、`line`(LINE Notify)。
- 接続情報は `AppSettings`(`webhook_url`, `smtp_*`, `line_token`)。未設定チャネルは無効。

**API / UI**:
- `GET /api/alerts` … 現在アクティブなアラート。
- ビューの SSE ペイロードに `alerts: [...]` を付与(または専用 `/api/alerts/stream`)。
- `table.html` / `app.js`: 違反時に画面上部へ赤バナー + ブラウザ通知 + ビープ音(Web Audio)。

**モジュール**: `settings/declarative.py`(`AlertRule`)、`services/alert_service.py`、
`services/notifiers/*`、`api/routers/alerts.py`、`view_service`/`app.js`、`main.py`(エンジン保持)。

### B. Andon / 大型表示モード

**目的**: ライン大型モニタでの常時表示。遠くから見える表示と信号灯色。

**設定 (`KioskConfig`、`MonitorConfig.kiosk`)**
```python
class KioskConfig(BaseModel):
    views: list[str] = []            # ローテーション対象(空なら全ビュー)
    rotate_seconds: int = 15
    theme: Literal["dark", "light"] = "dark"
```

**UI**: `/kiosk` ルート + `kiosk.html` / `kiosk.js`
- 複数ビューを `rotate_seconds` ごとに自動切替。SSE 購読でデータ更新。
- ダーク/ハイコントラストテーマ(既存 CSS 変数を `data-theme="dark"` で切替)。
- 全体ステータス(緑/黄/赤)を A のアクティブアラートから算出。異常時はローテーションを
  止めて該当ビューを強調。

**モジュール**: `api/routers/pages.py`(`/kiosk`)、`web/templates/kiosk.html`、
`web/static/js/kiosk.js`、`web/static/css/app.css`(ダークテーマ変数)。

---

## フェーズ2

### D. トレンドグラフ / 管理図(SPC)

**設定 (`ChartDef`、`ViewDef.chart`)**
```python
class ChartDef(BaseModel):
    type: Literal["line", "bar"] = "line"
    x: str                       # 横軸の列(時刻・連番)
    y: str | list[str]           # 縦軸の列(複数系列可)
    ucl: float | None = None     # 管理上限線
    lcl: float | None = None     # 管理下限線
    target: float | None = None  # 目標線
```

**UI**: `table.html` を表示タイプで分岐(表 or グラフ)。Chart.js を `web/static/js/` に
同梱。`app.js` がビューデータをチャートデータに整形。UCL/LCL/target は注釈線、
管理限界外の点を強調。リアルタイム更新は既存 SSE。

**モジュール**: `settings/declarative.py`(`ChartDef`)、`web/static/js/chart.js`、
`table.html`、`pages.py`(chart 情報をテンプレートへ)。

### E. KPIサマリーカード

**設定 (`KpiCard`、`MonitorConfig.kpis: dict[str, KpiCard]`)**
```python
class KpiCard(BaseModel):
    title: str
    query: str                   # スカラー1値を返す SELECT
    unit: str = ""
    target: float | None = None
    higher_is_better: bool = True
    format: str = "{:.0f}"
```

**API / UI**: `GET /api/kpis` が全カードを readonly 実行して値を返す。ホーム or 専用
ダッシュボードにカードグリッド。目標との差で色分け。Kiosk と共用。

**モジュール**: `settings/declarative.py`(`KpiCard`)、`services/kpi_service.py`、
`api/routers/meta.py`(`/api/kpis`)、`web`(カードUI)。

---

## フェーズ3

### F. 作業者入力フォーム(電子チェックシート)

**設定 (`FormDef`、`TableDef.form`)**
```python
class FormDef(BaseModel):
    fields: list[str] | None = None        # 入力対象列(既定は主キー以外の全列)
    labels: dict[str, str] = {}            # 列の表示名
    choices: dict[str, list[str]] = {}     # 選択肢(不良コード等のマスタ)
```

**UI**: `/form/{table}` がスキーマからタッチ向けフォームを自動生成。列の論理型に応じた
入力(数値/日付/テキスト/選択)。送信は既存 `POST /api/tables/{table}`。送信後クリアして
連続入力。任意でバーコード/QR(端末スキャナのキー入力 or カメラ)。

**モジュール**: `settings/declarative.py`(`FormDef`)、`api/routers/pages.py`
(`/form/{table}`)、`web/templates/form.html`、`web/static/js/form.js`。
再利用: `CrudService.create_record`、`schema()`。

---

## フェーズ4

### G. 変更履歴 / 監査ログ

**目的**: 「いつ・誰が・何を変えたか」の追跡(ISO/IATF 等で必須)。

**設定 (`AppSettings`)**: `audit_enabled: bool = False`

**仕組み**: 内部メタテーブル `_audit`(table, record_id, action, before, after,
actor, created_at)。`CrudService` の create/update/delete 成功後にフックして記録。
actor は API キー識別子(`deps` でキー→名称を解決)。`GET /api/audit` で閲覧。

**モジュール**: `services/audit_service.py`、`crud_service.py`(フック)、
`db`(内部テーブル登録)、`api/routers/audit.py`。

### H. エクスポート(CSV / Excel)

**API**: `GET /api/views/{view}/export?format=csv|xlsx`
- csv は標準ライブラリ、xlsx は任意依存 `openpyxl`(未導入時は csv のみ)。
- UI のビュー画面にダウンロードボタン。

**モジュール**: `api/routers/views.py`(export ルート)、`services/view_service.py` 再利用。

---

## テスト計画

- 単体: `AlertEngine` のエッジ検出、`KpiCard`/`ChartDef`/`FormDef`/`AlertRule` の
  バリデーション、`insert_many`、coercion。
- API 統合(既存フィクスチャ流用): `/api/ingest`、`/api/alerts`、`/api/kpis`、
  `/api/audit`、`/api/views/{v}/export`、`/kiosk`・`/form/{t}` の HTML。
- 通知は `console` notifier で副作用を観測(外部送信はモック)。
- 監視ループ・lifespan はユニットで `ingest_watcher` の差分検知を直接呼ぶ。

## 実装順(依存)

1. `settings/declarative.py` に全モデル追加(土台)
2. Ph1: `insert_many` → ingest ルート → watcher/lifespan → AlertEngine/notifiers → alerts API/SSE → kiosk
3. Ph2: ChartDef/KpiCard → kpi_service → /api/kpis → chart.js/UI
4. Ph3: FormDef → /form ルート → form UI
5. Ph4: audit_service → CRUD フック → /api/audit → export
6. scaffold/config.py と README に新機能の例を反映、テスト・black・実機検証
