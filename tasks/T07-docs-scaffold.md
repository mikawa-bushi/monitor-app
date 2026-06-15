# T07: ドキュメント・scaffold 更新

先に `tasks/00-overview.md` を読むこと。本タスクは Wave 3
(T01/T02/T04/T05/T06 完了済み。実装されたコードを読んで正確に書くこと。
T03 は並列実行中 — CLI `sync-sources` と watcher 連携は tasks/T03-watcher-cli.md の
仕様を正として記述してよい)。

## 所有ファイル(これ以外は変更禁止)

- `docs/integrations_v2_2.md`(新規)
- `README.md` / `README_ja.md`(セクション追記のみ)
- `monitor_app/scaffold/config.py`(コメント例の追記のみ)
- `CHANGELOG.md`(Unreleased セクション追記)
- `tests/test_scaffold_config.py`(新規)

## やること

### 1. `docs/integrations_v2_2.md`(新規・日本語)

既存の `docs/redesign_v2.md` / `docs/feature_expansion_v2.1.md` の文体に合わせ、
以下を含む設計+利用ドキュメントを書く:

- 目的: procdiag / netdiag / network-checker の出力を宣言だけで可視化する
- アーキテクチャ: `SourceDef`(sqlite/jsonl/json)→ コネクタ → `_ingest_state`
  カーソル永続化 → IngestWatcher 統合、というデータフロー(実装を読んで正確に)
- `SourceDef` 全フィールドのリファレンス(kind 別の必須/不可の組み合わせ表)
- レコード変換規則(mapping ドット記法・list/dict 平坦化・型変換とスキップ)
- 各プリセットの使い方: 実装済みの `attach()` signature を**コードから転記**し、
  生成されるテーブル/ビュー/KPI/アラートの一覧表
- 運用ガイド: `MONITOR_INGEST_WATCH=1` での常時取り込み、`monitor-app sync-sources`
  での手動/cron 同期、ツール側は無改造(pull 型・読み取り専用)であること
- 制約: プリセットのビュー SQL は SQLite 方言前提。MySQL/PostgreSQL で使う場合は
  ビューを自前定義する必要がある

### 2. README 追記

README_ja.md(と README.md に英語で同内容)に「外部ツール連携
(integrations)」の短いセクションを追加。3 行プリセット例と
docs/integrations_v2_2.md へのリンク。長くしない(各 README 30 行以内の追記)。

例(実装に合わせて調整):

```python
from monitor_app.integrations import procdiag, netdiag, network_checker

config = procdiag.attach(config, db_path="/opt/procdiag/data/procdiag.db")
config = netdiag.attach(config)
config = network_checker.attach(config, hosts=["plc-01", "gateway"])
```

### 3. scaffold/config.py 追記

末尾にコメントアウトされた統合例(上記 3 行スタイル+1〜2 行の説明)を追加。
**コメントのみ**で、scaffold 生成直後の動作を一切変えないこと。

### 4. CHANGELOG.md

Unreleased(なければ新設)に v2.2 機能として: sources 宣言、3 コネクタ、
sync-sources CLI、3 プリセット、を既存の書式で追記。

### 5. tests/test_scaffold_config.py(新規)

- `monitor_app/scaffold/config.py` を `settings.loader.load_config` で読み込み、
  有効な MonitorConfig が得られること(scaffold 追記が壊れていないことの回帰テスト)
- docs/integrations_v2_2.md に記載した attach() の使用例コードブロックが実際に
  動くこと(プリセット 3 つを scaffold 由来 config に attach して検証が通る)

## 完了条件

```bash
.venv/bin/python -m pytest tests/test_scaffold_config.py -q
.venv/bin/python -m pytest -q
.venv/bin/black --check monitor_app/scaffold/config.py tests/test_scaffold_config.py
```
