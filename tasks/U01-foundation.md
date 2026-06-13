# U01: 基盤 — group / DashboardConfig / ページコンテキスト

先に `tasks/U00-overview.md` を読むこと。本タスクは Wave 1(単独実行)。
Wave 2 の 3 タスクがこの契約に依存するため、契約(§1・§2)を一字一句守ること。

## 所有ファイル(これ以外は変更禁止)

- `monitor_app/settings/declarative.py`(ViewDef.group / DashboardConfig /
  MonitorConfig.dashboard の追加のみ)
- `monitor_app/api/routers/pages.py`
- `monitor_app/integrations/procdiag.py` / `netdiag.py` / `network_checker.py`
  (各ビューへの `group=` 追加のみ)
- `tests/test_ui_config.py`(新規)

## やること

### 1. declarative.py — U00 §1 の契約どおり

- `ViewDef.group: str = ""` を追加(コメント: ナビ・一覧でのセクション名)。
- `DashboardConfig` を追加し、`MonitorConfig.dashboard` に既定値付きで配線。
- `MonitorConfig` のバリデータに追加: `dashboard.views` の各名前が
  `views` に存在しない、または chart を持たない場合は ValueError
  (メッセージは既存スタイルの日本語)。`columns` の 1〜4 制約は
  `Field(ge=..., le=...)` か validator どちらでも可。

### 2. pages.py — U00 §2 の契約どおり

- `_nav_groups(config) -> list[dict]` ヘルパを作り、`_ctx()` で全ページに
  `nav_groups` を渡す。グループの順序は config.views の出現順。
  `group==""` のビューはグループが 1 つでも存在する場合のみ「その他」に集約。
  全ビューが無グループなら `{"group": "", "views": [...]}` の単一要素。
- `index()`: `dashboard_json` と `list_views` を契約どおり追加
  (`json.dumps(..., ensure_ascii=False)`、ChartDef は `model_dump()`)。
- `show_table()`: `current_view=view_name` を追加。

### 3. プリセットに group を設定

- procdiag の全ビュー: `group="ホストリソース"`
- netdiag の全ビュー: `group="ネットワーク診断"`
- network_checker の全ビュー: `group="ping 監視"`

### 4. tests/test_ui_config.py(新規)

- DashboardConfig: 存在しないビュー名 → ValidationError /
  chart なしビュー名 → ValidationError / columns=5 → ValidationError
- nav_groups: グループ付き+無グループ混在の config で index HTML に
  各 group 名が出る(現段階では context 経由の確認として、
  `request.app.state.templates` を介さず pages._nav_groups() を直接呼んで
  構造を assert するのが確実)
- dashboard_json: chart 付きビューだけが入る(既定)/ dashboard.views 指定時は
  その順序で入る
- プリセット: attach 後の各ビューに期待した group が付いている

注意: index.html はまだ旧構造(U03 が並列改修中)。**テンプレート HTML の
文言には依存しない**テストにすること(_nav_groups / context 構築ロジックの
単体テスト+config 検証中心)。

## 完了条件

```bash
.venv/bin/python -m pytest tests/test_ui_config.py -q
.venv/bin/python -m pytest -q          # 既存 194 件を壊さない
.venv/bin/black --check monitor_app/settings/declarative.py monitor_app/api/routers/pages.py \
    monitor_app/integrations/procdiag.py monitor_app/integrations/netdiag.py \
    monitor_app/integrations/network_checker.py tests/test_ui_config.py
```
