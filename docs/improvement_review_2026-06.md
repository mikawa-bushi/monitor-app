# monitor-app コード改善レビュー(2026-06-13)

対象: `monitor_app/` 全レイヤ(settings / db / services / api / integrations / web)+ テスト・CI。
観点: **保守性・エラーハンドリング・UX・処理速度** の4軸。各項目に「場所(`file:line`)/根拠/改善案」を付す。

> 前提として土台は良好です(末尾「良い点」参照)。以下は伸びしろの洗い出しであり、全部を直す必要はありません。**重大度の高い E1/E2/U1 から着手**することを推奨します。

---

## 重大度サマリ

| ID | 区分 | 重大度 | 一行要約 |
|----|------|--------|----------|
| **E1** | エラー/正確性 | 🔴 重大 | append 監視で CSV が変更されるたび全行を重複取り込み |
| **E2** | エラー/正確性 | 🔴 重大 | 取り込み例外で監視ループが無言で恒久停止 |
| **U1** | UX | 🔴 重大 | `protect_reads=True` で同梱 Web UI が全滅(401) |
| **E5** | エラー/正確性 | 🟠 中 | AlertEngine の共有状態が非スレッドセーフ(二重通知/KeyError) |
| **E6** | エラー/正確性 | 🟠 中 | アラートが「画面を見ている時だけ」評価される |
| **E7** | エラー/正確性 | 🟠 中 | 同期ブロッキング通知がワーカースレッドを最大10秒占有 |
| **E3/E4** | エラー/正確性 | 🟠 中 | CSV エンコーディング固定 / 空CSVで全データ消去 |
| **V1/V2/V3** | 処理速度 | 🟠 中 | 全件SELECT・SSEのクライアント毎再実行・カード毎ポーリング |
| **M1** | 保守性 | 🟠 中 | フロントのテーブル描画ロジックが3重複 |
| その他 | — | 🟡 低 | E8/E9 / M2–M5 / U2–U4 / V4–V6 / S1–S3 |

---

## 1. エラーハンドリング・正確性

### 🔴 E1. append 監視で CSV を変更するたびに全行を重複取り込み
- **場所**: `services/ingest_watcher.py:51-58`(`poll_once`)→ `services/importer.py:75-106`(`_import_one`)
- **根拠**: `ingest_mode="append"`(= `keep=True`)で監視中、CSV の mtime が変わると `_import_one(..., keep=True)` がファイル**全体**を読み直して全行を再 INSERT する。1 行追記しただけでもファイル全体が再追加され、ポーリングのたびに重複が雪だるま式に増える。append は時系列用途でこそ使う設定なのに、その用途で破綻する。JSONL/SQLite コネクタはオフセット/ウォーターマークで増分対応しているのに、CSV 監視だけ全件方式で不整合。
- **改善案**: CSV も行オフセット(JSONL コネクタと同方式)で増分取り込みするか、append 監視時は前回取り込み済みの行数を超えた分のみ挿入する。当面の回避策としては、追記系データは CSV ではなく `sources`(jsonl/sqlite)経由に誘導し、ドキュメントへ「CSV の append 自動監視は重複する」と明記する。

### 🔴 E2. 取り込み例外で監視ループが無言で恒久停止
- **場所**: `services/ingest_watcher.py:73-84`(`_run`)
- **根拠**: `_run` は `asyncio.CancelledError` しか捕捉しない。`poll_once` 内の CSV 取り込み部(`_import_one`)は個別 try で囲まれていない(`try` が掛かっているのはコネクタ呼び出しループだけ)。よって CSV のデコード失敗(E3)や想定外の例外が起きると `await asyncio.to_thread(self.poll_once)` から伝播して `while True` を抜け、**以後の自動取り込みが完全停止**する。しかもログにも残らないため気付けない。
- **改善案**: `while` 本体を `try/except Exception` で包み、1 周期の失敗は `logger.exception` に留めて次周期へ継続する。

### 🟠 E3. CSV エンコーディングが `utf-8-sig` 固定
- **場所**: `services/importer.py:84`
- **根拠**: 日本の製造現場で扱う CSV は Excel 由来の **CP932/Shift-JIS** が多い。日本語 CSV を置くと `UnicodeDecodeError` で取り込み失敗し、E2 と重なると監視ループごと停止する。
- **改善案**: `AppSettings` に `csv_encoding`(既定 `utf-8-sig`)を追加して設定可能にする。あるいは `errors="replace"` でフォールバックしつつ警告する。

### 🟠 E4. 置換取り込みで「空CSV/全行失敗」時に既存データを全消去
- **場所**: `services/importer.py:102-106`
- **根拠**: `keep=False`(置換)では `delete(table)` を先に実行してから挿入する。CSV が空・破損・全行型変換失敗のとき、既存行を消したうえで 0 件挿入 = **全消去**になる。誤って空ファイルや壊れたファイルを置くと本番データが消える。
- **改善案**: 有効行が 0 件のときは置換を中止して warning を出す(= 1 件以上ある時だけ `delete → insert`)。`SqliteSourceConnector` の `replace` 経路(`connectors.py:155-159`)も同様。

### 🟠 E5. AlertEngine の共有状態が非スレッドセーフ
- **場所**: `services/alert_service.py:92,97`(`self._active[key] = alert` / `del self._active[key]`)
- **根拠**: `AlertEngine` は `app.state` に単一インスタンスで保持され、`evaluate_view` は同期エンドポイント(`get_view`、スレッドプール実行)と SSE(`views.py:117` の `anyio.to_thread.run_sync`)の双方から呼ばれる。さらに**同一ビューを複数クライアントが同時購読**すると並行実行になる。`_active` dict をロックなしで読み書きするため、(a) `not was_active` 判定の競合による二重通知、(b) 2 スレッドが同じ key を `del` して `KeyError` などが起こり得る。
- **改善案**: `threading.Lock` で `evaluate_view` を保護する。根本的には E6 のとおり評価をビュー取得から分離する。

### 🟠 E6. アラートが「画面を見ている時だけ」評価される
- **場所**: `api/routers/views.py:26-31`(`_build_payload` が `engine.evaluate_view` を駆動)
- **根拠**: 閾値評価は `/api/views/<v>` が叩かれた瞬間にしか走らない。kiosk/dashboard に載っていないビューや、誰も画面を開いていない夜間などは閾値を超えても**通知されない**。逆に多人数が同じビューを開くと評価が多重実行され、E5(競合)と V2(負荷)を悪化させる。
- **改善案**: アラート評価を `IngestWatcher` のようなバックグラウンド単一ループへ移し、画面側はその結果(`active_alerts()`)を購読するだけにする。これで E5・E6・E7 が同時に解消する。

### 🟠 E7. 同期ブロッキング通知がワーカースレッドを最大10秒占有
- **場所**: `services/notifiers/webhook.py:19`(urlopen timeout=5)、`notifiers/email.py:23`(smtp timeout=10)、`notifiers/line.py:18`。呼び出し元は `alert_service.py:101-117`(`_notify` / `_notify_recovery`)で、これは `evaluate_view` 内 = ビュー取得処理の中。
- **根拠**: 発火エッジで外部通知先が遅い/タイムアウトすると、そのビュー取得を処理しているスレッドプールワーカーが最大 5〜10 秒ブロックされる。複数アラートが同時に立つとワーカー枯渇でアプリ全体の応答が悪化し得る(`タイムアウトがある点は良い`が、リクエスト経路で同期実行しているのが問題)。
- **改善案**: 通知をバックグラウンドのキュー/タスクへ投げて即時 return する(評価ループ分離=E6 と併せると自然に解決)。

### 🟡 E8. パスパラメータ `record_id` が論理型へ変換されない
- **場所**: `api/routers/crud.py:35-83`(get/put/delete は `record_id: str`)→ `db/repository.py:28-77`(`pk == record_id`)
- **根拠**: 本文は `crud_service._clean_payload` で `coerce` されるが、URL 上の主キーは**文字列のまま**比較に渡される。SQLite は動的型付けで救われるが、PostgreSQL/MySQL では `integer = '5'(text)` の型不一致でエラーまたは無マッチになり得る。本文だけ変換し主キーは未変換という一貫性の欠如でもある。
- **改善案**: `crud_service` 側で `coerce(record_id, tdef.column_type(pk))` してから repository に渡す。

### 🟡 E9. フロントの fetch 失敗が画面に出ず、古い表示のまま気付けない
- **場所**: `js/kpis.js:78`、`js/dashboard.js:140-146`、`js/kiosk.js:171-173`(いずれも `if (!res.ok) return;` / catch で「前回表示維持」)
- **根拠**: KPI やダッシュボードはエラー時に黙って前回値を保持するため、サーバー障害でデータが何分も古いままでもユーザーが気付けない(現場の判断ミスにつながる)。
- **改善案**: 「最終更新時刻」表示や stale バッジ、連続失敗時の視覚インジケータを出す(`table.html` の `#status` 相当を全画面に)。

---

## 2. 保守性

### 🟠 M1. フロントのテーブル描画ロジックが3重複
- **場所**: `js/app.js:93-133`(`formatCell`/`renderHead`/`renderBody`/`cellClass`/`applyCellStyle`)、`js/kiosk.js:85-138`(`formatCell`/`renderTable`/`applyStyle`)、一部 `js/dashboard.js`
- **根拠**: `formatCell` はほぼ完全コピー。セルスタイル適用は app.js が `class` と inline style の両方、kiosk.js は `else-if` で片方のみ…と**微妙に挙動が違う**ため、片方だけ修正する事故が起きやすい。
- **改善案**: `alerts-ui.js`/`chart-view.js` と同じ `window.MonitorXxx` 公開パターンで `MonitorTable`(描画+セル整形+スタイル適用)を切り出し、3画面で共有する。

### 🟡 M2. ウォッチャがインポータの非公開メソッドを直接呼ぶ
- **場所**: `services/ingest_watcher.py:57`(`self.importer._import_one(...)`)
- **根拠**: アンダースコア始まり = 私的 API への外部依存。インポータ内部を変えるとウォッチャが壊れる。
- **改善案**: `CsvImporter.import_table(name, *, keep)` のような公開メソッドを 1 つ用意して呼ぶ。

### 🟡 M3. import 文・相対 import の並びが不統一
- **場所**: 例 `services/crud_service.py:5-7`(`from typing import` が 2 行に分割)、`services/view_service.py:12-14` と `kpi_service.py:15-16` で `..exceptions` → `..db` / `..settings` → `..db` の順がばらつく。
- **根拠**: black は通るが import 整列ルールが無いため差分ノイズと不統一が蓄積する。
- **改善案**: CI に `ruff`(または isort)を追加して import を自動整列。

### 🟡 M4. integrations プリセットが SQLite 専用 SQL に依存
- **場所**: `integrations/network_checker.py:66-81,114-120`、`integrations/procdiag.py:101-189`、`integrations/netdiag.py:118-122`
- **根拠**: `strftime('%s','now')` / `datetime('now','-1 day')` / `unixepoch` は SQLite 方言。アプリは MySQL/PostgreSQL 対応を謳う(`settings/runtime.py:22-26`)が、これらプリセットのビュー/KPI は他 DB では動かない。
- **改善案**: 各プリセットに「SQLite 前提」を明記するか、時刻関数を吸収するヘルパを設ける。

### 🟡 M5. LINE Notify は提供終了済み(2025-03-31)
- **場所**: `services/notifiers/line.py:10`(`notify-api.line.me`)
- **根拠**: LINE Notify は 2025-03-31 にサービス終了しており、このチャネルは現状ほぼ常に失敗する。死んだ統合を同梱・ドキュメント化している(`scaffold/config.py:102` のコメントにも `line` が残る)。
- **改善案**: 削除するか、LINE Messaging API への移行を案内するコメントへ差し替える。

---

## 3. UX

### 🔴 U1. `protect_reads=True` にすると同梱 Web UI が全滅する
- **場所**: 同梱 JS は一切 `X-API-Key` を送らない(`js/app.js:150`、`js/kpis.js:77`、`js/kiosk.js:154,180`、`js/dashboard.js:79,140`、`js/form.js:40`)。検証側は `api/deps.py:82-88`(`require_read_auth`)。
- **根拠**: `protect_reads=True` かつ `api_key` 設定時、画面の全データ取得が 401 になり、表は空・ステータス消灯のまま**無言で壊れる**。書き込みも同様で、`api_key` 設定時は作業者フォーム送信(`form.js`)が 401 → 「失敗しました」になる。ブラウザからキーを渡す手段が用意されていない。さらに HTML ページ自体(`api/routers/pages.py`)には認証が無いため「ガワだけ表示・中身は空」という分かりにくい状態になる。
- **改善案**: (a) UI 用に Cookie セッション/簡易ログインを設けてサーバー側でキーを補完する、または (b) 当面は「`protect_reads` は API 専用で同梱 UI とは併用不可」とドキュメント明記し、UI を使う場合の代替(リバースプロキシでの Basic 認証等)を案内する。

### 🟠 U2. SSE の一過性切断で恒久的にポーリングへ降格
- **場所**: `js/app.js:169-174`(`source.onerror`)
- **根拠**: ネットワークの瞬断でも `onerror` で即 `close()` → ポーリング固定になり、SSE に二度と戻らない。効率の良い差分配信を失う。
- **改善案**: バックオフ付き再接続を試み、一定回数連続失敗で初めてポーリングへフォールバックする。

### 🟡 U3. 入力フォームに二重送信ガードが無い
- **場所**: `js/form.js:36-58`
- **根拠**: 送信中に submit ボタンを無効化しないため、Enter 連打や二度押しで重複登録が起こり得る(作業者がタッチ操作で使う前提なら尚更)。
- **改善案**: 送信開始時に `button.disabled = true`、完了で戻す。

### 🟡 U4. ページを開いた瞬間にブラウザ通知の許可を要求
- **場所**: `js/app.js:43` → `js/alerts-ui.js:69-73`(`requestPermission`)
- **根拠**: ロード直後に許可ダイアログが出るのはユーザー体験上唐突で、拒否されやすい(拒否されると以後通知不可)。
- **改善案**: ユーザー操作(「通知を有効化」ボタン)を起点に要求する。

---

## 4. 処理速度

### 🟠 V1. ビュー/テーブルに LIMIT・ページングが無い
- **場所**: `services/view_service.py:27-31`、`db/repository.py:22-26`(`list`)、`api/routers/crud.py:21-32`(`list_records`)
- **根拠**: 全件 SELECT → 全件 JSON 化 → 全件 DOM 描画。時系列テーブルが伸びると `/api/views/<v>` が行数に比例して重くなり、SSE がそれを周期実行するため悪化が増幅する。
- **改善案**: ビュー定義に既定 `LIMIT` を持たせる、API に `?limit&offset`(またはカーソル)を追加、フロントは仮想スクロール or ページング。

### 🟠 V2. SSE がクライアント毎に全クエリ再実行 + 全データ sha256
- **場所**: `api/routers/views.py:112-124`、`services/view_service.py:51-55`(`data_hash`)
- **根拠**: 接続クライアントごとに `refresh_interval` 周期で同一ビューを独立再クエリし、毎回 `json.dumps`+`sha256` を全行に対して計算する。kiosk 1 台 + ダッシュボード複数で **N 倍の DB 負荷と CPU**。
- **改善案**: ビュー単位で「直近結果 + ハッシュ」を TTL=interval で**共有キャッシュ**し、全クライアントで使い回す(評価・ハッシュは 1 回だけ)。E6 のバックグラウンド評価と統合すると素直。

### 🟠 V3. ダッシュボードはカード枚数だけ独立ポーリング
- **場所**: `js/dashboard.js:124-135`(カードごとに `setInterval(fetchAndUpdate, interval)`)
- **根拠**: ミニチャート M 枚なら 5 秒ごとに M 本の `/api/views/<name>` リクエストが飛び、それぞれがアラート評価も駆動する。
- **改善案**: 複数ビューをまとめて返すバッチ API、または V2 の共有キャッシュ利用。

### 🟡 V4. KPI 評価が件数分のコネクションを開く(N+1)
- **場所**: `services/kpi_service.py:26-34,64-65`(`_evaluate_one` が KPI ごとに `self.db.readonly()`)
- **根拠**: `evaluate_all` が KPI ごとに別コネクションで 1 クエリ。`/api/kpis` は home/kiosk から定期ポーリングされるため、KPI 枚数 × ポーリング頻度の接続が発生。
- **改善案**: `with self.db.readonly() as conn:` を 1 本開いて全 KPI を回す。

### 🟡 V5. 取り込みが全行をメモリ展開してから一括 INSERT
- **場所**: `services/importer.py:83-106`、`services/connectors.py:108-159`、`JsonSourceConnector`(`connectors.py:366-367` の `json.load` 全読み)
- **根拠**: CSV/JSON/全件 SQLite を一旦 list に全展開してから挿入するため、大容量ソースでメモリを圧迫する。
- **改善案**: N 件ごとのチャンク `executemany`、JSONL は既に逐次なので CSV/JSON も同様にストリーミング化。

### 🟡 V6. 業務インデックスが張られない
- **場所**: `db/registry.py:41-50`(`_build_table`)
- **根拠**: 宣言列から表を作るが、ウォーターマーク列・時刻列・JOIN 外部キーにインデックスを張らない。integrations の時系列ビュー(`WHERE ts > ...` / `ORDER BY ts`)がフルスキャンになりがち。
- **改善案**: 外部キー列・ウォーターマーク列へインデックスを自動付与、または `TableDef` に `indexes` 宣言を追加。

---

## 5. セキュリティ補足(参考)

> `config.py` と環境変数は運用者(信頼境界内)が書く前提なので緊急度は低いが、ハードニングとして。

- **S1.** `<script type="application/json">{{ ... | safe }}`(`templates/index.html:32`、`table.html:59`、`kiosk.html:66`)に config 由来文字列を埋め込んでいる。`json.dumps(ensure_ascii=False)` は `<` や `/` を素通しするため、ビュータイトル等に `</script>` を含めると DOM 破壊/XSS になり得る。`</` → `<\/` のエスケープを挟むと安全。
- **S2.** CSP 等のセキュリティヘッダが無く、`base.html` で `onclick="..."` のインラインハンドラを多用しているため厳格な CSP を後付けしにくい。最低限 `X-Content-Type-Options: nosniff` 等を付けると良い。
- **S3.** `ViewDef.query`/`KpiCard.query` は生 SQL(設計上、運用者が書く前提)。読み取り専用接続 + `PRAGMA query_only`(`db/engine.py:42-58`)で多重防御している点は良い。`integrations/network_checker.py:113` のホスト名は手動 `''` エスケープ依存なので、将来ユーザー入力を流す場合は要注意。

---

## 良い点(維持したい設計)

- **設定の宣言化と起動時バリデーション**: `settings/declarative.py` が Pydantic で typo/矛盾を起動時に弾く。`loader.py` がエラーを日本語整形するのも親切。
- **構造的な SQL インジェクション対策**: `db/registry.py` が列/表を SQLAlchemy Core のオブジェクト化し、CRUD は式ビルダのみ(`db/repository.py`)。文字列連結 SQL を作らない方針が徹底。
- **副作用の隔離**: `main.py:create_app` ファクトリで全初期化を閉じ込め、テストで設定差し替えが容易。
- **ドメイン例外の中立配置**: `exceptions.py` を API 層から独立させ、500 系は相関 ID のみ返して内部を漏らさない(`api/errors.py:24-30`)。
- **フロントの XSS 耐性**: 描画は `textContent`/`createElement` 中心で `innerHTML` をほぼ使わない。
- **テスト・CI**: 243 個のテスト、Python 3.10–3.13 マトリクス + `black --check`(`.github/workflows/test.yml`)。

---

## 推奨対応順序

1. **E2**(監視ループのクラッシュ耐性)— 数行の try/except で守れて影響大。
2. **E1**(append 重複)— データ正確性の根幹。E3/E4 も取り込み信頼性として同時に。
3. **U1**(`protect_reads` で UI 全滅)— 最低限ドキュメント明記、できればセッション導入。
4. **E5/E6/E7** をまとめて — アラート評価をバックグラウンド単一ループへ分離すると 3 件が同時に解消し、**V2** の共有キャッシュとも統合できる。
5. **V1/V3/V4**(LIMIT・バッチ・コネクション集約)→ **M1**(フロント共通化)。
