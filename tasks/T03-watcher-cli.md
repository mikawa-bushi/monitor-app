# T03: IngestWatcher 一般化・CLI sync-sources・アプリ配線

先に `tasks/00-overview.md` を読むこと。本タスクは Wave 3
(T01 基盤・T02 コネクタは実装済み。`services/connectors.py` を import して使う)。

## 所有ファイル(これ以外は変更禁止)

- `monitor_app/services/ingest_watcher.py`
- `monitor_app/cli.py`
- `monitor_app/main.py`
- `tests/test_ingest_sources.py`(新規)

## やること

### 1. IngestWatcher の一般化

00-overview.md の契約どおり、コンストラクタに
`connectors: Dict[str, SourceConnector] | None = None` を追加(既定 None で後方互換)。

- `poll_once()`: 既存の CSV 処理に加え、各コネクタの `poll_once()` を呼ぶ。
  取り込み行数 > 0 だったソース名を戻り値リストに加える。
- コネクタの例外は捕捉して `logger.warning`(1 ソースの失敗で他を止めない。
  watcher 本体のループも死なせない)。
- 既存の非同期ループ(`start`/`stop`)はそのまま流用。

### 2. main.py の配線

現状の create_app での watcher / importer の組み立て箇所を確認し、次を追加:

- `config.sources` が空でなければ:
  - `IngestStateStore(db)` を作り `create()`(`init_db` フラグに関係なく冪等なので常に)
  - `build_connectors(config, registry, db, state)` でコネクタ群を生成
  - watcher に渡す(watcher 自体の起動条件 `settings.ingest_watch` は従来どおり)
- `app.state` に connectors を載せる(テスト・デバッグ用)。既存の app.state の
  命名スタイルに合わせること。

### 3. CLI `sync-sources` コマンド

`import-csv` コマンドに倣い、全ソースを 1 回同期する Typer コマンドを追加:

```
monitor-app sync-sources [--config config.py]
```

- config 読込 → Database / TableRegistry / create_all → IngestStateStore.create()
  → build_connectors → 各 poll_once()
- ソースごとに `  {name}: {n} 行` を echo、例外時は赤で `  {name}: エラー — {exc}` を
  表示して継続。最後に合計を緑で表示(import-csv の出力スタイルに合わせる)。
- `config.sources` が空なら「sources が定義されていません」と表示して正常終了。

### 4. tests/test_ingest_sources.py(新規)

E2E 観点(conftest.py のフィクスチャ流儀に倣う。ファイル DB は tmp_path):

- sources 付き MonitorConfig で `create_app` → `_ingest_state` テーブルが存在し、
  `app.state` にコネクタが載っている
- watcher.poll_once(): CSV 変更とソース取り込みが同時に報告される
  (CSV は既存テストの流儀で `csv_dir` を tmp に向ける)
- コネクタが例外を投げても poll_once が他ソースを処理して戻る
  (モック/ダミーコネクタで可)
- API 経由確認: jsonl ソースに行を書く → poll_once → `/api/views/...` または
  CRUD API で行が見える
- CLI: `typer.testing.CliRunner` で sync-sources を叩き、取り込み件数の出力と
  exit code 0 を確認(sources 空のケースも)

## 完了条件

```bash
.venv/bin/python -m pytest tests/test_ingest_sources.py -q
.venv/bin/python -m pytest -q          # 既存・他タスクのテスト含め全パス
.venv/bin/black --check monitor_app/services/ingest_watcher.py monitor_app/cli.py \
    monitor_app/main.py tests/test_ingest_sources.py
```
