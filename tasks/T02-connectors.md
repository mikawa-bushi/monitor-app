# T02: コネクタ — sqlite / jsonl / json ソースの取り込み実装

先に `tasks/00-overview.md` を読むこと。本タスクは Wave 2(T01 完了済み。
`SourceDef`・`IngestStateStore`・`MonitorConfig.sources` は実装済みなので import して使う)。

## 所有ファイル(これ以外は変更禁止)

- `monitor_app/services/connectors.py`(新規)
- `tests/test_connectors.py`(新規)

## やること

00-overview.md の「コネクタ」契約どおりに `connectors.py` を実装する。
参考実装として `monitor_app/services/importer.py`(CsvImporter)の構造・ログ方針に倣う。
logger 名は `"monitor_app.connectors"`。

### 共通(基底クラス `SourceConnector`)

- `path` は `Path(sdef.path).expanduser()` で解決。ファイルが存在しなければ
  poll_once は 0 を返す(エラーにしない。ツール未稼働は正常系)。
- レコード変換は 00-overview.md の規則どおり(ドット記法走査・list/dict の平坦化・
  coerce・未知列破棄・失敗行スキップ+logger.warning)。
- 変換と挿入は基底クラスの共通メソッドにまとめ、サブクラスは
  「変更検出と raw レコード列挙」だけを担う構成を推奨。

### SqliteSourceConnector

- 読み取り専用接続: `sqlite3.connect(f"file:{path}?mode=ro", uri=True)`、
  `row_factory = sqlite3.Row`。ツール側 DB には絶対に書かない。
- SQL の決定:
  - `query` 指定時はそれを使う(:wm を含むかは設定者の責任)。
  - 未指定なら `SELECT * FROM {source_table}`。`watermark_column`(=wc)指定時は
    `SELECT * FROM {source_table} WHERE (:wm IS NULL OR {wc} > :wm) ORDER BY {wc}`。
  - 識別子は T01 のバリデーションで保証済み(f-string 埋め込みでよい)。
- watermark あり: 実行時パラメータ `{"wm": state.get(name)}`。取り込み後、
  読んだ行の wc 最大値を `str()` にして `state.set(name, ...)`。
  cursor が進まない限り再取り込みしない(増分)。
- watermark なし: ファイルの `mtime:size` を cursor にし、変化時のみ全件を
  `mode` に従って取り込む(replace=洗い替え / append=追記)。
- DB がロック中・破損などの `sqlite3.Error` は logger.warning して 0 を返す
  (監視ループを殺さない)。

### JsonlSourceConnector

- cursor = 消費済みバイトオフセット(int を str 化)。
- poll: `stat()` で size を見る。size < offset → ローテーション/truncate とみなし
  offset=0 から読み直す。size == offset → 0 を返す。
- offset から読み、**末尾に改行のない未完了行は消費しない**(次回に持ち越す)。
- 1 行 = 1 JSON オブジェクト。`json.JSONDecodeError` の行はスキップして warning。
- 取り込み成功後に新しいオフセットを保存。

### JsonSourceConnector

- cursor = `f"{mtime_ns}:{size}"`。変化なしなら 0。
- ファイル全体を `json.load`。`record_path`(ドット区切り)を辿って list を得る。
  単一 dict なら `[dict]` に包む。record_path 先が見つからない/型不一致は warning して 0。
- `mode` に従い取り込み(findings のような「毎回再生成されるファイル」は replace 想定)。

### build_connectors

`config.sources` を回して kind に応じたコネクタを生成して返すだけ。

## tests/test_connectors.py

`tmp_path` でソースファイルを合成し、in-memory ではなく
`Database(f"sqlite:///{tmp_path}/app.db")` を使う(state の永続性確認のため)。
ヘルパで `MonitorConfig` + `TableRegistry` + `registry.create_all(db)` +
`IngestStateStore(db).create()` を組み立てる。

最低限カバーすること:
- sqlite + watermark: 初回全件 → 行追加 → poll で増分のみ → 変化なしで 0
- sqlite watermark なし + replace: ファイル更新で洗い替え
- jsonl: 2 行 → poll(2)→ 1 行追記 → poll(1)→ 変化なし 0 → truncate 後の再読込
- jsonl: 未完了行(改行なし)は消費されず、改行追記後に取り込まれる
- json + record_path + replace: 初回取り込み → ファイル再生成で行が置き換わる
- mapping のドット記法(`"detected_at.0"` → list インデックス)と list 値の平坦化
- 型変換失敗行のスキップ(他の行は入る)
- path 不存在 → 0(例外なし)

## 完了条件

```bash
.venv/bin/python -m pytest tests/test_connectors.py -q
.venv/bin/python -m pytest -q
.venv/bin/black --check monitor_app/services/connectors.py tests/test_connectors.py
```
