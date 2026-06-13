# UI 刷新(v2.3)— 実装計画

「リンク一覧」だったトップを監視ダッシュボードに作り替え、ナビゲーション・
ダークテーマ・グラフ主役のビューページに刷新する。タスクは**ファイル所有権**で
分割しており、自タスクの所有ファイル以外は変更禁止(読むのは自由)。

## ウェーブと依存関係

| Wave | タスク | 内容 |
|---|---|---|
| 1 | U01 | 基盤: ViewDef.group / DashboardConfig / ページコンテキスト |
| 2 | U02, U03, U04 | base+テーマ / ダッシュボード / ビューページ(並列) |
| 3 | U05 | ドキュメント・scaffold |
| 4 | — | 統合検証(メインセッションが実施) |

## 全タスク共通ルール

- 既存スタイル踏襲: 日本語 docstring / コメント、`from __future__ import annotations`。
  JS は既存の IIFE + ES5 風(var でなく const/let は可、ビルドなし・素の JS)。
  CSS は app.css 冒頭の保守指針に従う(色は CSS 変数経由)。
- 新規依存の追加禁止。Chart.js / bootstrap.min.css は同梱済みをそのまま使う。
- Python は `.venv/bin/black <所有ファイル>` でフォーマット。JS は `node -c <file>` で
  構文チェック。
- テスト: `.venv/bin/python -m pytest tests/<自分のテスト>.py -q`、最後に全体
  `.venv/bin/python -m pytest -q`(並列実装中の他タスク起因の失敗は無視してよいが、
  自分の所有ファイル起因の失敗はゼロにする)。
- **ポート 9990 で動作中のデモサーバには触らない**(kill・再起動禁止)。検証は
  TestClient で行う。
- git コミットはしない。

## 凍結済みインターフェース契約(変更不可)

### 1. 宣言モデル追加(U01 が実装、declarative.py)

```python
class ViewDef(BaseModel):
    ...既存フィールド...
    group: str = ""  # ナビ・一覧でのセクション名(空は「その他」扱い)

class DashboardConfig(BaseModel):
    """トップページのミニチャートグリッド設定。"""
    views: List[str] = Field(default_factory=list)  # 空なら chart 付き全ビュー
    columns: int = 3  # 1〜4

class MonitorConfig(BaseModel):
    ...既存...
    dashboard: DashboardConfig = Field(default_factory=DashboardConfig)
```

検証: `dashboard.views` の各要素は `views` に存在し **chart を持つ**こと。
`columns` は 1〜4。

### 2. ページコンテキスト(U01 が実装、pages.py)

`_ctx(config, **extra)` が全ページ共通で以下を追加提供:

```python
"nav_groups": [  # config.views の出現順。group=="" は最後に「その他」として集約。
                 # 全ビューが group=="" なら group 名 "" の単一グループ(見出しなし表示)
  {"group": "ホストリソース", "views": [{"name": "procdiag_cpu", "title": "CPU 使用率(直近 24h)"}, ...]},
  ...
]
```

`index()` が追加で渡すもの:

```python
"dashboard_json": json.dumps({
    "columns": <int>,
    "views": [{"name": ..., "title": ..., "chart": <ChartDef の dict>}],
}, ensure_ascii=False)
# 対象: config.dashboard.views(空なら chart 付き全ビューを config.views 順)
"list_views": [  # chart を持たないビューのみ、nav_groups と同形式(リンク一覧用)
  {"group": ..., "views": [{"name", "title"}]},
]
# 既存の has_kiosk / has_kpis / refresh_interval_ms / form_tables は維持
```

`show_table()` が追加で渡すもの: `"current_view": view_name`(ナビの現在地強調用。
index 等では未定義なのでテンプレートは `current_view | default("")` で参照)。

### 3. テーマ(U02 が実装)

- `<html data-theme="dark">` の有無で切替。app.css は `[data-theme="dark"]` で
  `:root` の CSS 変数一式を上書きする。
- 追加 CSS 変数(ライト/ダーク両方で定義):
  `--chart-grid`(ライト既定 `rgba(100,116,139,0.18)`)、
  `--chart-tick`(ライト既定 `#475569`)
- `static/js/theme.js`: localStorage キー `"monitor-theme"`("dark"|"light")を
  読んで data-theme を設定(FOUC 防止のため base.html の `<head>` 内で同期読込)。
  トグル時に `document` へ `CustomEvent("monitor:themechange")` を dispatch。
- base.html ヘッダーにトグルボタン `<button id="theme-toggle">`(🌙/☀️)。

### 4. チャート(U04 が実装、U03 が利用)

- `MonitorChart.create(canvas, cfg, opts)` — 第 3 引数 `opts` は省略可。
  `{compact: true}` で: 凡例なし・軸タイトルなし・目盛り上限 x:6 / y:5・
  pointRadius 0。既存呼び出し(2 引数)の挙動は不変。
- chart-view.js はグリッド/目盛り色を `getComputedStyle` で `--chart-grid` /
  `--chart-tick` から読む(取れなければ現行の定数にフォールバック)。
  `monitor:themechange` を listen して色を更新し `chart.update("none")`。

### 5. テーブルページ(U04 が実装)

- `table.html` は `data-view="{{ view_name }}"` 属性を**必ず残す**(app.js と既存
  テストが参照)。
- chart 付きビュー: チャートを主役(上・大きく)、テーブルは `<details>` で
  折りたたみ(既定 closed)。chart なしビュー: 従来どおりテーブルを開いた状態。

### 6. ステータスサマリー(U03 が実装)

- データ源: `GET /api/alerts` → `{"alerts": [{"view","column","level","message","count"}]}`。
  評価はビュー取得で駆動されるため、ダッシュボードの定期取得自体が評価を回す。
- 0 件: 緑ドット+「すべて正常」。1 件以上: 赤(critical)/黄(warning)ドット+
  「アラート N 件」+ message の列挙(`/table/<view>` へのリンク)。
  右端に「最終更新 HH:MM:SS」。

### 7. 数値フォーマット(U04 が実装、app.js)

セル値が `typeof v === "number" && !Number.isInteger(v)` のとき
`v.toLocaleString("ja-JP", {maximumFractionDigits: 2})`。整数と文字列は不変
(id 列に桁区切りを入れないため)。

## 検証環境

- venv: `.venv`(構築済み)。テストはリポジトリルートから実行。
- 現在テストは 194 件全パス・black クリーンが基準線。
