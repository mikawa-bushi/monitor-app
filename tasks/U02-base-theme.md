# U02: ベースレイアウト — サイドバーナビ・ダークテーマ・質感

先に `tasks/U00-overview.md` を読むこと。本タスクは Wave 2(U01 完了済み。
`nav_groups` / `current_view` は全ページのコンテキストに入っている)。

## 所有ファイル(これ以外は変更禁止)

- `monitor_app/web/templates/base.html`
- `monitor_app/web/static/css/app.css`
- `monitor_app/web/static/js/theme.js`(新規)
- `tests/test_ui_base.py`(新規)

並列注意: index.html は U03 が、table.html / app.js / chart-view.js は U04 が
同時改修中。これらに依存する assert を書かないこと。

## やること

### 1. サイドバーナビ(base.html + app.css)

- `<body>` を「左サイドバー+右メインカラム」のレイアウトに変更。
  サイドバー: ブランド(リンク先 `/`)、`nav_groups` をセクション見出し+
  ビューリンク(`/table/<name>`、表示は title)で描画。
  `current_view`(`| default("")`)と一致するリンクに `aria-current="page"` を
  付け、CSS で強調。kiosk へのリンク(`has_kiosk` は index 以外で未定義のため
  `url_for('kiosk')` 固定リンクを常に出してよい)。
- `group == ""` のセクションは見出し「その他」(単一無名グループなら見出しなし)。
- レスポンシブ: 900px 以上はサイドバー常設、未満はヘッダーのハンバーガー
  ボタンで開閉(JS は base.html 内の小さなインラインスクリプトで可)。
- 既存の `.site-header` はメインカラム上部のバーとして残す(テーマトグル・
  ページタイトル用)。footer は維持。
- form.html / table.html / index.html は base.html を継承しているので、
  **content ブロックの構造は変えない**こと(子テンプレートに手を入れない)。

### 2. ダークテーマ(app.css + theme.js + base.html)

U00 §3 の契約どおり:
- `:root` に `--chart-grid` / `--chart-tick` を追加し、`[data-theme="dark"]` で
  全 CSS 変数(bg/surface/text/muted/border/accent/shadow/chart 系)を上書き。
  ダーク配色の目安: bg `#0f172a` / surface `#1e293b` / text `#e2e8f0` /
  muted `#94a3b8` / border `#334155` / accent `#60a5fa`。
- `theme.js`: 同期実行(`<head>` 内で読込)。localStorage `"monitor-theme"` が
  なければ `prefers-color-scheme` を初期値に。`window.MonitorTheme.toggle()` を
  公開し、トグル時に保存+ `data-theme` 更新+ `monitor:themechange` dispatch。
- base.html ヘッダーに `<button id="theme-toggle">`。クリックで toggle、
  アイコンは現在テーマで 🌙/☀️ を切替。
- bootstrap の色クラス(bg-danger 等)はダークでもそのまま使う(視認性は十分)。

### 3. 質感(app.css)

- テーブル: `td, th { font-variant-numeric: tabular-nums; }`、行 hover の背景、
  ヘッダーの背景を surface より一段濃く。
- KPI カード: `status` クラス(ok / bad / neutral — kpis.js が付与)で
  左ボーダー色(ok=緑 / bad=赤 / neutral=アクセント)。値は大きく
  (2rem 前後)、`.kpi-unit` は muted の小さめ表示。
- `.chart-card` の高さを 360px → 440px に(グラフ主役化。U04 が前提にする)。
- `<details class="table-collapse">` 用スタイル(summary をリンク風に、
  開閉アイコン)。U04 が table.html で使う。
- ダッシュボード固有のスタイルは**書かない**(U03 が dashboard.css に書く)。

### 4. tests/test_ui_base.py(新規)

conftest の `client` フィクスチャ(グループなし config)で:
- `/` と `/table/users_view` の両方にサイドバー nav 要素(例: `id="sidebar"`)と
  テーマトグルボタンが含まれる
- ビューリンクが title(「ユーザー」)で表示される
- group 付き config を組んで(MonitorConfig を直接構築)グループ見出しが出る
- `theme.js` が `/static/js/theme.js` で 200 を返す

## 完了条件

```bash
.venv/bin/python -m pytest tests/test_ui_base.py -q
.venv/bin/python -m pytest -q   # 自分の所有ファイル起因の失敗ゼロ
node -c monitor_app/web/static/js/theme.js
.venv/bin/black --check tests/test_ui_base.py
```
