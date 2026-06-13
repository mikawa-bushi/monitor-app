# U04: ビューページ — グラフ主役・折りたたみテーブル・数値整形

先に `tasks/U00-overview.md` を読むこと。本タスクは Wave 2(U01 完了済み)。

## 所有ファイル(これ以外は変更禁止)

- `monitor_app/web/templates/table.html`
- `monitor_app/web/static/js/app.js`
- `monitor_app/web/static/js/chart-view.js`
- `tests/test_ui_viewpage.py`(新規)

並列注意: base.html / app.css は U02 が、index.html / dashboard.js は U03 が
同時改修中。`.table-collapse` の CSS は U02 が app.css に用意する(クラス名は
U00 と本ファイルの記載が契約)。app.css に依存する見た目の assert は書かない。

## やること

### 1. table.html — チャート主役化(A3)

- chart 付きビュー(`has_chart`): チャートカードを最上部・主役に。テーブルは
  その下の `<details class="table-collapse">`(**既定 closed**)に入れ、
  `<summary>` は「データテーブル」+ 行数バッジ(`<span id="row-count">`、
  app.js が更新)。
- chart なしビュー: 従来どおりテーブルを直接表示(details で包まない。
  Jinja の if で分岐)。
- **`data-view="{{ view_name }}"` 属性と既存の要素 id(`thead-row` / `tbody` /
  `chart` / `chart-config`)は必ず維持**(app.js・既存テスト・U03 が参照)。
- CSV / xlsx エクスポートリンク等、既存の機能要素は位置を変えてよいが削らない。

### 2. app.js — 数値整形(C2)+ 行数バッジ + 空状態(C3)

- レンダリング時、U00 §7 の契約どおり非整数の number のみ
  `toLocaleString("ja-JP", {maximumFractionDigits: 2})`。整数・文字列・null は
  従来どおり。
- `#row-count` 要素があれば描画ごとに「全 N 行」を更新。
- 0 行のとき(既存の空行表示を拡張): 「データがありません —
  ソース同期(monitor-app sync-sources)またはツールの稼働状況を確認して
  ください」を表示。
- ペイロード契約コメント(ファイル冒頭)も実態に合わせて更新。

### 3. chart-view.js — compact モード(U00 §4)+ テーマ追従

- `create(canvas, cfg, opts)` に `opts`(省略可)を追加。`{compact: true}` で
  凡例 off / 軸タイトル off / 目盛り上限 x:6, y:5 / pointRadius 0。
  2 引数の既存呼び出しの描画結果は変えない。
- グリッド/目盛り色を `getComputedStyle(document.documentElement)
  .getPropertyValue("--chart-grid" / "--chart-tick")` から読む(空なら現行の
  定数にフォールバック)。`document` の `monitor:themechange` を listen して
  色を再適用し `chart.update("none")`。

### 4. tests/test_ui_viewpage.py(新規)

- chart 付きビューのページ: `<details` と `row-count` が含まれ、
  `data-view` 属性が維持されている
- chart なしビューのページ: `<details` で包まれて**いない**
- `chart-config` JSON が引き続き埋め込まれる
- 既存 `tests/test_pages.py::test_table_page`(`orders_summary` が本文に
  含まれる)が通ることを確認(自分では編集しない)

## 完了条件

```bash
.venv/bin/python -m pytest tests/test_ui_viewpage.py tests/test_pages.py -q
.venv/bin/python -m pytest -q   # 自分の所有ファイル起因の失敗ゼロ
node -c monitor_app/web/static/js/app.js && node -c monitor_app/web/static/js/chart-view.js
.venv/bin/black --check tests/test_ui_viewpage.py
```
