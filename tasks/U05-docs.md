# U05: ドキュメント・scaffold 更新(UI 刷新 v2.3)

先に `tasks/U00-overview.md` を読むこと。本タスクは Wave 3
(U01〜U04 完了済み。**実装されたコードを読んで**正確に書くこと)。

## 所有ファイル(これ以外は変更禁止)

- `docs/ui_redesign_v2_3.md`(新規)
- `README.md` / `README_ja.md`(セクション追記・更新のみ)
- `CHANGELOG.md`(Unreleased 追記)
- `monitor_app/scaffold/config.py`(group / dashboard の例の追加)
- `tests/test_scaffold_config.py`(scaffold 変更の回帰)

## やること

### 1. docs/ui_redesign_v2_3.md(日本語)

既存 docs の文体で: 刷新の動機(リンク一覧 → ダッシュボード)、
ダッシュボードの構成(ステータス行 / KPI / ミニチャート / 一覧)、
`ViewDef.group` と `DashboardConfig` のリファレンス、サイドバーナビ、
ダークテーマ(localStorage `"monitor-theme"`、`data-theme` 属性、
`--chart-*` 変数)、ビューページの折りたたみテーブルと数値整形規則、
スコープ外にしたもの(KPI スパークライン — 履歴 API が必要なため見送り)。

### 2. README 更新

README_ja.md / README.md のスクリーンショット説明や機能一覧に
ダッシュボード・ダークテーマ・グループナビを反映(各 20 行以内の追記・修正)。

### 3. scaffold/config.py

- 既存ビューに `group=` の実例を 1 つ付ける(例: products_view に
  `group="販売"`)。
- `MonitorConfig(...)` に `dashboard=DashboardConfig(columns=2)` の
  コメント付き実例(コメントアウトでなく実際に動く形で入れてよいが、
  検証エラーにならないこと — views は chart 付きのみ指定可)。

### 4. tests/test_scaffold_config.py

scaffold の config が load でき、group / dashboard が期待どおりかの assert を
追加(既存テストの形式に合わせる)。

## 完了条件

```bash
.venv/bin/python -m pytest tests/test_scaffold_config.py -q
.venv/bin/python -m pytest -q
.venv/bin/black --check monitor_app/scaffold/config.py tests/test_scaffold_config.py
```
