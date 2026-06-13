# T01: 基盤 — SourceDef・IngestStateStore・integrations.merge

先に `tasks/00-overview.md` を読むこと。本タスクは Wave 1(単独実行)。
後続の全タスクが本タスクの成果物を import するため、**契約どおりの正確な実装**が最優先。

## 所有ファイル(これ以外は変更禁止)

- `monitor_app/settings/declarative.py`(SourceDef 追加・MonitorConfig 拡張のみ)
- `monitor_app/db/ingest_state.py`(新規)
- `monitor_app/integrations/__init__.py`(新規)
- `tests/test_sources_config.py`(新規)

## やること

### 1. `SourceDef` を declarative.py に追加

00-overview.md の契約どおり。既存モデルのスタイル(日本語 docstring・フィールド横の
簡潔なコメント)に合わせる。バリデーションエラーのメッセージは既存に倣い日本語。

`MonitorConfig` に `sources: Dict[str, SourceDef] = Field(default_factory=dict)` を追加。
既存の `_validate_relations` バリデータ(または新規バリデータ)で
「`sources[name].table` が `tables` に存在しない場合 ValueError」を実装。
エラーメッセージ例: `sources['procdiag_sys']: 取り込み先テーブル 'xxx' が tables に存在しません`

### 2. `monitor_app/db/ingest_state.py`(新規)

00-overview.md の契約どおり `IngestStateStore` を実装。

- 独自の `MetaData` に sqlalchemy `Table("_ingest_state", ...)` を定義
  (`TableRegistry` には登録しない。ユーザー定義テーブルと混ぜない)。
- `create()` は `metadata.create_all(db.engine)`(冪等)。
- `set()` は同一トランザクションで delete → insert(方言非依存の UPSERT)。
  `updated_at` は `datetime.now(timezone.utc).isoformat()`。
- `db.connect()`(`monitor_app/db/engine.py`)を使う。

### 3. `monitor_app/integrations/__init__.py`(新規)

00-overview.md の契約どおり `merge()` を実装。パッケージ docstring には
「ツール別プリセット(procdiag / netdiag / network_checker)を提供するパッケージ。
各モジュールの attach() でフラグメントを config に合成する」旨を書く
(モジュール本体は別タスクが追加するので import しない)。

実装方針:
```python
new = config.model_copy(deep=True)
# 衝突チェック: tables/views/kpis/sources の既存キーと重複したら ValueError
new.tables.update(tables or {})
new.views.update(views or {})
new.kpis.update(kpis or {})
new.alerts = list(new.alerts) + list(alerts or [])
new.sources.update(sources or {})
return MonitorConfig.model_validate(new.model_dump())
```
`model_dump()` → `model_validate()` の往復で全バリデータ(外部キー・アラート参照・
sources.table 検証)を再実行させるのが狙い。

### 4. `tests/test_sources_config.py`(新規)

最低限カバーすること:
- SourceDef: sqlite で source_table も query もない → ValidationError
- SourceDef: source_table / watermark_column に識別子以外(`"a; DROP"` 等)→ ValidationError
- SourceDef: jsonl + mode="replace" → ValidationError
- MonitorConfig: sources.table が tables にない → ValidationError、ある → OK
- IngestStateStore: create → get(None)→ set → get → set(上書き)→ get(in-memory
  SQLite: `Database("sqlite://")`)
- merge: 正常合成(tables/views/sources が増え、元 config は不変)
- merge: テーブル名衝突 → ValueError
- merge: 合成後の再検証が効くこと(例: 存在しないビューを指す AlertRule を渡すと
  ValidationError)

## 完了条件

```bash
.venv/bin/python -m pytest tests/test_sources_config.py -q   # 全パス
.venv/bin/python -m pytest -q                                 # 既存含め全パス(42+新規)
.venv/bin/black --check monitor_app/settings/declarative.py \
    monitor_app/db/ingest_state.py monitor_app/integrations/__init__.py \
    tests/test_sources_config.py
```
