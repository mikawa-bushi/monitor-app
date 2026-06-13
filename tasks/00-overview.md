# 外部ツール出力の可視化機能 — 実装計画(v2.2)

procdiag(host-check-tool)・netdiag / network-checker(network-check-tool)の出力を
monitor-app で宣言的に可視化する。2層構成:

1. **コア**: `MonitorConfig.sources` による宣言的データソース(sqlite / jsonl / json)
2. **プリセット**: `monitor_app/integrations/` にツール別の設定フラグメント生成器

## ウェーブ(並列実行単位)と依存関係

| Wave | タスク | 内容 |
|---|---|---|
| 1 | T01 | 基盤: SourceDef・IngestStateStore・integrations.merge |
| 2 | T02, T04, T05, T06 | コネクタ実装 / 3 プリセット(T01 完了後に並列) |
| 3 | T03, T07 | watcher 一般化+CLI+配線 / ドキュメント+scaffold |
| 4 | — | 統合検証(全テスト・Black・スモーク) |

## 全タスク共通ルール

- **自タスクの「所有ファイル」以外は変更禁止**(読むのは自由)。並列実行のため厳守。
- 既存コードのスタイルに従う: 日本語 docstring、`from __future__ import annotations`、
  型ヒント必須。コメントは「コードで表せない制約」のみ。
- 新規依存パッケージの追加禁止(標準ライブラリ+既存依存のみ)。
- フォーマット: `.venv/bin/black <自分の所有ファイル>`(black==24.10.0、インストール済み)。
- テスト実行: `.venv/bin/python -m pytest tests/<自分のテスト>.py -q`。
  既存テストを壊さないこと: `.venv/bin/python -m pytest -q` が最後に全件パスすること。
- git コミットはしない(ワーキングツリーに変更を残すだけ)。
- ツールのリポジトリ(host-check-tool / network-check-tool)は参照不可。必要な
  スキーマ・サンプルはすべて各ゴールファイルに記載済み。

## 凍結済みインターフェース契約

実装者は以下の契約を**変更せずに**実装する。疑問があっても契約を勝手に変えない。

### SourceDef(`monitor_app/settings/declarative.py`、T01 が実装)

```python
class SourceDef(BaseModel):
    """外部データソース宣言。MonitorConfig.sources の値。"""
    kind: Literal["sqlite", "jsonl", "json"]
    path: str                            # 対象ファイル。~ はコネクタ側で展開
    table: str                           # 取り込み先テーブル(tables に必須)
    # --- kind="sqlite" ---
    source_table: str | None = None      # 取得元テーブル(query 省略時は必須)
    query: str | None = None             # カスタム SELECT。:wm 名前付きパラメータ可
    watermark_column: str | None = None  # 増分取り込みキー列
    # --- kind="json" ---
    record_path: str | None = None       # レコード配列までのドット区切りパス
    # --- 共通 ---
    mapping: Dict[str, str] = Field(default_factory=dict)  # ソースキー → 列名
    mode: Literal["append", "replace"] = "append"
```

バリデーション(model_validator):
- sqlite: `source_table` か `query` のどちらか必須。`source_table`・`watermark_column` は
  識別子 `^[A-Za-z_][A-Za-z0-9_]*$` のみ許可(SQL 文字列組み立てに使うため)。
- jsonl: `mode="append"` のみ。`watermark_column` 不可。
- json: `watermark_column` 不可。
- `watermark_column` 指定時は `mode="append"` のみ。

`MonitorConfig` に `sources: Dict[str, SourceDef] = Field(default_factory=dict)` を追加し、
`source.table` が `tables` に存在することを検証する。

### IngestStateStore(`monitor_app/db/ingest_state.py`、T01 が実装)

```python
class IngestStateStore:
    """ソースごとの取り込みカーソルを monitor-app 側 DB に永続化する。"""
    def __init__(self, db: Database) -> None: ...
    def create(self) -> None                      # _ingest_state を必要なら作成
    def get(self, source: str) -> str | None: ...
    def set(self, source: str, cursor: str) -> None   # UPSERT 相当
```

テーブル: `_ingest_state(source TEXT 主キー, cursor TEXT, updated_at TEXT)`。
cursor の中身はコネクタ依存の不透明文字列(watermark 値・バイトオフセット等)。

### integrations.merge(`monitor_app/integrations/__init__.py`、T01 が実装)

```python
def merge(config: MonitorConfig, *, tables=None, views=None, kpis=None,
          alerts=None, sources=None) -> MonitorConfig:
    """フラグメントを合成した新しい MonitorConfig を返す(元は変更しない)。

    tables/views/kpis/sources の名前衝突は ValueError。合成後に
    MonitorConfig.model_validate(...model_dump()...) で全バリデータを再実行する。
    """
```

### コネクタ(`monitor_app/services/connectors.py`、T02 が実装)

```python
class SourceConnector:   # 基底クラス
    def __init__(self, name: str, sdef: SourceDef, config: MonitorConfig,
                 registry: TableRegistry, db: Database,
                 state: IngestStateStore) -> None: ...
    def poll_once(self) -> int   # 取り込んだ行数。変更なしは 0。例外は呼び出し側が捕捉

class SqliteSourceConnector(SourceConnector): ...
class JsonlSourceConnector(SourceConnector): ...
class JsonSourceConnector(SourceConnector): ...

def build_connectors(config: MonitorConfig, registry: TableRegistry, db: Database,
                     state: IngestStateStore) -> Dict[str, SourceConnector]: ...
```

レコード変換規則(全 kind 共通):
- `mapping` が空ならソースキーをそのまま列名に使う。
- `mapping` のキーはドット記法でネスト走査(dict はキー、list は整数インデックス。
  例 `"detected_at.0"`)。
- 値が list → `"; ".join(str(x))`、dict → `json.dumps(..., ensure_ascii=False)`。
- 型変換は `TableDef.column_type()` + `services.coercion.coerce`。失敗行はスキップして
  logger.warning(CsvImporter と同じ方針)。テーブル定義にない列は捨てる。
- 挿入は sqlalchemy `insert`。`mode="replace"` は同一トランザクションで
  `delete(table)` → `insert`。

### IngestWatcher 拡張(T03 が実装)

```python
class IngestWatcher:
    def __init__(self, importer: CsvImporter, interval: float, keep: bool,
                 connectors: Dict[str, SourceConnector] | None = None) -> None: ...
    def poll_once(self, import_on_change: bool = True) -> List[str]:
        """変更を検出した CSV テーブル+取り込みが発生したソース名のリストを返す。"""
```

## 検証環境

- venv: `/home/juntanishitani/projects/gitpjt/monitor-app/.venv`(Python 3.11.10、
  `pip install -e .` 済み、pytest / httpx / black==24.10.0 入り)
- 実行はリポジトリルート(`/home/juntanishitani/projects/gitpjt/monitor-app`)から。
