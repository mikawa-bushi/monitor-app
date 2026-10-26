# Changelog

## [2.4.0] - 2026-10-21

> 「誰でも手元ですぐ動かせて、速く、簡単に公開できる」ことを目標にした機能リリース。設定ファイルなしでダッシュボードを開く `monitor-app serve`、SQLite の WAL 化とストリーミング取り込み・共有キャッシュによる高速化、Docker イメージと `monitor-app deploy`(API キーの生成つき)、バックアップ・保持期間・`check` の強化、公開時のセキュリティの強化を含む。既存のプロジェクト・`config.py`(v1 の dict 形式も)・DB・REST API・CLI はそのまま動く。挙動が変わる点は「Changed」、更新の手順は「Upgrade notes」を参照。

### ハイライト
- **設定なしで始める** — `monitor-app serve ./csv-folder`(`uvx monitor-app serve ...` ならインストールも不要)で、CSV から列の型・主キー・グラフ・KPI を推論してダッシュボードを開く。`startproject --from-csv` でそのままプロジェクトにできる
- **速い** — 100 万行の CSV の取り込みが約 1.3 倍速く(6.0 秒 → 4.7 秒)、メモリは 584 MB → 約 60 MB。同じ 1 万行のビューへの同時 30 リクエストが約 40 秒 → 約 40 ミリ秒。追記監視は 100 万行の CSV でも追記分を約 1 ミリ秒で取り込む
- **Excel の CSV にそのまま対応** — cp932(Shift_JIS)の自動判別、`2026/10/21 8:00` 形式の日時、見出しの表記ゆれ、Excel 向けの BOM 付きエクスポート(`?bom=1`)
- **既定で安全** — `runserver --import-csv-if-empty` で画面から入力した行が再起動後も残る。列を失う置き換えは中止し、`monitor-app check` が設定・SQL・`.env` の誤りを起動前に見つける
- **公開が簡単** — `docker run -p 9990:9990 ghcr.io/mikawa-bushi/monitor-app`(amd64 / arm64)、`monitor-app deploy` で Docker Compose・systemd・Windows タスク スケジューラの起動ファイルと API キーを生成、リバースプロキシのサブパス配信、`/api/health` での死活監視
- **安全に公開** — 空の API キーで認証を回避できた問題の修正、期限つきのログイン、DNS リバインディング対策(`MONITOR_ALLOWED_HOSTS`)、SMTP の証明書の検証、CSV・Excel のエクスポートでの数式の無害化、権限を捨てて動くコンテナ
- **データを守る** — `monitor-app backup` / `restore`(壊れた DB の復旧にも)、定期バックアップ、`Retention` による古い行の自動削除、`monitor-app migrate`

### Added
- **`monitor-app serve [PATH...]`** — config.py なしで、CSV のフォルダ(または同じフォルダの CSV ファイル)からダッシュボードを開く
  - 列の型はデータから推論する(整数・小数・真偽値 `TRUE/FALSE/yes/no`・日付・日時)。`0012` のように先頭が 0 のコードや 2^53 を超える数値は文字列のまま、`0/1` は数値のまま
  - 重複のない整数の `id` を主キーにし、なければ `_row_id` を足す。CSV ごとにビュー(大きなファイルは新しい 5,000 行)と行数の KPI を作り、数値の列があれば折れ線グラフも作る。ホームに並べるグラフは最初の 6 つ(`dashboard.views` で変更可)
  - 文字コードはファイルごとに判別する(UTF-8・BOM 付き UTF-8・cp932)。日本語の見出しも可
  - データは一時ファイルの SQLite に入れ、終了時(Ctrl+C・SIGTERM・端末を閉じる)に削除。オプション: `--db FILE`(残す)・`--append`(追記行だけ取り込む)・`--no-watch`・`--write-config FILE`(推論した設定を config.py に書き出す。上書きしない)・`--open`・`--host` / `--port`
  - 取り込めない名前(引用符・`/`・`,`・`: # ? %` を含む等)・Excel のロックファイル(`~$*.csv`)・隠しファイル・タブ / セミコロン区切りのファイルは理由を示して飛ばす
- **`monitor-app startproject NAME --from-csv DIR`** — 手元の CSV から推論した config.py と CSV のコピーでプロジェクトを作る(サンプルの CSV は入れない)。**`--deploy docker,systemd,windows|all`** で起動ファイルも生成。生成するプロジェクトに `.gitignore`(`instances/*.db*`・`.env`・`logs/`・`backups/` など)を追加
- **`monitor-app deploy [docker|systemd|windows|all] [--project DIR] [--port N] [--config FILE] [--force]`** — 起動ファイルを生成し、次の手順を表示する。既存のファイルは `--force` なしでは上書きしない。対象を省略すると Docker とこの OS 向けのもの
  - どの対象でも、プロジェクトの `.env` に `MONITOR_API_KEY` が無ければ推測されにくいキー(URL で使える 32 文字)を生成して書く(`startproject --deploy` も同じ)。キーそのものは表示せず、書いた場所を表示する。新しく作る `.env` は所有者だけが読める(0600。`sudo` で実行したら `sudo` したユーザーの所有)。既にあるキーは変えず、説明用の値なら警告する。`startproject`(`--deploy` なし)・`runserver`・`serve` は従来どおりキーなしで手元で動く
  - `--config FILE` — systemd のユニット・`start-monitor.bat`・compose の `command:` で runserver に渡す設定ファイル(プロジェクトのフォルダからの相対パスか絶対パス。Docker は相対パスのみ)
  - systemd・windows は、フォルダに `config.py`(または `--config` のファイル)が無ければ終了コード 1 で止まる(`--force` で生成する)。docker は警告だけ(空のフォルダにはサンプルを書く)
  - docker: 公開イメージを使い、フォルダをマウントする `compose.yaml` と `.dockerignore`(`.env` の `MONITOR_PORT`・`TZ`・`MONITOR_IMAGE`・`MONITOR_IMAGE_TAG` を使う。自分でビルドしたイメージは `MONITOR_IMAGE=monitor-app`)。`cap_drop: [ALL]`・`cap_add: [SETUID, SETGID, CHOWN]`・`no-new-privileges` で権限を絞り、ヘルスチェックは `python -I` で動かす
  - systemd: 今の Python で `runserver --import-csv-if-empty` を動かす `monitor-app.service`(異常終了時に再起動、停止は 15 秒以内、今のユーザーで実行)
  - windows: ダブルクリックで起動する `start-monitor.bat`(ログは `logs\monitor.log`)と、タスク スケジューラに登録する `install-task.ps1`(PC の起動時に登録したユーザーで動かす。S4U でパスワードを保存せず、管理者の権限も使わない / `-AtLogon` / `-Uninstall` / `-AsSystem` で SYSTEM。プロジェクトのフォルダ・`config.py`・`start-monitor.bat` をすべてのユーザーが変更できるときは警告する)
  - `start-monitor.bat` と systemd のユニットは `PYTHONSAFEPATH=1` を設定する(Python 3.11 以降で、プロジェクトのフォルダに置かれたモジュールを読み込まない)
- **`monitor-app check` の強化** — `--data`(CSV の見出し・型・文字コードも点検)、`--strict`(警告でも終了コード 1。CI 向け)、`--fix-schema`(足りない列を追加)を追加。次も報告する:
  - config.py のキーの綴りの誤り(行番号と正しい候補つき)、SQL の誤り(全ビュー・KPI を空の DB で試行)、存在しないアラート・グラフ・色分け・ラベルの列
  - `.env` と環境変数の `MONITOR_*` の綴りの誤り(`MONITOR_API_KEY`・`MONITOR_PROTECT_READS` の誤りは認証が無効のままになるためエラー)、API キーなしの LAN 公開、届かない通知先
  - config.py と既存の DB の表の違い(DB は読み取り専用で開き、作成も変更もしない)。PostgreSQL の遅れた連番(`schema-sequence-behind`)と、1 秒未満を保てない MySQL の DATETIME の列(`schema-datetime-precision`。`ALTER TABLE t MODIFY ts DATETIME(6)` を示す)も情報として知らせる
  - `--data` は `import-csv` と同じコードで CSV を読み、取り込みと同じ結果を報告する: 見出しの表記ゆれ(BOM・前後の空白。`csv-header-variant`、見えない文字は `\ufeff` のように表示)、変換できない値(取り込みが読み飛ばす行)、CSV の中の主キーの重複(`csv-duplicate-key`。エラー)、読めないバイト(`csv-undecodable`)・CSV として解析できない行(`csv-unparsable`)、`MONITOR_DATABASE_URL` が PostgreSQL / MySQL なら、その DB が受け付けない値(`csv-db-limit`。DB には接続せず、新しく作る表の型で判定)。数値の列の助言は `serve` の推論・取り込みと同じ規則(`007` や 2^53 を超える数は文字列のまま、`1.`・`1e3` は小数)
  - API キーが説明用の値(`change-me`・`secret`・`<...>` など。`api-key-placeholder`)・16 文字未満(`api-key-short`)・ASCII 以外(`api-key-non-ascii`)。主キーを持たないレコードを毎回入れ直す `sources`(`source-reinsert`。`mode="replace"` を案内)
  - 出力は エラー / 警告 / 情報 に分けてヒントを添える。従来の `✅ OK — テーブル N 件 / ビュー M 件` の行はそのまま
- **`monitor-app migrate [--dry-run]`** — config.py に足した列を既存の表に追加する(NULL を許す列の追加のみ。実行する SQL を先に表示)。型の違い・主キーの違い(v0.x の表)・MySQL の単精度 FLOAT など自動で直せないものは、直し方(正確な `ALTER` 文)を表示する。主キーの無い v0.x の表には、作り直す手順(バックアップ → `ALTER TABLE … RENAME TO …_v0` → 起動か `import-csv` で表を作る → `INSERT … SELECT` → `DROP TABLE …_v0`)を実際の列名で示す。PostgreSQL の遅れた連番も直す(「Fixed」を参照)
- **`monitor-app backup [--dest DIR] [--keep N]`** — SQLite の一貫したバックアップを 1 ファイル(`<DB 名>-YYYYmmdd-HHMMSS.db`、所有者だけが読める)に作る。サーバーの稼働中でもよく、WAL にしかない書き込みも含む。古いものは `--keep` 世代(既定 `MONITOR_BACKUP_KEEP`)だけ残す
- **`monitor-app restore FILE --yes`** — バックアップから復元する。`--yes` なしでは置き換わる内容を表示して止まる。先にバックアップを SQLite の `quick_check` で点検し、壊れていれば「バックアップファイルが壊れているため、復元に使えません」と別の世代を案内して今の DB を残す(ファイル全体を読む)。復元の前に今の DB をバックアップのフォルダへ退避する。開けなくなった DB も復旧でき、壊れたファイルは `<DB 名>-damaged-日時.db` として残す
- **`import-csv --if-empty` / `--force`、`runserver --import-csv-if-empty` / `--force`** — 行の無いテーブルにだけ取り込む / 見出しに列が無くても置き換える。取り込みの前に、どの CSV をどのテーブルへ入れるか・一致した列・見出しの表記ゆれ・足りない列を表示する
- **`--config` にプロジェクトのフォルダを指定可能**(`~` も展開)。ほかのフォルダから実行したとき、そのプロジェクトの `.env` も読む(作業フォルダの `.env` と環境変数が優先)。同じ規則を `MONITOR_CONFIG_PATH`(`uvicorn monitor_app.main:app_factory --factory`)と `load_config()` にも適用した
- **`monitor-app --version` / `-V`、`monitor-app --debug`(`MONITOR_DEBUG=1`)**、**`python -m monitor_app`**。`monitor_app.__version__` がインストールされた版を返す(2.3.1 は "2.1.0" を返していた)
- **宣言的設定**
  - 列の型 **`"datetime"`**(SQL の DateTime。時刻順の並べ替え・期間の絞り込みが正しく動く)。SQLite には `YYYY-MM-DD HH:MM:SS`(1 秒未満があるときだけ `.ffffff` を付ける)で保存するため、ビュー・表・グラフのラベル・CSV のエクスポートに `.000000` が付かず、文字列の順が時刻の順になる(生の SQL の `ORDER BY ts`・`WHERE ts >= '…'` が動く)。MySQL / MariaDB の新しい表は `DATETIME(6)`(1 秒未満を丸めない)、PostgreSQL は `timestamp`。読み取りは寛容で、2.3 の文字列の列を `datetime` に変えたときの `2026/10/21 8:00` などの値も日時として読み、読めない値はそのまま返す(500 にしない)
  - **`TableDef(retention=Retention(keep_days=..., keep_rows=..., column=...))`** — 古い行を `MONITOR_RETENTION_INTERVAL_MINUTES`(既定 10 分)ごとに、小分けにして削除する。文字列の列では行ごとにその行の形(`T` 区切り・空白区切り・日付だけ)で比べるため、形が混ざっても新しい行を消さない。空の値と ISO 形式でない値は消さない
  - **`ViewDef(max_rows=N)`** — ビューが返す行数の上限。上限なしのビューが 50,000 行を超えると 1 回警告する
  - `Retention`・`DashboardConfig`・`SourceDef` を `monitor_app` から直接 import できる
- **REST API**
  - **`POST /api/ingest/{table}?on_conflict=error|ignore|update`** — `ignore` は既存の主キーを読み飛ばし(PLC・MES の再送が安全)、`{"success":true,"inserted":n,"skipped":m}` を返す。`update` は上書き(送っていない列は残す)。`error`(既定)は重複で 409
  - **`GET /api/views/{view}/export?bom=1`** — UTF-8 の BOM を付ける(Excel で日本語が化けない)。画面のエクスポートのリンクは BOM 付き
  - **`GET /api/views/{view}/export?raw=1`** — 保存した値をそのまま書き出す(既定では数式として実行されないようにする。Security を参照)
  - **`GET /api/views/{view}` と `/api/views/batch` の `ETag` と `304 Not Modified`**
  - **`/api/health`** の本文に `version`・`database`・`tasks`(バックグラウンド処理ごとの `running`・`last_tick`・`last_error`)を追加。DB に接続できなければ 503。`HEAD` にも応答
- **Docker** — 動くコンテナイメージを `ghcr.io/mikawa-bushi/monitor-app` に公開(linux/amd64・linux/arm64。タグ `X.Y.Z`・`X.Y`・`X`・`latest`)
  - 何もマウントしなければデモ、空のフォルダならサンプルを書き込み、プロジェクトのフォルダをマウントすればそれを動かす。コンテナはフォルダの所有者のユーザーに切り替えて動く
  - ヘルスチェック(`--port` にも追従。プロキシの環境変数を無視し、nobody で動く)、`docker stop` は約 1 秒、`docker run IMAGE check` のようにほかのコマンドも実行できる。`docker run IMAGE --version`(`-V`)は版を表示し、`--help` はマウントしたフォルダにデモを書かずに runserver のヘルプを表示する。書き込めない保存先などは 3 行で原因と直し方を示す
  - 権限を絞った実行(`--cap-drop ALL --cap-add SETUID --cap-add SETGID --cap-add CHOWN --security-opt no-new-privileges`)に対応。`SETUID` / `SETGID` が無くてフォルダの所有者に切り替えられないときは、足りない権限を示して止まる
  - チェックアウトからデモを動かす `compose.yaml`、ビルドは `docker build -t monitor-app .`
- **リバースプロキシ** — `MONITOR_ROOT_PATH=/monitor` でサブパス配信(プレフィックスを外すプロキシでも外さないプロキシでも動く。nginx・Caddy で確認)。ページ・静的ファイル・API・ログインのリダイレクト・Cookie がサブパスの中に収まる。`MONITOR_FORWARDED_ALLOW_IPS` で `X-Forwarded-*` を信頼するプロキシを指定
- **新しい実行時設定** — `MONITOR_SQLITE_JOURNAL_MODE`・`MONITOR_SQLITE_SYNCHRONOUS`・`MONITOR_SQLITE_BUSY_TIMEOUT_MS`・`MONITOR_DB_WAIT_TIMEOUT`・`MONITOR_CSV_ENCODING_FALLBACK`・`MONITOR_VIEW_CACHE_MAX_AGE_MS`・`MONITOR_BACKUP_DIR`・`MONITOR_BACKUP_INTERVAL_HOURS`・`MONITOR_BACKUP_KEEP`・`MONITOR_RETENTION_INTERVAL_MINUTES`・`MONITOR_ROOT_PATH`・`MONITOR_FORWARDED_ALLOW_IPS`・`MONITOR_COMPRESS_RESPONSES`・`MONITOR_ACCESS_LOG`・`MONITOR_LOG_FORMAT`(`json` で 1 行 1 つの JSON)・`MONITOR_SMTP_SECURITY`(`starttls` / `ssl` / `none`)・`MONITOR_NOTIFY_RETRIES`・`MONITOR_SMTP_TLS_VERIFY`(SMTP の証明書の検証。既定 `true`)・`MONITOR_ALLOWED_HOSTS`(受け付けるホスト名)・`MONITOR_API_DOCS`(API ドキュメントを出すか。既定 `true`)・`MONITOR_VIEW_CACHE_MAX_MB`(ビューのキャッシュのメモリの上限。既定 256。`0` で上限なし)。一覧は README の「実行時設定」と `.env.example`
- **定期バックアップ** — `MONITOR_BACKUP_DIR` を設定すると、稼働中でも安全な方法で `MONITOR_BACKUP_INTERVAL_HOURS`(既定 24 時間)ごとにバックアップする。期限を過ぎていれば起動時にすぐ取る(夜に電源を切る PC でも取れる)。SQLite のみ
  - 複数のプロジェクトで 1 つのフォルダを共有できる。各 DB はファイルの先頭に乱数の ID を 1 回だけ書き(`PRAGMA application_id`。データは変えない)、世代の整理(`MONITOR_BACKUP_KEEP`)・「最後のバックアップ」・`monitor-app backup` の件数は自分のバックアップだけを数える(以前のバックアップも数える)。ほかの DB のバックアップがあれば 1 回警告する。`cp` で複製した DB は同じ ID になる
  - 時計が狂って未来の日時になったバックアップがあっても定期バックアップは止まらない(夏時間のため 1 時間は許す。そのファイルを 1 回警告する)
  - 止められたバックアップ・復元が残した一時ファイル(`.<DB 名>-XXXXXXXX.tmp` と `-journal`)は、1 時間たてば起動時とバックアップのたびに消す
- **通知** — Webhook / メールの送信に失敗したら 1・2・4…秒(最大 30 秒)待って再試行する(`MONITOR_NOTIFY_RETRIES`、既定 2 回)。停止時は送信待ちの通知を送ってから終了する(最大約 5 秒)。起動時に、未知の通知先・接続情報の足りない通知先を警告する。通知は状態が変わった順(発生 → 復帰)に必ず届く
- **2.3.x に戻すための関数** — `monitor_app.db.ingest_state.unwrap_typed_cursors(URL)` が `sources` の読み取り位置を 2.3.x の読める形に戻す(手順は「Upgrade notes」)
- **Python API の範囲を README に明記** — 互換性を守る名前(`monitor_app.__all__`・`create_app`・`app_factory`・`load_config`・`AppSettings`・`integrations`)と、それ以外は内部用であること
- **起動時の表示** — 版・URL・DB(パスワードは伏せる)を 1 行で表示。`0.0.0.0` などで公開し `MONITOR_API_KEY` が無いときは目立つ警告と、ほかの PC から開く URL を表示する。config.py と既存の DB の表の違いは、直し方つきで 1 件ずつ警告する(起動は止めない)
- **アクセシビリティ** — アラートのバナーをスクリーンリーダーが読み上げる(重大は割り込み)。ナビゲーションに名前を付け、モバイルのメニューは Esc で閉じる

### Changed
- **SQLite を WAL モード・`synchronous=normal`・`busy_timeout=5000` で使う** — 読み書きが互いを待たず、書き込みのたびの fsync も大幅に減る。DB の隣に `-wal`・`-shm` ができる。稼働中に `database.db` だけをコピーすると最新の行が欠けるため、`monitor-app backup` を使う。ネットワーク共有(UNC・ネットワークドライブ・NFS/SMB・WSL の `/mnt/c`・Docker Desktop の共有フォルダ)では自動で `delete`。従来に戻すには `MONITOR_SQLITE_JOURNAL_MODE=delete`
- **CSV の文字コードの既定を `auto` に**(`MONITOR_CSV_ENCODING`。旧既定は `utf-8-sig`) — UTF-8(BOM の有無を問わない)で読めなければ `MONITOR_CSV_ENCODING_FALLBACK`(既定 cp932)で読み、どの文字コードを使ったかを警告する。どれでも読めなければファイル名と設定名を示す短いエラー。UTF-8 のファイルに壊れたバイトが少しあっても、ファイル全体(追記の監視では以後のすべての行)を cp932 で読み直さず、その行だけを行番号つきの警告(「m.csv 29001行目: 文字コード UTF-8 として読めないバイトを含むため、この行は取り込みません」)で読み飛ばす。文字コードを決めた設定(`MONITOR_CSV_ENCODING=cp932` など)でも 1 行だけなら読み飛ばすが、ほとんど読めないファイルは従来どおりエラー(追記の監視はその位置で止まり、行を捨てない)。**移行**: 西欧の Windows の CSV(cp1252)は失敗せずに文字化けして取り込まれるため、`MONITOR_CSV_ENCODING=cp1252` を設定する
- **置き換えの取り込みが、宣言した列の無い CSV を拒否する** — 主キー以外の列が CSV の見出しに無いと、そのテーブルは置き換えずに既存の行を残す(従来はその列を NULL で置き換えていた)。`import-csv` は終了コード 1、`runserver --import-csv` は警告して起動する。**移行**: 意図どおりなら `--force`
- **見出しの表記ゆれを吸収** — 大文字・小文字、全角文字、前後の空白、BOM の違いを宣言した列に合わせて取り込む(従来は黙って捨てていた)。どのテーブルにも合わない CSV は一覧にする
- **取り込みは 1 つのトランザクション** — 失敗すると何も変わらない(従来は途中のテーブルまで確定していた)。外部キーの順に処理する(子を先に削除し、親を先に挿入)。PostgreSQL / MySQL の置き換え・`--if-empty` の取り込みでは、自分自身を参照する表で子が親より前にある CSV も取り込める
- **SQLite の置き換えの取り込みが DB をロックするのは入れ替えの間だけ** — `import-csv`・置き換えモードの監視・`runserver --import-csv` は、先に CSV を一時的な表に読み込み、最後の削除と挿入の間だけ書き込みのロックを持つ。大きな取り込みの間もフォーム・画面・API の書き込みが待ちの上限で失敗しない。取り込み全体の時間は数 % 延びることがあり、表のデータと同じくらいの一時的な容量(SQLite の一時フォルダ)を使う。一時的な表を作れなければ従来の方法で取り込む
- **取り込みの問題をファイルと行で知らせる**
  - CSV の中の主キーの重複は、ファイル・両方の行・キーを示して取り消す(「users.csv 3行目: 主キー id=1 が 2行目と重複しています。取り込みは取り消しました(データは変わっていません)」)
  - PostgreSQL / MySQL で列に入らない値(255 文字を超える文字列、列の範囲を超える整数)は、その行だけを警告して読み飛ばす(「lim.csv 3行目: 列 note の値 '…' が長すぎます(300 文字。PostgreSQL のこの列は 255 文字まで)」)。従来は 1 つの値で取り込み全体が失敗した。`import-csv`・フォルダの監視・`sources` に適用。DB が拒否したそのほかの値は、ファイルと行の範囲を示し、何も変えなかったことを伝える
  - NUL(SD カード・FAT での停電で残る)は解析の前に取り除く。CSV として解析できない行(131072 文字を超える項目など)は、監視を止めたりトレースバックを出したりせず、警告して読み飛ばす。見出しを解析できないときは短いエラー(「<ファイル> の見出し(1 行目)を CSV として読めません」、コード `CSV_FORMAT_ERROR`)。警告に含める長い値は切り詰める
- **`import-csv` の表示** — 取り込みの注意は 1 回だけ(結果の一覧に出し、ログには重ねて出さない)。テーブルごとの行は「users: 0 行 (skip 0、既存 5 行)」。`--keep` の既存のキーの注意に ⚠️ を付けない。見出しの見えない文字は `\ufeff` のように表示する。`serve` も取り込みの注意を要約に 1 回だけ表示する
- **小数の列の `NaN`・`inf`・`-inf`・`Infinity`(JSON の NaN / Infinity も)は空(NULL)として保存する**(どの DB でも `AVG`・`MAX` が正しくなり、MySQL で取り込み全体が失敗しない)。プロセスごとに 1 回、ロガー `monitor_app.coercion` に INFO を出す
- **`--keep`(追記)は主キーが既にある行を読み飛ばして件数を報告する**(従来は IntegrityError で失敗)。主キーの列が無い CSV は毎回追記される。画面で削除した行も CSV に残っていれば入れ直す
- **値がすべて空の行(Excel の `,,,` の行)は読み飛ばす**(従来は空の行として登録)
- **入力フォームのあるテーブルを CSV で置き換えると警告する** — `runserver --import-csv-if-empty` か `import-csv --keep` を案内する。Docker イメージの起動コマンドは `runserver --import-csv-if-empty`(従来は `--import-csv` で、起動のたびに入力が消えていた)
- **日付・日時の受け付ける形式を拡大** — `2026/10/21`・`2026/1/5`・`2026/10/21 8:00`、ISO 8601 の `T`/空白区切り・秒の省略・小数秒・`Z`/`+09:00`。タイムゾーンつきの値は PC のローカル時刻に変換して保存する。REST API とフォームの入力にも適用
- **アラートはビューの全データでだけ評価する** — `?limit=` のページ取得では評価しない(同じ通知の繰り返しを防ぐ)。同じデータは 1 回だけ評価し、アラートだけが変わったときも SSE で送る
- **ビューのキャッシュ** — 同じビューへの同時の要求は 1 回の計算を共有する。ファイルの SQLite では `PRAGMA data_version` で変更を検知し、変わらない結果を `MONITOR_VIEW_CACHE_MAX_AGE_MS`(既定 15 秒)まで使う(ほかのプロセスの書き込みもすぐ反映)。時刻に依存する SQL(`now`・`CURRENT_TIMESTAMP`・`random()` など)と PostgreSQL / MySQL は従来どおり `MONITOR_VIEW_CACHE_TTL_MS`。KPI も同じキャッシュを使う
- **`/api/views/batch` と SSE の JSON を `GET /api/views/{view}` と同じにした** — PostgreSQL / MySQL の DECIMAL は文字列(`"10.500"`)、INTERVAL は ISO 8601、日時は `T` 区切り。NaN と Infinity は `null`(従来は 500)。**移行**: `/api/views/batch` の DECIMAL を数値として扱っていたクライアントは変換する
- **レスポンスの gzip 圧縮を既定で有効に**(1 KiB 以上。SSE は圧縮しない。`MONITOR_COMPRESS_RESPONSES=false` で無効)
- **静的ファイルの URL に版と内容のハッシュを付け、1 年キャッシュする** — 更新後は自動で新しい JS / CSS を読む。HTML の URL はホスト名を含まない形になった
- **画面の更新** — 前の要求が終わってから次を送り、失敗したら最大 30 秒まで間隔を延ばす。応答の無い要求は 60 秒で打ち切る。データが変わらなければ描き直さない。5 秒以上隠れたタブは SSE を止め(通知は続く)、表示されたら最新のデータで再開する
- **ダークテーマの文字色** — 表・フォーム・ログイン・ボタン・リンクのコントラストを WCAG AA(4.5:1 以上)にした。`bg-*` だけを指定したセルには読みやすい文字色を自動で付ける
- **DB のエラーを JSON で返す** — CRUD と取り込みの API で、主キーの重複・参照中の行は 409 `CONFLICT`、長すぎる値・範囲外は 422 `INVALID_PAYLOAD`、DB のロックの待ちの上限は 503 `DATABASE_BUSY`(`Retry-After: 2`。ログはトレースバックなしの WARNING)、表定義の不一致は 500 `DB_SCHEMA_MISMATCH`、その他は 500 `DATABASE_ERROR`(詳細はサーバーのログ、応答には `ref=`)。そのほかの処理されなかった例外も、text/plain ではなくセキュリティヘッダつきの JSON(500 `INTERNAL_ERROR`、`ref=` つき)で返す。従来は text/plain の 500 だった。SQL や送られた値は応答に含めない
- **`GET /api/tables/{table}?limit=&offset=` のページは主キーの順**(行を編集してもページがずれない)。limit なしの一覧の順は変えない
- **CLI のエラーは最大 3 行の短いメッセージを stderr に出す**(`--debug` / `MONITOR_DEBUG=1` でトレースバック)。設定の値の誤り(`MONITOR_PORT=abc` など)、範囲外のポート、不正な `MONITOR_LOG_LEVEL`、使用中のポート、DB の URL の誤り(`postgres://`、`mysql://`、パスワードの `@` など)も短く説明する。`--help` はコマンドを使う順に並べ、すべて日本語で説明する
- **`runserver`** — 取り込みの前に config.py を検証し、ポートが空いているかを確かめる。ワーカーは常に 1 プロセス。uvicorn のログにも時刻が付き、アクセスログは stderr に出る(従来は stdout)。`--reload` で `--config` のフォルダの config.py も監視する。権限の無いポート(1024 未満、Windows の Hyper-V の予約)・名前を引けないホスト・この PC のものでないアドレスは、uvicorn のエラー(終了コード 3)ではなく、取り込みの前にヒントつきで終了コード 1 にする(`serve` も同じ)
- **`--config <別のフォルダ>` で空の DB を作らない** — 相対パスの DB(取り込むときは CSV のフォルダも)が作業フォルダに無く config.py のフォルダにだけあるとき、空の DB を作って動かずに、`cd <プロジェクトのフォルダ>` か絶対パスの `MONITOR_DATABASE_URL` / `MONITOR_CSV_DIR` を案内して終了コード 1 で止まる(`check` は警告、`sync-sources` は CSV のフォルダについて警告だけ)。相対パスは従来どおり作業フォルダが基準。0.1.x の `config/config.py` の構成に出ていた誤った警告は出なくなった
- **CLI のエラーの案内** — SQLite の権限のエラーは書き込めないファイル・フォルダを名指しし、所有者と Docker のマウントの直し方を示す(SQLite にホスト・パスワードの案内を出さない)。WAL への切り替えの警告は「ほかのプロセスが使用中」と「読み取り専用」を区別する。`postgres://` の URL の案内は、psycopg が無ければ同梱の pg8000 を勧める
- **未知の `MONITOR_*` の点検** — 接頭辞 `MONITOR_` を除いた名前で候補を探し、`MONITOR_DB_URL`・`MONITOR_DB`・`MONITOR_DATABASE` には `MONITOR_DATABASE_URL` を勧める。`MONITOR_NAME`・`MONITOR_URL` のような無関係な変数に的外れな候補を出さず、認証のエラーにもしない(警告。終了コード 0)。文言は「環境変数の MONITOR_X は monitor-app の設定ではありません」
- **PostgreSQL / MySQL に接続できるまで起動時に待つ**(`MONITOR_DB_WAIT_TIMEOUT`、既定 15 秒。パスワード違いなどはすぐ失敗する)。`import-csv`・`sync-sources` も同じ。待っている間も Ctrl+C・`docker stop`・`systemctl stop` ですぐ止まる。1 回の接続は、URL で指定しない限り 10 秒で諦める(応答しないホストでも `MONITOR_DB_WAIT_TIMEOUT` を守る。長いクエリには影響しない)。`check` は表定義の読み取りを 3 秒で諦める(従来は約 21 秒)
- **停止が速く安全に** — `docker stop`・`systemctl stop`・Ctrl+C で、実行中のバックグラウンドの処理(フォルダの監視の取り込み・保持期間による削除・アラートの評価・`sources`)を次の SQL の実行時に打ち切り、トランザクションを取り消す。待つのはタスクごとに最大 3 秒(全体で 4.5 秒)で、それを過ぎたら待たずに終了する。ログに「<タスク>: 停止の指示を受けて処理を打ち切りました」(INFO)・「<タスク>: 実行中の処理が 3 秒以内に終わらないため、待たずに停止します」(WARNING)を出す。SSE の接続も停止時にきれいに閉じる(画面ごとの「ASGI callable returned without completing response」のエラーが出なくなった)
- **1 回きりのコマンドも SIGTERM で止まる** — `import-csv`・`check`・`migrate`・`sync-sources`・`startproject`・`deploy` は、Docker の PID 1 でも DB の起動待ちの間でも SIGTERM で止まり、「停止の指示(SIGTERM)を受けたため、<コマンド> を中止しました」を表示して終了コード 143 で終わる
- **多くの閲覧者でも API が遅くならない** — SSE は専用の 8 つのスレッドを使い、計算中のビューを待つ閲覧者はスレッドを持たない。`/api/health` も専用のスレッドで答える(Docker のヘルスチェックが誤って unhealthy にならない)。SQLite で、小さな読み取りが続いても大きな表の読み取りが止まらない
- **ビューのキャッシュのメモリに上限** — `MONITOR_VIEW_CACHE_MAX_MB`(既定 256)を超えたら、最近使っていない結果から捨てる。上限の 1/16 を超えるページ(`?limit=`)の結果はキャッシュしない。期限の切れた結果は新しい結果を入れなくても捨てる。ビューの表示・SSE・KPI の応答は `Cache-Control: private, no-cache`(共有のプロキシに保存させない)
- **新しく作る表** — 小数の列は倍精度(DOUBLE)、`_ingest_state` の列は VARCHAR(255)。PostgreSQL / MySQL では主キー・外部キー・その参照先以外の整数の列を `BIGINT` にする(Unix 時刻のミリ秒も入る。主キー・外部キーは INTEGER のまま)。既存の表は起動時に変更しない(以前の INTEGER の列に入らない値の行は警告して読み飛ばす)。MySQL のセッションは `NO_AUTO_VALUE_ON_ZERO`(明示した id 0 を保つ)
- **`sources`** — 取り込んだ行とカーソルを同じトランザクションで保存する。ウォーターマークのカーソルは型を保った JSON(`{"v":11}`)で保存する(2.3 以前の文字列のカーソルも読める)。ウォーターマークが NULL の行と、JSON のオブジェクトでないレコードは警告して読み飛ばす。カーソルは書き込みのトランザクションの中で読み直すため、cron の `sync-sources` とフォルダの監視が同じソースを同時に読んでも行が二重にならない。`kind="json"` のソース(と、`query` があり `watermark_column` の無い sqlite のソース)は既定の追記モードで、主キーが既にあるレコードを読み飛ばして新しいものだけを足す。`sync-sources` は実際に追加した行数を表示する
- **追記モードの監視**(`MONITOR_INGEST_MODE=append`)は、ファイルの続きのバイトだけを読む。書きかけの最後の行は完成してから取り込む。ファイルがローテート・書き直しされたら先頭から読み直す(主キーが既にある行は読み飛ばす)
  - 読み取り位置を DB(`_ingest_state` の `csv:<テーブル>`)に、取り込んだ行と同じトランザクションで保存する。サーバーを止めている間に追記された行も次の起動で取り込む(Docker・systemd の既定の `runserver --import-csv-if-empty` を含む。2.3 は止めていた間の追記をすべて取りこぼしていた)。止めている間に差し替え・短縮されたファイルは先頭から読み直す。`import-csv`・`--keep`・`--import-csv` は、書き込んだテーブルの読み取り位置を消す
- **フォルダの監視は失敗した CSV を取り込み直す** — 「database is locked」などで失敗したファイルは、次の確認で、その後は間隔を倍にしながら(最大 60 秒)、ファイルが変わればすぐに取り込み直す。成功するまで `/api/health` がエラーを示す。直らないファイルは取り込み直すたびに 1 回警告する(「(N 回目。…秒後以降にもう一度取り込みます)」)
- **置き換えモードの監視** は、同じ回に変わった CSV をまとめて 1 トランザクションで置き換える(関連する CSV を揃えて差し替えても外部キーに違反しない)
- **config.py の未知のキーを警告する**(ConfigWarning。行番号と候補つき)。読み込みは続ける。`python -W error` ではエラー。config.py の誤りは、モデル名・行番号つきの日本語で説明する。ビューと KPI の SQL の末尾の `;` を許す(従来はページ送りが壊れた)
- **`sqlite:///~/…` の `~` をホームフォルダとして展開する**。SQLite の DB のフォルダ(`instances/` など)は無ければ作る
- **メール** — `MONITOR_SMTP_SECURITY=ssl` / `none` で `MONITOR_SMTP_PORT` を省略すると 465 / 25 を使う(既定の `starttls` は従来どおり 587)
- **雛形(startproject)** — config.py は列の型を明示し、`datetime`・`Retention`・`max_rows` の例をコメントで示す。`orders_summary` は新しい 500 件を表示する。`.env.example` はすべての `MONITOR_*` を説明する
- **依存パッケージの下限** — SQLAlchemy 2.0.8(未満は PostgreSQL で 256 文字以上の文字列を 255 文字に切り詰める)、sse-starlette 3.3(未満はリアルタイム更新や停止が不安定。3.3 から停止時に SSE をきれいに閉じられる)、jinja2 3.1.6(未満に既知の脆弱性)、PyMySQL 1.2(未満は MySQL 8 の再起動後に cryptography なしで接続できない)、uvicorn 0.47(SSE の接続中でも SIGTERM で止まる)、typer 0.26(click の固定が不要。`click<8.2` の制約を外した)。extra **`[postgres]`**(psycopg。高速な PostgreSQL ドライバ)を追加
- **開発・CI** — Poetry から uv + hatchling に移行(`uv sync --all-extras`、`uv.lock`)。CI は Python 3.10〜3.14(Linux)・Windows・macOS、最低バージョンの依存、PostgreSQL 16 / MySQL 8.4、wheel と Docker イメージのスモークテストを実行する。リリースは `v*` タグからだけ行い、全テストに通った版を PyPI と GHCR に公開する。GitHub Actions はコミットの SHA で固定し(版はコメント)、チェックアウトは認証情報を残さない。Dependabot(`.github/dependabot.yml`)が actions の更新を週 1 回、公開から 7 日後に提案する。sdist に `docker/entrypoint.sh`・`docker/healthcheck.py` を含め、sdist からテストを実行できる。テストは開発者のシェルの `MONITOR_*`(`MONITOR_TEST_*` は除く)とリポジトリ直下の `.env` を無視し、各テストを空の一時フォルダで実行する。ruff で関数の複雑さ(C901、上限 20)を検査する

### Fixed
- **PostgreSQL / MySQL で使えない問題を修正**
  - `import-csv` と `runserver --import-csv` の 2 回目以降が外部キーで失敗した
  - CSV の id を取り込んだあと、画面・API での追加が主キーの重複で失敗した(PostgreSQL の連番を進めるようにした。連番は戻さない)。2.3.x で入れた DB で遅れたままの連番も、最初の `runserver` / `serve` / `import-csv` / `sync-sources` / 監視の起動と `migrate` で 1 回だけ進める(「テーブル 'users' の連番を表の最大値に合わせました(0 → 5)」)。遅れていない連番には触れないため、特別な権限は要らない。進められなければ、管理者が実行する SQL(`SELECT setval(pg_get_serial_sequence('users', 'id'), (SELECT MAX(id) FROM users))`)を警告に示す。稼働中にほかのプロセスが id を指定して追加しても、次のフォーム・API の追加が連番を進めて 1 回だけやり直す(409 にしない)
  - MySQL で、自分自身を参照する外部キーの表(`parent_id` が同じ表を指す)の置き換えの再取り込みが必ず失敗した
  - 256 文字以上の文字列が 500 になった(422 で理由を返す)、MySQL で `sources` が動かなかった、サーバーに切られた接続を使って失敗した、MySQL で明示した id 0 が振り直された
  - 主キーが NULL のレコードが PostgreSQL で NOT NULL 違反になった
- **CSV の取り込み**
  - 見出しの前後に空白がある・大文字小文字が違う列が、黙って NULL で取り込まれた
  - 追記モードの監視が、書きかけの最後の行を途中までで保存した
  - `import-csv --keep` の 2 回目が IntegrityError で失敗した
  - 1 回の取り込みでレコードごとにキーが違うと、最初のレコードに無い列が捨てられた(または 500)
  - `instances/` フォルダが無いと `unable to open database file` で起動できなかった
- **`sources`** — 取り込みの途中で落ちると主キーの重複エラーが毎回続いた。ウォーターマークが NULL の行で毎回失敗・重複した。型の無い SQLite の列で 2 回目以降が取り込まれなかった。JSON のオブジェクトでないレコードで落ちた。`kind="json"` のソースが既定の追記モードで、ファイルが変わった後のポーリングのたびに主キーの重複エラーになった。cron の `sync-sources` と監視が同じソースを読むと行が二重に入ることがあった
- **フォルダの監視** — 取り込む前に CSV を処理済みとして記録していたため、一時的な失敗(DB のロックなど)の後に取り込み直さず、`/api/health` も正常に戻っていた。mtime だけで変更を見ていたため、時刻の刻みが粗いファイルシステム(Windows は約 16 ms、FAT は 2 秒)で続けて書き込むと、次の書き込みまで取り込まれなかった(サイズの変化でも検知する)
- **アラート** — API でページ単位(`?limit=`)に読むと、アラートの発火と復帰が繰り返され、同じ通知が何度も送られた
- **エクスポート** — 日本語など ASCII 以外の名前のビューのエクスポートが 500 になった(RFC 5987 の `filename*` で正しいファイル名を返す)
- **画面**
  - ダークテーマで表・フォームのラベル・ログイン画面の文字がほぼ読めなかった(コントラスト 1.05:1)
  - 応答の返らない要求が 1 つあると、大型表示・ダッシュボードの更新が止まったままになった
  - 大型表示で、切り替えた直後に前のビューのデータが一瞬表示された。進捗バーがデータの更新のたびに最初に戻った
  - 表のタブを 6 つ以上開くと、同じサーバーのほかのページが開けなくなった(隠れたタブは接続を閉じる)
  - `#`・`?`・`%`・空白を含むビュー名のリンクが壊れていた
  - 存在しないビューの SSE が、応答の開始後にエラーになった(404 の JSON を返す)
  - v1 形式の config.py の `FAVICON_PATH='favicon.ico'` が 404 になった(同梱のアイコンを表示)
- **CLI** — 日本語版 Windows で出力をリダイレクトすると `check` などが UnicodeEncodeError で失敗した。設定の値の誤り・範囲外のポート・不正な `MONITOR_LOG_LEVEL` で長いトレースバックになった(`check` を含む)。`runserver` で `MONITOR_LOG_LEVEL=warn` などが動かなかった
- **停止** — 起動時の CSV の取り込み中や DB の起動待ちの間に `docker stop` / `systemctl stop` しても止まらず、強制終了(SIGKILL)されていた(0.2 秒以内に止まり、取り込みは取り消す)。停止時に送信待ちの通知が失われた
- **v0.x の表** — 主キーの無い表へのレコードの追加が、存在しない id を返していた(500 `DB_SCHEMA_MISMATCH` で理由を返し、取り消す)
- **Docker** — 従来の `docker/Dockerfile` は v2 では動かなかった(新しいイメージで解決)

### Performance
数値は開発用の PC(8 コア・Python 3.13・ファイルの SQLite)で v2.3.1 と交互に測った中央値で、環境によって変わる(ほかの処理で CPU が混んでいる間に測ったため、絶対値より比を目安にしてほしい)。DB と CSV のファイルは RAM ディスク(tmpfs)に置いた。「SQLite の書き込み」だけは Docker コンテナから NVMe の実ディスクで測った。
- **CSV の取り込み** — 100 万行(57 MB・8 列): 6.0 秒・最大 584 MB → 4.7 秒・約 60 MB(約 1.3 倍速い)。20 万行: 1.16 秒・159 MB → 0.93 秒・61 MB。cp932 の 20 万行: 1.15 秒 → 0.95 秒(自動判別でも同じ)。メモリは行数によらずほぼ一定。`--keep` で 100 万行すべてが既存: 失敗 → 約 2.8 秒で全件読み飛ばし
- **追記モードの監視**(100 万行の CSV) — 起動 3.2 秒 → 0.1 秒、追記 10 行の取り込み 3.2 秒 → 約 1 ミリ秒、メモリ 557 MB → 67 MB
- **同時アクセス**(20 万行の DB) — 同じ 1 万行のビューへの同時 30 リクエスト: 約 40 秒 → 約 40 ミリ秒(SQL の実行 30 回 → 1 回。従来の時間は CPU の負荷によって 7〜40 秒と大きく変わる)。別々の 1 万行のビュー 30 個: 40 秒 → 0.65 秒。10 万行のビューへの同時 10 リクエスト: 70〜115 秒 → 0.2 秒
- **SSE 30 クライアント**(1 万行のビュー、毎秒 5 行の書き込み) — サーバーの CPU 182 % → 12 %、最初のイベントまで 15 秒 → 0.4 秒、書き込みのタイムアウトなし(従来は 1 件に 30 秒)
- **待機中**(10 万行のビューにアラート 1 件、閲覧者なし) — CPU 15.8 % → 約 1 %
- **API の応答** — キャッシュ済みの 10 万行のビュー(13.6 MB): 72 → 5 ミリ秒。gzip で受け取るブラウザには圧縮済みの 1.7 MB を約 1.5 ミリ秒で返す(圧縮はデータが変わったときに 1 回だけ)。ダッシュボードの `/api/views/batch`: 63 → 1.3 ミリ秒(gzip では約 10 ミリ秒)、キャッシュなしの 10 万行のビュー: 442 → 199 ミリ秒、変化なしの 304: 約 0.8 ミリ秒・0 バイト。表の一覧(20 万行): 812 → 740 ミリ秒
- **表の一覧(`/api/tables/{table}`)の大きな読み取りが重なったとき** — 1 万行の一覧を 8 つ同時に: 8.9 秒 → 0.14 秒。10 万行の一覧を 4〜8 つ読み続けている間、それらの読み取りは約 2 倍進み、その間の小さな要求の中央値は約 30 → 10〜15 ミリ秒(ただし遅い側の 5 %(95 パーセンタイル)は 70〜120 → 180〜220 ミリ秒に延びた。CPU が混んだ状態での測定)
- **SQLite の書き込み**(WAL + normal、NVMe) — 1 件の追加の中央値 1.1 → 0.26 ミリ秒、400 件の書き込みでの fsync 1,608 回 → 6 回
- **転送量** — gzip で `bootstrap.css` 233 KB → 32 KB、2 MB のビューは約 7.7 分の 1(圧縮約 14 ミリ秒)。静的ファイルは再訪問時に要求しない
- **Docker** — イメージ 336 MB(圧縮時 77 MB)、`docker run` から最初の応答まで約 1.2 秒、SSE の接続があっても `docker stop` 約 0.8 秒

### Security
- **秘密情報を表示しない** — DB の URL のパスワード(`@` を含む場合も)と `?password=`・`?pwd=`・`?token=` などのクエリの値を起動ログ・エラーから伏せる。Webhook の URL をログに出さない。設定の値の誤りの説明でも、パスワード・キー・URL の値は表示しない。CLI の例外表示にローカル変数を出さない
- **API キーなしの LAN 公開を警告** — `runserver` / `serve` の起動時と `check`。`MONITOR_API_KEY`・`MONITOR_PROTECT_READS` の綴りの誤り(認証が無効のまま起動する)を `check` がエラーにする
- **DB のエラーの応答に SQL や送られた値を含めない**(詳細はサーバーのログだけ)
- **Cookie** — サブパス配信ではログインの Cookie をサブパスに限定する。`X-Forwarded-*` を信頼するプロキシを `MONITOR_FORWARDED_ALLOW_IPS` で指定でき、そのプロキシを通した HTTPS では Cookie に `Secure` が付く
- **通知** — `MONITOR_SMTP_SECURITY=none` で認証情報を平文で送る設定を警告する
- **バックアップのファイルは所有者だけが読める**(0600)
- **空の `MONITOR_API_KEY`**(`.env` の空の行、未設定の Compose の変数)を「キーなし」として扱う。従来は認証が有効に見えるのに、誰でも作れるセッションの Cookie で書き込めた。キーが無いときの警告もこの場合に出る
- **API キーをバイト列で比べる** — ASCII 以外のキーや `X-API-Key` ヘッダで 500 にならない(誤りは 401)。日本語のキーはブラウザのログインで、また UTF-8 で送るクライアントの `X-API-Key` で使える。`check` と起動時のログは、説明用の値(`change-me`・`secret`・`<...>` など)・16 文字未満・ASCII 以外のキーを警告する
- **ログインの Cookie** — 値は `<発行時刻>.<署名>` になり、サーバーが 12 時間(Cookie の Max-Age と同じ)で拒否する。ログアウトするとサーバー側でも(再起動まで)無効になる
- **SMTP の証明書を検証する**(`starttls`・`ssl`)。新しい設定 `MONITOR_SMTP_TLS_VERIFY`(既定 `true`)。社内の CA はプロセスの環境変数 `SSL_CERT_FILE` で指定する(`.env` の行では効かない)。検証できない自己署名の中継サーバーは `MONITOR_SMTP_TLS_VERIFY=false` にし、`check` と起動時に警告する
- **DNS リバインディング対策** — 新しい設定 `MONITOR_ALLOWED_HOSTS`(カンマ区切り。`*.example.com` でサブドメイン、`*` で確かめない)。既定では、`MONITOR_HOST` がループバックで、`MONITOR_ROOT_PATH` も `MONITOR_FORWARDED_ALLOW_IPS` も設定されておらず、接続がループバックから来たときだけ確かめ、`localhost`・`*.localhost`・待ち受けの名前・この PC のホスト名・IP アドレス以外の Host ヘッダを `400 Invalid host header` で断る(断った名前はログに 1 回、直し方つきで出す)。LAN・Docker(0.0.0.0)・案内しているリバースプロキシの構成は影響を受けない。`runserver --host` の値もアプリに渡す。一覧を書くと、どのアドレスでも確かめる
- **API ドキュメント** — `/docs` と `/redoc` は版を固定した Swagger UI 5.33.1・ReDoc 2.5.4 を Subresource Integrity つきで読み込み、Google Fonts を使わない。`MONITOR_PROTECT_READS=true` ではログインが必要で、`/openapi.json` にもキーかセッションが必要。新しい設定 `MONITOR_API_DOCS`(既定 `true`。`false` で 3 つとも出さない)
- **CSV・xlsx のエクスポートで数式を書き出さない** — `=` `+` `-` `@`・タブ・CR で始まる文字列のセルは、CSV では先頭に `'` を付け、xlsx では文字列として保存する。数値は変えない。`?raw=1` で保存した値そのまま
- **エラーの応答** — 処理されなかった例外の 500 にもセキュリティヘッダを付ける。ビュー・バッチ・KPI の応答は `Cache-Control: private, no-cache`
- **配備用の API キー** — `monitor-app deploy`・`startproject --deploy` が推測されにくいキーを `.env`(0600)に書く(Added を参照)。`.env.example` は `change-me` を勧めず、キーの作り方を示す
- **Windows の起動時のタスク** は SYSTEM ではなく登録したユーザー(S4U)で動き、プロジェクトのフォルダがすべてのユーザーから変更できれば警告する。`start-monitor.bat` と systemd のユニットは `PYTHONSAFEPATH=1`
- **Docker** — コンテナはマウントしたフォルダの所有者のユーザーに切り替え、そのとき Linux の特権(capability)をすべて捨てて `no_new_privs` を設定する。`/project` が root の所有なら uid 0 のまま特権を持たずに動き、rootful Docker では `chown` の直し方を 1 行で警告する。ヘルスチェックは `python -I` で、プロキシの環境変数を無視し、nobody で動く。スクリプトの権限を固定し(0755 / 0644)、setuid / setgid の実行ファイルを残さず、`chown` はシンボリックリンクを辿らない。イメージにはコンパイラ・curl・sqlite3 を入れない
- **GitHub Actions** — actions をコミットの SHA で固定し、チェックアウトは認証情報を残さない(Changed を参照)。リリースの `pypi` 環境には承認者を置き、`v*` タグだけに限ることを推奨する
- **依存の下限** — jinja2 >= 3.1.6(既知の脆弱性を除く)
- **開発用の依存** — `uv.lock` の virtualenv を 21.14.13(GHSA-cx6f-vjhg-g3cc)、filelock を 4.2.4(GHSA-v4x8-3wr5-3wq8 など 6 件)に更新。filelock の修正版は Python 3.11 以上にしか無いため、dev グループの pre-commit は Python 3.11 以上でだけ入れる(3.10 では `uvx pre-commit`)。パッケージの実行時の依存には影響しない

### Removed
- 使われていなかった画像 `img/background.webp`・`img/logo.webp`(wheel が約 478 KB 小さくなった。`favicon_path` にこれらを指定していた場合のみ影響)
- `docker/Dockerfile`・`docker/docker-compose.yml`・`docker/.dockerignore`(v2 では動いていなかった)→ リポジトリ直下の `Dockerfile`・`compose.yaml` と `monitor-app deploy docker`
- 開発用の Poetry の設定と `MANIFEST.in`(uv + hatchling に移行)
- 依存の `click<8.2` の固定(typer 0.26 以降は click を同梱する)

### Upgrade notes
- **更新**: `pipx upgrade monitor-app`(または `uv tool upgrade monitor-app` / `pip install -U monitor-app`)。そのあと `monitor-app check --data` で、直すべき点(綴りの誤り・DB の表との違い・CSV の問題)を確かめる(DB は変更しない)
- **起動コマンド**: `runserver --import-csv` を起動のたびに使っていた場合は `runserver --import-csv-if-empty` に変える(画面・フォームから入力した行が消えなくなる)
- **バックアップ**: 初回の起動で SQLite が WAL になる。DB ファイルのコピーによるバックアップは `monitor-app backup` か `MONITOR_BACKUP_DIR` に切り替える
- **文字コード**: cp1252 の CSV を使っている場合は `MONITOR_CSV_ENCODING=cp1252`
- **列を足した config.py**: `monitor-app migrate --dry-run` で SQL を確かめ、`monitor-app migrate`
- **MySQL**: 2.3 以前に作った表の小数の列は単精度(`1234567.89` → `1234570`)。起動時の警告と `migrate` が示す `ALTER TABLE <表> MODIFY <列> DOUBLE;` を実行する
- **ログ**: アクセスログを stdout から読んでいた場合は stderr から読む
- **ログインし直す**: 2.3.x のブラウザのセッションは使えなくなるため、利用者は 1 回ログインし直す
- **空の `MONITOR_API_KEY=`**: 「キーなし」として扱う(従来は回避できる認証だった)。本物のキーを書くか、`monitor-app deploy` で生成する
- **メールの通知**: 自己署名などの正しくない証明書の SMTP の中継サーバーは、環境変数 `SSL_CERT_FILE`(社内の CA)か `MONITOR_SMTP_TLS_VERIFY=false` を設定するまで送れない(失敗のログに両方の設定を示す)
- **同じマシンのリバースプロキシ**: ルートのパスで公開し、元の Host ヘッダのまま 127.0.0.1 で待ち受ける monitor-app へ転送していて、`MONITOR_ROOT_PATH` も `MONITOR_FORWARDED_ALLOW_IPS` も設定していないなら、`MONITOR_ALLOWED_HOSTS=<公開する名前>` を設定する
- **PostgreSQL**: 2.3.x で CSV から入れた DB の遅れた連番は、最初の起動で自動的に直る(作業は不要。権限が無ければ警告の SQL を管理者が実行する)
- **API のクライアント**: `/api/views/batch` の DECIMAL(PostgreSQL / MySQL)は文字列になった。gzip を解凍しない独自のクライアントは `Accept-Encoding` を送らないか、`MONITOR_COMPRESS_RESPONSES=false`。DB のロックは 503 `DATABASE_BUSY`(`Retry-After` の後にやり直す)、処理されなかったエラーは JSON の 500 `INTERNAL_ERROR` になった。エクスポートの値をそのまま使うプログラムは `?raw=1` を付ける
- **Docker**: `docker build -f docker/Dockerfile .` は `docker build -t monitor-app .` に変わった。自分のプロジェクトは `monitor-app deploy docker` で作る `compose.yaml` を使う。コンテナ内で `backup` などを実行するときは `docker compose run --rm monitor-app backup`(`exec` は root で動くため、作ったファイルを root しか読めなくなる)
- **2.3.x に戻す場合**: `"datetime"` 型の列・`retention`・`max_rows` や、`monitor_app` からの `Retention`・`DashboardConfig`・`SourceDef` の import を使った config.py は 2.3.x では読み込めない。WAL のままでも、SQLite のファイルの先頭にバックアップの ID があっても 2.3.x で開ける
  - config.py で `sources` か procdiag・netdiag・network-checker のプリセットを使っている場合だけ、読み取り位置を戻す。2.4 はウォーターマークを型つきの JSON(`{"v": 12}`)で保存し、2.3.x はこれを比べられないため、そのままでは何も取り込まない
  - **`_ingest_state` の行は削除しない**。削除すると 2.3.x が最初から取り込み直し、ポーリングのたびに主キーの重複で失敗するか、行が二重に入る
  - サーバーを止め、2.4 が入っているうちに、プロジェクトのフォルダで次を実行する(PostgreSQL / MySQL は `MONITOR_DATABASE_URL` の値を渡す。pipx・`uv tool` で入れたなら `python` の代わりに `uvx --from monitor-app python`):
    `python -c "from monitor_app.db.ingest_state import unwrap_typed_cursors as u; print(u('sqlite:///instances/database.db'))"`
  - 同じことを SQL で行うなら: SQLite `UPDATE _ingest_state SET cursor = json_extract(cursor, '$.v') WHERE cursor LIKE '{"v":%'`、PostgreSQL `UPDATE _ingest_state SET cursor = (cursor::json ->> 'v') WHERE cursor LIKE '{"v":%';`、MySQL ``UPDATE _ingest_state SET `cursor` = JSON_UNQUOTE(JSON_EXTRACT(`cursor`, '$.v')) WHERE `cursor` LIKE '{"v":%';``
  - そのあと 2.3.x を入れると、止まったところから続けて取り込む。2.4 も戻した形を読めるため、あとで再び更新しても問題ない。`csv:` で始まる行(追記の監視の読み取り位置)は 2.3.x が無視する(消してもよい)
- **既知の制限**
  - `MONITOR_INGEST_MODE=append` で、起動時に全体を取り込んだ直後(`runserver --import-csv`、または `--import-csv-if-empty` の初回の起動)は、監視が始まるまでの数秒間に追記された行を取り込まない(`monitor-app serve --append` にはこの隙間がない)。2.3 から更新した最初の起動は、読み取り位置がまだ無いためファイルの末尾から読み始める
  - フォルダの監視はファイルの更新日時で変更を検知する。FAT / exFAT や SMB では、1 回の確認の間の 2 回の書き込みが 1 回に見え、最後の行が次の書き込みまで待つことがある
  - PostgreSQL / MySQL で、自分自身を参照する表の子が親より前にある CSV は、追記(`--keep`・追記の監視)では取り込めない(置き換え・`--if-empty` では並べ替える)
  - 2.3 で作った PostgreSQL / MySQL の表の整数の列は INTEGER のままで、2^31 を超える値の行は警告して読み飛ばす(`ALTER TABLE` で BIGINT に広げる)。`check --data` の `csv-db-limit` は新しく作る表の型で判定するため、この場合は警告しない
  - `uvicorn monitor_app.main:app_factory` で直接起動したときは、作業フォルダの取り違え(相対パスの DB が `MONITOR_CONFIG_PATH` のフォルダにしか無い)を確かめない(CLI のコマンドは確かめる)
  - ログインと `X-API-Key` の試行の回数は制限しない(推測されにくい長いキーを使う。`monitor-app deploy` が生成する)
  - Docker のデモ(何もマウントしない・空のフォルダ)は API キーなしで動く(コンテナのログに警告が出る)
  - macOS のネットワーク共有は自動で判別しないため、`MONITOR_SQLITE_JOURNAL_MODE=delete` を設定する
  - MariaDB は、`max_rows` や `?limit=` を使うとビューの中の `ORDER BY` を無視することがある(ビューの SQL の中に `LIMIT` を書く)
  - 表のページを表示したウィンドウを 6 つ以上並べると、ブラウザの同時接続数(HTTP/1.1)の上限でほかのページが開けないことがある(HTTP/2 のリバースプロキシを使えば解消する)
  - 32 ビットの Raspberry Pi OS 用の Docker イメージはない(pipx などで直接インストールする)

## [2.3.1] - 2026-06-17

> Dependabot が検出した依存パッケージの脆弱性（19 件）をまとめて解消するセキュリティ
> パッチリリース。アプリのコード・公開 API に挙動変更はなし。

### Security
- **starlette `0.46.2` → `1.3.1`** — 7 件の脆弱性を修正（CVE-2025-54121 / CVE-2025-62727 /
  CVE-2026-48710 / CVE-2026-48817 / CVE-2026-48818 / CVE-2026-54282 / CVE-2026-54283。
  `request.form()` の `max_fields`/`max_part_size` が `application/x-www-form-urlencoded`
  で無視される DoS など）。`CORSMiddleware` を直接 import しているため依存に明示宣言し、
  下限を `>=1.3.1,<2.0` に固定
- **python-multipart `0.0.9` → `0.0.32`**（下限 `>=0.0.31`）— 8 件の脆弱性を修正
  （CVE-2024-53981 / CVE-2026-24486 / CVE-2026-40347 / CVE-2026-42561 / CVE-2026-53537 /
  CVE-2026-53538 / CVE-2026-53539 / CVE-2026-53540。マルチパート解析時の ReDoS / リソース枯渇）
- **pg8000 `1.31.4` → `1.31.5`**（下限 `>=1.31.5`）— CVE-2025-61385 を修正
- **fastapi 下限を `>=0.115` → `>=0.133`** — fastapi `<0.133` は starlette を `<1.0` に固定して
  おり patched starlette と両立しないため引き上げ（解決後 `0.137.1`）。public API 変更なし
- **Pygments `2.19.2` → `2.20.0`**（推移的依存）— CVE-2026-4539 を修正
- **black `24.10.0` → `26.5.1`**（開発依存）— CVE-2026-32274 を修正。`.pre-commit-config.yaml`
  と `tools/precommit-bundle/` の black rev も同期。26.x の stable style に追従して再整形
- **pytest `8.4.1` → `9.1.0`**（開発依存）— CVE-2025-71176 を修正

## [2.3.0] - 2026-06-15

> 2.1.0 以降の v2.2(外部ツール連携)・v2.3(UI 刷新・ダークテーマ・ダッシュボード)
> と、コードレビュー起点の堅牢化/高速化/セキュリティ修正をまとめて公開。下の
> 「2.2.0」節の integrations 機能も本リリースに同梱(PyPI は 2.1.0 → 2.3.0)。

### Added (UI redesign — dashboard & dark theme)
- **ダッシュボードホームページ** — ホームを `chart` 付きビューのミニチャートグリッド主体に刷新。
  グラフなしビューはリンク一覧として下部に表示。グリッド列数は `DashboardConfig.columns`(1〜4)で変更可
- **`DashboardConfig`** — `MonitorConfig.dashboard` に `views`(表示するビューの明示指定)と
  `columns`(列数)を追加。`dashboard.views` に `chart` なしビューを指定すると起動時エラー
- **`ViewDef.group`** — ナビ・ビュー一覧でのセクション名フィールドを追加。サイドバーと
  ホームのビュー一覧でグループ見出しが表示される
- **ダークテーマ** — `theme.js` が `localStorage["monitor-theme"]` を読んで `<html data-theme>` を設定(FOUC なし)。
  ヘッダーのトグルボタン(🌙/☀️)でライト / ダーク切替。`monitor:themechange` イベントで Chart.js の色も即時更新
- **CSS 変数 `--chart-grid` / `--chart-tick`** — グリッド線・目盛り色をテーマ変数で制御。
  ライト既定値はそれぞれ `rgba(100,116,139,0.18)` / `#475569`
- **ビューページのテーブル折りたたみ** — `chart` 付きビュー(`/table/<view>`)でチャートを最上部に配置し、
  データテーブルを `<details>` で折りたたみ表示。`#row-count` バッジで行数を表示
- **`MonitorChart.create()` compact オプション** — `{ compact: true }` で凡例・軸タイトルを省略し
  目盛りを絞ったミニ表示(ダッシュボードカード用)

### Security
- **埋め込み JSON の `</script>` 混入による XSS を防止(#S1)** — `<script
  type="application/json">` に埋め込む dashboard/kiosk/chart の JSON を、`<` `>`
  `&` と行区切り(U+2028/U+2029)を `\uXXXX` へ置換して無害化。ビュータイトルや
  チャートラベル等の config 由来文字列に `</script>` を含めても script タグを
  閉じられない(`JSON.parse` 後の値は不変)
- **基本的なセキュリティヘッダを付与(#S2)** — 全レスポンスへ
  `X-Content-Type-Options: nosniff` / `X-Frame-Options: SAMEORIGIN` /
  `Referrer-Policy: same-origin` を付与(MIME スニッフィング XSS・クリック
  ジャッキングの基本対策)。厳格な CSP は将来対応
- **network_checker のホスト名を検証(#S3)** — SQL へ直接埋め込むホスト名を
  許可文字(英数字 `. _ - :`)で検証し、不正な値は早期に拒否。手動エスケープ依存を解消

### Fixed
- **パスパラメータの主キーを型変換(#E8)** — URL 上の主キーを主キー列の論理型へ
  coerce してから比較。PostgreSQL/MySQL での `integer = '5'(text)` 型不一致を防止
- **fetch 失敗の可視化(#E9)** — ダッシュボード/KPI/kiosk が取得失敗時に黙って
  古い値を保持していたのを、stale 表示(最終更新時刻・灰の点滅)で明示
- **入力フォームの二重送信ガード(#U3)** — 送信中は submit ボタンを無効化し、連打
  による重複登録を防止
- **ブラウザ通知をオプトインに(#U4)** — ロード時に許可ダイアログを出さず、
  「通知を有効化」ボタンのユーザー操作を起点に要求
- **KPI 評価の N+1 解消(#V4)** — 全 KPI を 1 本の読み取り専用コネクションで評価。
  1 件の失敗は rollback で分離
- **取り込みのチャンク挿入(#V5)** — 一括 INSERT を 1000 件ずつに分割し、パラメータ
  上限超過と巨大文によるメモリ圧迫を回避
- **インデックス自動生成(#V6)** — 外部キー列へ索引を自動付与し、`TableDef.indexes` で
  時刻/ウォーターマーク列に索引を宣言可能に。integrations の時系列ビューにも適用
- **SSE が瞬断で恒久的にポーリングへ降格する問題を修正(#U2)** — これまで
  `EventSource.onerror` で即 `close()` していたため、ネットワークの一瞬の切断でも
  二度と SSE に戻らずポーリング固定になっていた。ブラウザ標準の自動再接続を活かし、
  連続失敗が閾値(6 回)を超えたときだけポーリングへ降格するよう変更。接続回復時は
  自動で差分配信(SSE)に戻る

### Changed
- **ruff による import 整列を導入(#M3)** — import 順の不統一を ruff(isort 相当)で
  自動整列し、CI で強制(`code-quality-check`)。差分ノイズを根絶
- **integrations の SQLite 方言依存を明記(#M4)** — procdiag/netdiag/network_checker の
  生成するビュー/KPI が SQLite 方言(`datetime('now',...)` 等)に依存することを docstring
  と integrations ドキュメントに明記(アプリ DB が SQLite 前提)

### Removed
- **LINE Notify 通知チャネルを削除** — LINE Notify は 2025-03-31 にサービス終了済みで、
  常に失敗する死んだ統合だったため除去。`notify=["line"]` チャネル・`MONITOR_LINE_TOKEN`
  設定・`LineNotifier` を削除した。現場スマホへのプッシュが必要な場合は `webhook`
  (汎用 JSON POST)経由を推奨

## [2.2.0] - 2026-06-13 — 2.3.0 に同梱(PyPI 未公開)

### Added (external tool integrations)
- **`SourceDef` / declarative sources** — `MonitorConfig.sources` で SQLite / JSONL / JSON
  ファイルを宣言的に取り込み先テーブルへ紐付けられるようになりました
  (`kind`, `path`, `table`, `watermark_column`, `mapping`, `mode` など)
- **3 コネクタ** — `SqliteSourceConnector`(watermark / mtime 差分)・
  `JsonlSourceConnector`(バイトオフセット差分)・`JsonSourceConnector`(mtime 全置換)
  を `monitor_app/services/connectors.py` に実装
- **`sync-sources` CLI** — `monitor-app sync-sources` で全ソースを一括同期(サーバー不要)
- **3 プリセット** — ツール無改造・読み取り専用で接続:
  - `monitor_app.integrations.procdiag` — CPU / メモリ / PSI / 診断所見
  - `monitor_app.integrations.netdiag` — netdiag watch アラートログ(JSONL)
  - `monitor_app.integrations.network_checker` — ping 結果 SQLite

## [2.1.0] - 2026-06-10

Complete rewrite on **FastAPI + Pydantic** (from Flask), plus manufacturing-focused features.

### Added (shop-floor features)
- **Auto data ingest** — `csv/` folder watching (`MONITOR_INGEST_WATCH`) and bulk
  `POST /api/ingest/{table}` for sensors / PLC / MES
- **Threshold alerts** — declarative `AlertRule`s, edge-triggered notifications
  (console / Webhook / LINE / e-mail), on-screen banner with sound, `/api/alerts`
- **Andon wallboard** — `/kiosk` full-screen rotating views with green/amber/red status light
- **Trend / SPC charts** — `ChartDef` with UCL/LCL/target lines (bundled Chart.js, offline-capable)
- **KPI cards** — scalar-SQL `KpiCard`s with target comparison, `/api/kpis`
- **Operator entry forms** — touch-friendly forms generated from table schema at `/form/{table}`
- **Audit log** — before/after change history with actor (`MONITOR_AUDIT_ENABLED`), `/api/audit`
- **Export** — `/api/views/{view}/export?format=csv|xlsx` (`pip install monitor-app[xlsx]`)

### Changed (v2 architecture)
- Flask → **FastAPI**; flasgger's hand-written Swagger YAML → automatic OpenAPI (`/docs`, `/redoc`)
- dict-based config → **type-safe `MonitorConfig`** (Pydantic). Typos are caught at startup;
  `monitor-app check` validates without starting the server. The legacy v1 dict format
  (`ALLOWED_TABLES` etc.) still loads via a compatibility shim
- Raw f-string SQL → **SQLAlchemy 2.0 Core expression builder** (structural injection safety)
- pandas CSV import → stdlib `csv` with config-driven schema (no more schema clobbering;
  pandas dependency removed)
- `startproject` no longer copies application source — only `config.py`, `.env.example`
  and sample CSVs; the app updates via `pip install -U monitor-app`
- DB settings (`DB_TYPE`, credentials) → `MONITOR_DATABASE_URL` environment variable / `.env`
- API paths: `/api/<table>` → `/api/tables/{table}`, `/api/view/<v>` → `/api/views/{view}`,
  schema list → `/api/schema`
- Polling-only refresh → **Server-Sent Events** with polling fallback
- Vue (CDN) frontend → vanilla JS + bundled assets; modern minimal UI
- click CLI → **Typer** (`runserver`, `import-csv`, `check`, `startproject`)
- Optional API-key auth (`MONITOR_API_KEY`, `X-API-Key` header) for write endpoints;
  CORS now defaults to same-origin (configurable)

### Removed
- Flask, flask-sqlalchemy, flask-cors, flasgger, pandas, toml dependencies
- jQuery / DataTables assets

## [0.1.9] - earlier
- Final Flask-based release.
