# U03: トップページのダッシュボード化

先に `tasks/U00-overview.md` を読むこと。本タスクは Wave 2(U01 完了済み。
index のコンテキストに `dashboard_json` / `list_views` / `nav_groups` がある)。

## 所有ファイル(これ以外は変更禁止)

- `monitor_app/web/templates/index.html`
- `monitor_app/web/static/js/dashboard.js`(新規)
- `monitor_app/web/static/css/dashboard.css`(新規。index.html から読み込む)
- `monitor_app/web/static/js/kpis.js`
- `tests/test_pages.py`(index 関連の既存 assert の更新のみ)
- `tests/test_ui_dashboard.py`(新規)

並列注意: base.html(サイドバー)は U02 が、chart-view.js は U04 が同時改修中。
chart-view.js の `MonitorChart.create(canvas, cfg, opts)` は U00 §4 の契約を
前提に `{compact: true}` を渡してよい(旧実装でも第 3 引数は無視され動作する)。

## やること

### 1. index.html の再構成(上から順)

1. **ステータスサマリー行**(`#status-bar`)— U00 §6 の契約どおり。
   dashboard.js が `/api/alerts` を定期取得して描画。
2. **KPI グリッド**(既存 `#kpi-grid`、kpis.js)— has_kpis 時のみ。
3. **ミニチャートグリッド**(`#dashboard-grid`)—
   `<script id="dashboard-config" type="application/json">{{ dashboard_json | safe }}</script>`
   を置き、dashboard.js が読む。カード = タイトル(`/table/<name>` への
   リンク)+ `<canvas>`。列数は `columns`(CSS Grid、900px 未満は 1 列)。
4. **テーブルビュー一覧**(`list_views`)— chart を持たないビューへの
   リンクをグループ見出し付きで(従来の view-list スタイル踏襲、title 表示)。
5. **データ入力**(`form_tables`)— 既存のまま維持。
6. ページ見出しは「ビュー一覧」→「ダッシュボード」に変更。

空状態(C3): ビューが 1 つもない場合は従来どおり config 誘導。chart 付き
ビューが 0 でダッシュボード対象なしの場合はグリッドを出さない(セクションごと
非表示)。

### 2. dashboard.js(新規)

- `#dashboard-config` の JSON を読み、各ビューについて:
  `MonitorChart.create(canvas, view.chart, {compact: true})` でチャートを作り、
  `GET /api/views/<name>` で初回データ投入、`refresh_interval_ms`
  (`#dashboard-grid` の `data-refresh-interval` 属性で渡す)間隔で再取得。
- ステータスバー: `/api/alerts` を同間隔で取得し U00 §6 のとおり描画。
  取得失敗時は前回表示を維持(kpis.js と同じ方針)。レベルは alerts 内の
  level の最悪値(critical > warning > info)でドット色を決める。
  各 message は `/table/<view>` へのリンク。右端に「最終更新 HH:MM:SS」
  (`toLocaleTimeString("ja-JP", {hour12: false})`)。
- Chart.js が未ロードのケース(チャート 0 件で table.html 用スクリプトなし)に
  備え、index.html 側でチャートがあるときだけ chart.umd.min.js と
  chart-view.js を読み込む(table.html の読み込みパターンを踏襲)。

### 3. kpis.js の強化(B3)

- 既存の描画は維持しつつ: target がある KPI は「目標 X」の横に達成状況を
  矢印などで示す(status は API が ok/bad/neutral を返すので色は CSS 任せ)。
  `k.display` はそのまま使う(数値整形は API 側で済んでいる)。
- カードクリックなどの追加挙動は不要。

### 4. dashboard.css(新規)

ステータスバー(ドット・色)、ミニチャートグリッド(カード、canvas 高さ
180px 程度、レスポンシブ)、リンク一覧の微調整。色は app.css の CSS 変数
(`var(--surface)` 等)だけを使い、ダークテーマに自動追従させる。
固有の色が要る場合も `--ok: #16a34a` のような変数を **dashboard.css 内で**
定義する(app.css には触らない)。

### 5. テスト

- `tests/test_pages.py`: `test_index_page` の「ビュー一覧」assert を新見出し
  (「ダッシュボード」)に更新。**他のテストには触らない**。
- `tests/test_ui_dashboard.py`(新規):
  - chart 付きビューがある config で `/` に `dashboard-config` JSON が含まれ、
    その中身に chart 付きビューだけが入っている
  - `#status-bar` と `#dashboard-grid` が存在する
  - chart なしビューが `list_views` セクションにリンクとして出る(title 表示)
  - ビュー 0 件 config でも 200 で空状態文言が出る
  - `/static/js/dashboard.js` と `/static/css/dashboard.css` が 200

## 完了条件

```bash
.venv/bin/python -m pytest tests/test_ui_dashboard.py tests/test_pages.py -q
.venv/bin/python -m pytest -q   # 自分の所有ファイル起因の失敗ゼロ
node -c monitor_app/web/static/js/dashboard.js && node -c monitor_app/web/static/js/kpis.js
.venv/bin/black --check tests/test_pages.py tests/test_ui_dashboard.py
```
