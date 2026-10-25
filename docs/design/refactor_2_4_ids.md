# 2.4 の開発で使った内部 ID の一覧

コードとテストのコメントには `(PERF-06)`・`DX-08`・`#D6-17` のような ID が出てきます。
これは 2.4 の開発中に行った点検で、見つかった問題に付けた番号です。点検の記録そのものは
リポジトリに含めていないため、コメントの ID が何を指すかをここにまとめます。どの問題も
2.4.0 で対応済みで、利用者向けの説明は [CHANGELOG](../../CHANGELOG.md) にあります。

- **監査(2.4 の作業の前)** … v2.3.1 を点検した結果。接頭辞は分野を表します:
  `PERF`(性能)・`DX`(使い勝手)・`CRIT`(重大な不具合・欠けていた機能)・
  `ARCH`(構成)・`DEPLOY`(配備)・`FE`(画面)。
- **レビュー(2.4 の作業の後)** … 2.4 の変更全体を見直した結果。数字はレビューの担当です:
  `D1`(互換性の確認・パッケージ)・`D2`(性能)・`D3`(データの正しさ)・`D4`(並行処理・停止・資源)・
  `D5`(後方互換・更新・使い勝手)・`D6`(セキュリティ・配備の堅牢化)・`D7`(保守性)。
- `(#E8)`・`(#V5)` のような 2.3 以前の ID は
  [improvement_review_2026-06.md](improvement_review_2026-06.md) にあります。

重要度は 致命的 / 高 / 中 / 低 の 4 段階です。「主な場所」のパスは `monitor_app/` からの
相対パスです(`Dockerfile`・`docker/`・`.github/` などリポジトリ直下のものを除く)。
コメントに ID を新しく書くときは、ここに 1 行足してください(できればコメント自体に
理由を書き、ID は補足にとどめます)。

## 監査(2.4 の作業の前)

### PERF — 性能

| ID | 重要度 | 内容 | 主な場所 |
|---|---|---|---|
| PERF-01 | 致命的 | 同じビューへの要求が集中すると、キャッシュの計算が重なり、sqlite3 の行ごとの GIL の取り合いと合わせてダッシュボード・大型表示が 10〜40 秒止まり、数十のクライアントで書き込みが失敗した | `services/view_cache.py`, `db/repository.py`, `services/audit_service.py` |
| PERF-02 | 高 | ファイルの SQLite が DELETE ジャーナル・`synchronous=FULL` で動き、書き込みのたびに約 4 回 fsync し、読み取りと書き込みが互いを待った | `db/engine.py` |
| PERF-03 | 高 | ビューのキャッシュが 1 秒で切れ、変更の検知も無いため、待機中のサーバー・1 つだけのポーリング・アラートの評価・KPI が変わらないデータを計算し続けた | `db/change_token.py`, `services/view_service.py` |
| PERF-04 | 高 | CSV の取り込みがファイル全体を dict にしてセルごとに型を変換していた(メモリが行数に比例)。単純にストリーミングにすると書き込みロックが長くなる | `services/importer.py`, `services/rows.py` |
| PERF-05 | 高 | 追記モードの監視が、変更のたびに CSV 全体を読み直して変換し、起動時の行数の数え上げがイベントループを止めた | `services/importer.py`, `services/ingest_watcher.py` |
| PERF-06 | 高 | 追記モードの監視が、書きかけの最後の行を途中までの値で保存し、そのまま直さなかった | `services/importer.py`, `services/csv_tail.py` |
| PERF-09 | 中 | アラートをページ単位の一部のデータで評価し(発火と復帰の繰り返し・同じ通知の連発)、データが変わらなくても要求や SSE のたびに評価していた | `services/alert_service.py` |
| PERF-10 | 低 | `GET /api/tables/{t}` を limit なしで呼ぶと表全体を返し、しかも 2 回検証していた | `db/repository.py` |

### DX — 使い勝手

| ID | 重要度 | 内容 | 主な場所 |
|---|---|---|---|
| DX-01 | 高 | 案内していた起動コマンド `runserver --import-csv` が、再起動のたびにフォーム・API で入力した行を消した。代わりの `--keep` は 2 回目で失敗した | `cli/data.py`, `services/ingest_watcher.py` |
| DX-02 | 高 | SQLite の DB のフォルダを作らないため、`instances/` が無いと 515 行の OperationalError になった(`check` は OK と表示した) | `db/engine.py` |
| DX-03 | 高 | 日本語版 Windows で出力をリダイレクトすると(cp932)、成功したコマンドも「✅」の表示で失敗し、終了コード 1 になった | `cli/_errors.py`(`make_streams_safe`) |
| DX-04 | 高 | Excel の CSV(cp932)が 88 行の UnicodeDecodeError で失敗し、直す設定も案内されず、監視は古いデータのまま黙っていた。`MONITOR_CSV_ENCODING=auto` を追加した | `services/csv_encoding.py`, `services/importer.py` |
| DX-06 | 中 | `.env`・CSV のフォルダ・SQLite のパスが作業フォルダ基準で、`--config` で別のフォルダを指すと「0 行」のまま黙って成功した | `cli/_common.py`, `settings/runtime.py` |
| DX-07 | 中 | 設定の検証の抜け: 省略できるフィールドの綴りの誤りを黙って無視し、v2 形式の設定では分かりやすいエラー表示が使われず、`check` が SQL を試さなかった | `settings/declarative.py`, `settings/loader.py`, `checks.py` |
| DX-08 | 中 | 列名から型を推測していたため、数値の CSV の列を文字列として保存し、ビューと KPI の MAX・MIN・ORDER BY が誤った | `settings/infer.py`, `services/importer.py`, `checks.py` |
| DX-09 | 中 | 設定なしで CSV のフォルダからダッシュボードを開く `monitor-app serve`(`--write-config` つき)の提案 | `cli/zero.py`, `settings/infer.py` |

### CRIT — 重大な不具合・欠けていた機能

| ID | 重要度 | 内容 | 主な場所 |
|---|---|---|---|
| CRIT-01 | 致命的 | 案内どおりの手順で PostgreSQL / MySQL が動かなかった(2 回目の起動で失敗、フォームの追加が主キーで衝突、MySQL で小数が単精度に、MySQL で `sources` が動かない) | `db/engine.py`, `db/bulk.py`, `db/registry.py`, `db/errors.py`, `db/ingest_state.py`, `api/errors.py`, `services/importer.py` |
| CRIT-02 | 高 | データが増え続ける(保持期間・ビューの行数の上限が無い)ため、README の例が数週間で遅くなり、メモリ不足になった | `services/retention.py` |
| CRIT-03 | 高 | CSV の見出しを `TableDef` と照合しないため、見出しの変わった CSV で正しい行がすべて NULL の行に置き換わり、✅ と表示された。どのテーブルにも合わない CSV は黙って無視された | `services/importer.py` |
| CRIT-04 | 高 | 簡単に公開すると安全でない既定: API キーなしの LAN からの書き込みを警告しない、`.env` の綴りの誤りで認証が黙って無効になる、Host ヘッダを確かめない(DNS リバインディング) | `checks.py`, `settings/runtime.py`, `security.py` |
| CRIT-05 | 高 | v0.1.x のプロジェクトを更新すると CRUD が黙って壊れた(旧い表に主キーが無く、新しい行の id が NULL になり、API が存在しない id を返した) | `db/repository.py`, `db/schema.py` |
| CRIT-08 | 中 | 取り込み API が冪等でなく、PLC の再送が text/plain の 500 になり、そのバッチの新しい値が失われた | `db/bulk.py`, `services/crud_service.py` |
| CRIT-09 | 中 | 表のタブごとに SSE を 1 本使うため、ブラウザの同時接続数(6)を使い切り、同じサーバーの 7 つ目のタブが開けなかった | `web/static/js/app.js` |
| CRIT-11 | 中 | 画面から入力したデータを持つ SQLite のバックアップ・復元の手段が無かった(WAL にした後はファイルのコピーで最新の行が欠ける) | `db/backup.py`, `cli/maintenance.py` |
| CRIT-12 | 中 | datetime の列の型が無く、`date` は ISO の日付だけを受け付けたため、Excel の日時を拒否するか、文字列として誤った順に並べた | `services/coercion.py`, `db/types.py` |
| CRIT-13 | 中 | アラートのメールで暗号化なしの社内中継や SMTPS が使えず、失敗した通知は再試行されずに捨てられ、設定の足りない通知先は黙ってコンソールになった | `services/notifiers/` |

### ARCH・DEPLOY・FE — 構成・配備・画面

| ID | 重要度 | 内容 | 主な場所 |
|---|---|---|---|
| ARCH-01 | 高 | 読み取りの API が一部のデータでアラートの状態を変え、通知が繰り返された | `services/alert_service.py`, `api/routers/views.py` |
| ARCH-03 | 中 | 行の型変換が 3 か所にあり、取り込みのカーソルをデータとは別のトランザクションで保存していた(途中で落ちるとコネクタが止まる) | `services/rows.py`, `services/connectors.py`, `db/ingest_state.py` |
| ARCH-06 | 低 | DB 層の整理(URL の接頭辞による読み取り専用の判定、上限の無い ViewCache、使われていないコードなど) | `services/view_cache.py`, `db/engine.py` |
| DEPLOY-02 | 致命的 | イメージの起動コマンドが起動のたびに CSV で置き換え、画面・API・フォームで入力した行を消した | `Dockerfile`, `docker/entrypoint.sh` |
| DEPLOY-05 | 高 | SSE の接続があると停止が終わらなかった(uvicorn 0.47 未満) | `pyproject.toml`(uvicorn の下限) |
| DEPLOY-13 | 中 | 運用を始めた後に `config.py` に列を足すと、API が黙って壊れた(表定義のずれの点検も移行も無かった) | `db/schema.py`, `api/errors.py`, `cli/maintenance.py` |
| FE-03 | 高 | 表の描き直しが大きなデータに耐えなかった(更新のたびに表全体を作り直し、常に全行を取得) | `web/static/js/app.js` |
| FE-06 | 中 | ASCII 以外の名前のビューの CSV エクスポートが 500 になり、BOM も無かった(Excel で日本語が化ける) | `api/routers/views.py` |

## レビュー(2.4 の作業の後)

### D1 — 互換性の確認・パッケージ

| ID | 重要度 | 内容 | 主な場所 |
|---|---|---|---|
| D1-02 | 低 | sdist に `tests/` はあるが `docker/` が無く、sdist からテストを実行すると失敗した | `pyproject.toml`(`[tool.hatch]`) |
| D1-04 | 低 | 生成した `compose.yaml` でイメージのタグしか変えられず、自分でビルドしたイメージを使えなかった | `scaffold/deploy/compose.yaml` |

### D2 — 性能(v2.3.1 との比較)

| ID | 重要度 | 内容 | 主な場所 |
|---|---|---|---|
| D2-01 | 中 | キャッシュ済みの大きなビューを、gzip を受け付ける要求のたびに圧縮し直していた(10 万行のビューで v2.3.1 より遅かった)。圧縮した本文をスナップショットに残す | `services/view_service.py`, `api/routers/views.py`, `web/serving.py` |

### D3 — データの正しさ

| ID | 重要度 | 内容 | 主な場所 |
|---|---|---|---|
| D3-01 | 高 | 追記の監視が、読めない行が 1 行あると cp932 に切り替わったままになり、以後の UTF-8 の行がすべて文字化けした | `services/csv_tail.py`, `services/csv_encoding.py`, `services/importer.py` |
| D3-02 | 中 | 先頭 64 KB より後に読めないバイトが 1 つあると、UTF-8 の CSV 全体を cp932 で読んで文字化けさせ、置き換えモードでは正しい行を上書きした | `services/importer.py`, `services/csv_encoding.py` |
| D3-03 | 中 | 追記の監視と `runserver --import-csv-if-empty` の組み合わせで、サーバーを止めている間に追記された行が黙って失われた | `services/csv_tail.py`, `services/importer.py`, `services/ingest_watcher.py` |
| D3-04 | 中 | v2.3.1 の文字列の列を(保持期間の警告に従って)`datetime` に変えると、`GET /api/tables` が 500 になった | `db/types.py`, `db/schema.py` |
| D3-05 | 中 | MySQL の datetime の列が小数秒を丸めた(23:59:59.7 が翌日の 00:00:00 になった)。datetime を主キーにした再送の件数も誤った | `db/types.py`, `db/schema.py` |
| D3-06 | 中 | SQLite が受け付ける値(255 文字を超える文字列、2^31-1 を超える整数)で PostgreSQL / MySQL の取り込み全体が失敗し、ファイルも行も示さず、`check --data` は OK と表示した | `services/value_limits.py`, `services/importer.py`, `services/connectors.py`, `db/registry.py` |
| D3-07 | 中 | 自分自身を参照する外部キーの表: MySQL で置き換えの再取り込みが必ず失敗し、子が親より前にある CSV が PostgreSQL・MySQL で失敗した | `services/importer.py` |
| D3-08 | 中 | `kind="json"` のソースが既定の追記モードで、ファイルが変わった後のポーリングのたびに主キーの重複エラーになった | `services/connectors.py`, `checks.py` |
| D3-09 | 中 | CSV の末尾の NUL(SD カード・FAT での停電)で、Python 3.10 では追記の監視が止まったままになり、3.13 では 1 行を失ってログがあふれ、CLI はトレースバックを出した | `services/csv_tail.py`, `services/importer.py` |
| D3-10 | 低 | 文字列の列の保持期間: 区切りが混ざると新しい行を早く消し、空の時刻の行を消し、先頭の行が空だと保持期間が効かなかった | `services/retention.py` |
| D3-11 | 低 | 時計の狂った(未来の日時の)バックアップが 1 つあると、その日時まで定期バックアップが止まった | `services/backup_task.py` |
| D3-12 | 低 | 壊れたページのあるバックアップを検証が通し、`restore` が正常な DB を壊れた DB で置き換えた | `db/backup.py` |
| D3-13 | 低 | コピーの途中で止まったバックアップが隠れた一時ファイル(最大で DB の大きさ)を残し、消されなかった | `db/backup.py` |
| D3-14 | 低 | 小数の列の `NaN`・`inf` の扱いが DB ごとに違った(SQLite は NULL、PostgreSQL は AVG・MAX を壊す NaN、MySQL は取り込み全体が失敗) | `services/coercion.py`, `services/rows.py` |
| D3-15 | 低 | `GET /api/tables/{t}?limit=&offset=` に ORDER BY が無く、PostgreSQL では編集した行が最後のページへ移った | `db/repository.py` |
| D3-16 | 低 | 2 つのプロジェクトが同じ `MONITOR_BACKUP_DIR` を使うと(どちらも `database.db`)、片方がバックアップされず、世代の整理がもう片方のファイルを消した | `db/backup.py`, `services/backup_task.py`, `cli/maintenance.py` |

### D4 — 並行処理・停止・資源

| ID | 重要度 | 内容 | 主な場所 |
|---|---|---|---|
| D4-01 | 中 | 停止時にバックグラウンドの処理を止めも待ちもしないため、コンテナは長い処理を待って SIGKILL され、PC では処理が途中で打ち切られた | `cancel.py`, `services/periodic.py`, `db/engine.py`, `container.py` |
| D4-02 | 低 | 中断したバックアップが `MONITOR_BACKUP_DIR` に `.<DB 名>-XXXX.tmp` を残し続けた | `db/backup.py`, `services/backup_task.py` |
| D4-06 | 中 | 置き換えの取り込みが CSV の解析の間ずっと SQLite の書き込みロックを持ち、同時の API の書き込みが待ちの上限の後に HTTP 500 になった | `services/importer.py`, `db/errors.py`, `api/errors.py` |
| D4-07 | 中 | フォルダの監視が取り込む前に CSV を処理済みとして記録し、一時的な失敗(DB のロック)の後に取り込み直さず、`/api/health` も正常に戻った | `services/ingest_watcher.py` |
| D4-12 | 低 | コネクタのウォーターマークを書き込みのトランザクションの外で読むため、cron の `sync-sources` と監視が同じソースを読むと行が二重に入った | `services/connectors.py`, `db/ingest_state.py` |

### D5 — 後方互換・更新・使い勝手

| ID | 重要度 | 内容 | 主な場所 |
|---|---|---|---|
| D5-01 | 高 | 2.3.1 で入れた PostgreSQL の DB は、更新後も id の連番が遅れたままで、フォーム・API の追加が 409 になった | `db/registry.py`, `db/bulk.py`, `db/repository.py`, `db/schema.py`, `cli/maintenance.py` |
| D5-02 | 中 | 案内していた 2.3.x へ戻す手順(`_ingest_state` の行を消す)では、2.3.1 の sqlite ソースがポーリングのたびに失敗するか、行が二重になった | `db/ingest_state.py` |
| D5-06 | 低 | `import-csv` が取り込みの注意をすべて 2 回表示し(ログと要約)、「(skip 0)」が「N 行を読み飛ばしました」と食い違った | `cli/data.py`, `services/importer.py` |
| D5-07 | 低 | 未知の `MONITOR_*` の候補を共通の接頭辞 `MONITOR_` で選ぶため、無関係な変数に的外れな候補を出し、`MONITOR_NAME` が認証のエラーになった | `settings/runtime.py` |
| D5-08 | 低 | CSV の中の主キーの重複: `check --data` は何も言わず、取り込みのエラーはファイルもキーも示さず、何も取り込まなかったことも言わなかった | `services/importer.py`, `checks.py` |
| D5-09 | 低 | v0.x の主キーの無い表について、`check` / `migrate` が「作り直して」と言うだけで方法を示さなかった | `db/schema.py` |
| D5-10 | 低 | `config.py` の無いフォルダでの `deploy` が警告するだけで、起動に失敗し続ける systemd のユニットを書き、終了コード 0 で終わった | `cli/deploy.py`, `deploy.py` |

### D6 — セキュリティ・配備の堅牢化

| ID | 重要度 | 内容 | 主な場所 |
|---|---|---|---|
| D6-01 | 中 | `MONITOR_API_KEY` が空のとき、キーなしの要求は 401 なのに、誰でも作れる固定のセッション Cookie で書き込めた(起動時の警告も無かった) | `settings/runtime.py`, `api/deps.py` |
| D6-02 | 中 | メールの通知が SMTP サーバーの証明書を確かめず(STARTTLS・SMTPS)、中間者に SMTP のパスワードを盗まれ得た | `services/notifiers/email.py` |
| D6-03 | 中 | Host ヘッダを確かめないため、このパソコンだけで待ち受けるキーなしの既定(runserver・serve)が DNS リバインディングで読み書きされ得た | `security.py`, `web/serving.py`, `cli/server.py` |
| D6-04 | 中 | rootful Docker で `/project` が root の所有のとき(`-v` の無いパスを Docker が作った場合も)、アプリが uid 0 と既定の権限のまま動いた | `docker/entrypoint.sh` |
| D6-06 | 中 | Windows の起動時のタスクが、プロジェクトのフォルダのコード(`start-monitor.bat`・`config.py`・作業フォルダから読まれるモジュール)を SYSTEM で実行した | `scaffold/deploy/install-task.ps1`, `scaffold/deploy/start-monitor.bat` |
| D6-07 | 中 | ASCII 以外の API キーや `X-API-Key` ヘッダで比較が例外になり 500 を返した(日本語のキーでは書き込みもログインもできなかった) | `api/deps.py`, `security.py`, `checks.py`, `main.py` |
| D6-10 | 低 | CSV・xlsx のエクスポートが、`=` などで始まる文字列を表計算ソフトの数式としてそのまま書き出した | `security.py`, `api/routers/views.py` |
| D6-11 | 低 | 処理されなかった 500 の応答にセキュリティヘッダが付かず、保護したビューの応答も共有キャッシュに保存され得るヘッダで返していた | `main.py`, `api/routers/views.py` |
| D6-12 | 低 | `/docs` と `/redoc` が版を固定しない CDN のスクリプトを SRI なしで読み込み、`MONITOR_PROTECT_READS` でも公開のままだった | `web/api_docs.py` |
| D6-13 | 低 | ログインの Cookie が API キーの固定の HMAC で、期限も失効も無く、ログアウトはブラウザ側だけだった | `api/deps.py`, `api/routers/auth.py` |
| D6-14 | 低 | 依存の下限 `jinja2>=3.1` が既知の脆弱性のある版を許していた | `pyproject.toml` |
| D6-15 | 低 | リリースのワークフローの供給網の対策の不足(actions を書き換えられるタグで指定、チェックアウトの認証情報を保持) | `.github/workflows/` |
| D6-16 | 低 | エントリポイントの堅牢化の抜け(差し替えたシンボリックリンクを chown が辿る、`setpriv` に `--no-new-privs` が無い、イメージに setuid の実行ファイルが残る) | `docker/entrypoint.sh`, `Dockerfile` |
| D6-17 | 低 | 簡単に公開すると安全でない既定: 配備用の API キーを生成しない、例の `change-me` を黙って受け付ける | `deploy.py`, `cli/deploy.py`, `security.py`, `checks.py`, `main.py` |

### D7 — 保守性

| ID | 重要度 | 内容 | 主な場所 |
|---|---|---|---|
| D7-01 | 中 | `MONITOR_CSV_ENCODING=utf-8` で BOM つきの CSV を `check --data` が「列が無い」と誤って警告した(取り込みは問題なく動く) | `checks.py`, `services/importer.py` |
| D7-02 | 中 | 見出しに空白がある(`" qty "` など)と、取り込みは変換できない行を読み飛ばすのに、`check --data` は OK と表示した | `checks.py`, `services/importer.py` |
| D7-03 | 低 | 数値の列の助言が、`check`・`serve` の推論と取り込み(DX-08)とで食い違った(2**53 を超えるコード、`1.` のような値) | `settings/infer.py`, `services/importer.py` |
| D7-05 | 低 | CSV の文字コードの規則が 4 か所に別々に書かれていた | `services/csv_encoding.py` |
| D7-06 | 低 | コメントの内部 ID(この一覧の ID)の意味を、リポジトリのどこでも引けなかった | この文書, `tests/test_internal_ids_documented.py` |
| D7-07 | 低 | `run_csv_import` に、テストの代役の引数に合わせるためだけの分岐があった | `cli/data.py` |
| D7-08 | 低 | 使われていないコード(参照の無い非公開メソッド、テストだけが使う補助関数、CLI と同じ書き出しの繰り返しの写し) | `deploy.py`, `cli/deploy.py` |
| D7-09 | 低 | 設定ファイルのパスの解釈が二重にあり、CLI はフォルダと `~` を受け付けるのに `MONITOR_CONFIG_PATH`(`app_factory`)は受け付けなかった | `settings/loader.py`, `cli/_common.py`, `main.py` |
| D7-10 | 低 | 小さな補助関数が規則のずれたまま重複していた(ループバックか、メモリ上の SQLite か、同じフォルダか など) | `security.py`, `db/engine.py`, `settings/runtime.py` |
