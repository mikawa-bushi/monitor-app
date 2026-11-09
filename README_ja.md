# Monitor App 🚀

[![PyPI version](https://img.shields.io/pypi/v/monitor-app.svg)](https://pypi.org/project/monitor-app/)
[![Python versions](https://img.shields.io/pypi/pyversions/monitor-app.svg)](https://pypi.org/project/monitor-app/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/mikawa-bushi/monitor-app/blob/main/LICENSE)

**CSV を置いて設定ファイルを 1 つ書くだけで、リアルタイム更新の Web UI と REST API が手に入る。**

Monitor App は製造現場・実験室・小規模チーム向けのデータ監視ツールです。CSV のフォルダを指定すれば 1 分でダッシュボードが開きます。本格的に使うときは、テーブル・ビュー・アラート・KPI カードを 1 つの `config.py` に書けば、リアルタイムのダッシュボード・Andon 大型表示・作業者の入力フォーム・Swagger ドキュメント付きの REST API が動きます。SQLite で 1 台の PC や Raspberry Pi でも動き、フロントエンドのビルドは不要、インターネットにつながらない工場のネットワークでも使えます。

English documentation: [README.md](https://github.com/mikawa-bushi/monitor-app/blob/main/README.md)

---

## 👀 まず見てみる: 物流倉庫の在庫画面のデモ

準備なしで、架空の物流センターの在庫画面を開けます。

```sh
uvx monitor-app demo --open                                      # インストール済みなら monitor-app demo --open
docker run --rm -p 9990:9990 ghcr.io/mikawa-bushi/monitor-app    # Docker なら(何もマウントしない)
```

http://127.0.0.1:9990 が開きます(Docker は http://localhost:9990)。見どころ:

- **在庫一覧**: 棚番・ロット・賞味期限・残日数・ケース / バラ。在庫は入出庫の台帳を SQL で合計して求めます。
- **ひとりでに動く**: 4 秒ごとに入荷・出荷・移動が起き、入出庫履歴・KPI カード・グラフが自動で更新されます。
- **アラート**: 起動した直後は「安全在庫を下回っている品目」と「賞味期限まで 2 日以内のロット」のバナーが出ます。約 1 分半後に入荷が届くと、安全在庫のバナーが消えます。
- **ほかの画面**: `/kiosk`(大型表示)、`/form/movements`(入出庫の入力フォーム)。この PC で動かしているときは `/settings`(画面での設定の変更)も使えます。

データは一時的な DB に入り、終了すると消えます。起動したときに、この画面の `config.py` の場所を表示します。Docker では時刻が UTC になるため、日本時間で見るには `-e TZ=Asia/Tokyo` を付けます。

同じ設定と架空のデータで自分のプロジェクトを作るには:

```sh
monitor-app startproject my-warehouse --template warehouse
cd my-warehouse
monitor-app runserver --import-csv-if-empty
```

プロジェクトでは入出庫は自動で起きません。自分のデータへの替え方は、プロジェクトの `README.md` にあります。

## ✨ 2.5 の新機能

- **準備なしで動くデモ。** `monitor-app demo` で、架空の物流センターの在庫画面を開きます。4 秒ごとに入荷・出荷・移動が起き、KPI カード・グラフ・アラートがひとりでに動きます。データは一時的な SQLite に入り、終了すると消えます。Docker を何もマウントせずに起動したときも、サンプルのプロジェクトの代わりにこのデモが起動します(空のフォルダをマウントすれば、これまでどおりサンプルのプロジェクトを書き込みます)。
- **デモからプロジェクトを始める。** `monitor-app startproject 名前 --template warehouse` で、デモと同じ `config.py`・マスタ・7 日分の架空のデータでプロジェクトを作ります。
- **設定ページ。** `/settings` で、表示・ビュー(SQL・列の見出し・セルの色分け・グラフ)・KPI・アラート・大型表示・ダッシュボードを、`config.py` を書き換えずに編集できます。保存すると `monitor-app check` と同じ点検をしてから、サーバーを止めずに反映します。保存するのは `config.py` との差分だけで、DB に置きます。
- **CSV のアップロード。** `/upload` で、ブラウザから CSV をテーブルに取り込めます。`monitor-app import-csv` と同じ処理で、追記と置き換えを選べます。
- **`MONITOR_ADMIN_KEY`。** 設定ページと CSV のアップロードだけに使う鍵です。設定すると、`MONITOR_API_KEY` でログインした作業者は設定を変えられません。未設定なら `MONITOR_API_KEY` で使え、どちらの鍵も無ければこの PC からだけ使えます。
- **入力フォームの名前。** `FormDef(title="...")` で付けた名前が、フォームの見出し・ブラウザのタブの題名・トップページのリンクに出ます。
- **セキュリティの強化。** 管理の機能は、ほかのサイトのページから送られた要求を断ります(CSRF)。画面から保存する SQL は `;` を含まない 1 つの文に限り、KPI の表示形式は値の書式だけを許します。アップロードしたファイルの名前は、どの OS でもフォルダとドライブの部分を取り除くため、一時フォルダの外を指せません。

すべての変更: [CHANGELOG.md](https://github.com/mikawa-bushi/monitor-app/blob/main/CHANGELOG.md)。2.4 から更新するときは、CHANGELOG の「Upgrade notes」のほかに変えることはありません。2.3 から更新するときは、下の「**2.3.x からの更新**」を読んでください。

## 特徴

| 機能 | 内容 | 使い方 |
|---|---|---|
| 設定ファイルなしの CSV ダッシュボード | 型・主キー・グラフ・KPI カードをデータから推論 | `monitor-app serve フォルダ` |
| 宣言的な設定ファイル 1 つ | テーブル・ビュー(任意の SQL の `SELECT`)・セルの色分け・アラート・KPI・フォーム。起動時に検証し、綴りの誤りは行番号つきで知らせる | `config.py` + `monitor-app check` |
| リアルタイム更新 | Server-Sent Events で変更をブラウザへ送る(ポーリングにも自動で切り替え) | 既定(`refresh_mode="sse"` か `"polling"`) |
| ダッシュボード | ミニグラフ(1〜4 列)・KPI カード・グループ分けしたビュー一覧のホーム画面。ライト / ダークテーマ | `dashboard=DashboardConfig(...)` |
| Andon 大型表示 | 全画面で複数のビューを自動で切り替え、緑 / 黄 / 赤の信号灯を表示 | `/kiosk`、`kiosk=KioskConfig(...)` |
| トレンド / SPC グラフ | UCL / LCL / 目標線つきの折れ線・棒グラフ | `ViewDef(chart=ChartDef(...))` |
| 閾値アラート | 画面のバナー・ブラウザ通知・Webhook(Slack・Teams)・メール。状態が変わったときに 1 回通知し、失敗は再試行 | `alerts=[AlertRule(...)]` |
| 作業者の入力フォーム | テーブル定義からタッチ操作向けのフォームを自動生成 | `TableDef(form=FormDef(...))`、`/form/{table}` |
| 画面での設定・CSV の取り込み | 表示・ビュー(SQL)・色分け・グラフ・KPI・アラート・大型表示を画面で編集し、サーバーを止めずに反映(開いている画面も読み直す)。CSV をブラウザから取り込む | `/settings`、`/upload` |
| データの取り込み | CSV(UTF-8・Excel の cp932)、フォルダ監視(置き換え / 追記)、PLC・MES・センサからの一括 `POST`、読み取り専用の `sources`(SQLite / JSONL / JSON) | `import-csv`、`MONITOR_INGEST_WATCH`、`/api/ingest/{table}` |
| REST API | テーブルごとの CRUD・ビューのデータ・CSV / Excel エクスポート・OpenAPI ドキュメント | `/api/...`、`/docs`、`/redoc` |
| Python API | 自分のプログラムからアプリを組み立てる・`config.py` を読み込んで検証する | `create_app`、`load_config`(「**Python API**」を参照) |
| データの保全 | SQLite の WAL、稼働中のバックアップと復元、保持期間、表定義の点検と `migrate`、監査ログ | `monitor-app backup`、`MONITOR_AUDIT_ENABLED` |
| データベース | SQLite(既定。インストール不要)・PostgreSQL・MySQL | `MONITOR_DATABASE_URL` |
| 運用・公開 | Docker イメージ、Compose / systemd / Windows の起動ファイル、リバースプロキシ配下のサブパス、死活監視 | `monitor-app deploy`、`/api/health` |
| オフライン | Node.js もビルドも不要。Chart.js を同梱し、見た目は素の CSS で書いているため、ダッシュボードはインターネットなしで動きます。API ドキュメントの `/docs` と `/redoc` だけは Swagger UI / ReDoc(版を固定)を CDN から読み込みます。`MONITOR_API_DOCS=false` で出さないようにできます | — |

---

## 🚀 インストール

Monitor App には **Python 3.11 以降** が必要です。次のどれか 1 つの方法で入れます。

```sh
pipx install monitor-app         # pipx で
uv tool install monitor-app      # または uv で(必要なら Python も自動で入れる)
monitor-app --version
```

どちらも `monitor-app` コマンドを専用の環境に入れるため、ほかの Python ソフトと干渉しません。更新は `pipx upgrade monitor-app` または `uv tool upgrade monitor-app`。

**インストールせずに試す**([uv](https://docs.astral.sh/uv/) がキャッシュに入れて実行します):

```sh
uvx monitor-app serve ./my-csv-folder
```

**仮想環境と pip** でも入れられます:

```sh
python3 -m venv .venv
. .venv/bin/activate             # Windows: .venv\Scripts\activate
pip install monitor-app
```

`python -m monitor_app <コマンド>` は `monitor-app <コマンド>` と同じです(コマンドに PATH が通っていないときに便利)。

### Windows

いちばん簡単なのは uv です。管理者権限が要らず、合う Python も自動で入れます。PowerShell で:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
# PowerShell を開き直してから:
uv tool install monitor-app
monitor-app --version
```

python.org の Python を使っている場合は `py -m pip install monitor-app` で入れ、`py -m monitor_app runserver` で起動します(Python の `Scripts` フォルダに PATH が通っていなくても動きます)。

### Debian 12 以降・Ubuntu 23.04 以降・Raspberry Pi OS

仮想環境の外で `pip install monitor-app` を実行すると `error: externally-managed-environment` で止まります(PEP 668)。pipx か uv を使います:

```sh
sudo apt install pipx
pipx ensurepath                  # ターミナルを開き直す
pipx install monitor-app
```

または `curl -LsSf https://astral.sh/uv/install.sh | sh` で uv を入れて `uv tool install monitor-app`。

**64 ビット版の Raspberry Pi OS** はふつうの Linux PC と同じです(Docker イメージも対応)。**32 ビット版の Raspberry Pi OS** でも、Raspberry Pi 2 以降(Zero 2 W を含む)なら依存パッケージのビルド済みファイルがすべて PyPI にあり、`pipx install monitor-app` で入ります。ARMv6 の機種(Pi 1、初代の Pi Zero と Zero W)では、pydantic-core と MarkupSafe のビルド済みファイルが PyPI にありません。Raspberry Pi OS の pip は最初から [piwheels](https://www.piwheels.org/) も使う設定になっていて、piwheels はこの 2 つを ARMv6 向けにビルドしています。ただし piwheels に pydantic-core の最新版がまだ無いことがあり、手元でビルドするには Rust のコンパイラが要ります。uv は pip の設定を読まないため、uv を使うなら `UV_EXTRA_INDEX_URL=https://www.piwheels.org/simple` を設定します。32 ビットの ARM では `[postgres]` は使えないため、同梱の pg8000 ドライバ(`postgresql+pg8000://...`)を使います。32 ビット用の Docker イメージはありません。

### 追加機能(extras)

| extra | 追加されるもの | 用途 |
|---|---|---|
| `xlsx` | openpyxl | Excel へのエクスポート: `/api/views/{view}/export?format=xlsx` |
| `postgres` | psycopg(バイナリ版) | 高速な PostgreSQL ドライバ `postgresql+psycopg://`(pg8000 と PyMySQL は常に入っている) |

```sh
pipx install "monitor-app[xlsx,postgres]"       # pipx: 入れたあとに足すときは --force を付ける
uv tool install "monitor-app[xlsx,postgres]"    # uv: もう一度実行すると extras が加わる
```

### インターネットにつながらない PC へのインストール

インターネットにつながる PC でパッケージをまとめてダウンロードし、コピーして入れます。入れる先の PC の Python のバージョンとプラットフォームを指定します(ここでは Python 3.12・64 ビットの Linux):

```sh
# インターネットにつながる PC で(uv と pip が必要):
echo "monitor-app[xlsx]" > requirements.in
uv pip compile requirements.in -o requirements.txt --only-binary :all: \
    --python-version 3.12 --python-platform x86_64-manylinux_2_28
python3 -m pip download --no-deps --only-binary=:all: -d wheelhouse -r requirements.txt \
    --python-version 3.12 --platform manylinux_2_28_x86_64 \
    --platform manylinux_2_17_x86_64 --platform manylinux2014_x86_64
# requirements.txt と wheelhouse フォルダ(約 20 MB)を入れる先の PC にコピーし、そこで:
python3 -m venv ~/monitor-app
~/monitor-app/bin/pip install --no-index --find-links wheelhouse "monitor-app[xlsx]"
~/monitor-app/bin/monitor-app --version
```

ほかの環境の場合: 64 ビットの Raspberry Pi は `--python-platform aarch64-manylinux_2_28` と、3 つの `--platform` の `x86_64` を `aarch64` にしたもの。64 ビットの Windows は `--python-platform x86_64-pc-windows-msvc` と `--platform win_amd64` で、入れる先では `py -m venv monitor-app` と `monitor-app\Scripts\pip install ...`。`--only-binary :all:` を付けると、uv は入れる先向けのビルド済みファイルがある版だけを選びます。それでも `pip download` があるパッケージで「No matching distribution」と言うときは、`requirements.in` にその版より前を指定する行を書き足して(例: `sqlalchemy==2.1.6` で失敗したら `sqlalchemy<2.1.6`)、2 つのコマンドをやり直します。

---

## 🔧 クイックスタート A: CSV のフォルダだけで(設定なし・1 分)

```sh
uvx monitor-app serve ./my-csv-folder     # ファイル 1 つでも可: serve ./data/temperature.csv
```

http://127.0.0.1:9990 を開きます(終了は Ctrl+C。インストール済みなら `uvx` は不要)。CSV ファイルごとにテーブル・ビュー・行数の KPI カードを作り、数値の列があれば折れ線グラフも作ります。推論する内容:

- **ファイルごとの文字コード**: UTF-8(BOM の有無を問わない)か、Excel の「CSV(コンマ区切り)」形式(cp932)。`温度(℃)` のような日本語の見出しも使えます。
- **データから列の型**: 整数・小数・`TRUE/FALSE/yes/no`(真偽値)・日付と日時(`2026-10-21 08:00`、`2026/10/21 8:00`)。`0012` のように先頭が 0 のコードは文字列のままにします。
- **主キー**: 重複のない整数の `id` 列。なければ `_row_id` 列を足します。
- **グラフ**: x は最初の日付・日時の列(なければ行の番号)、y は数値の列を 4 つまで。大きなファイルのビューは新しい 5,000 行を表示し、ホーム画面には最初の 6 つのグラフを並べます。

データは一時的な DB に入れ、終了すると消えます(`--db data.db` で残せます)。CSV を変更すると自動で取り込み直します。`--append` はファイルに追記された行だけを取り込み(ログ・計測値向け)、`--no-watch` は 1 回だけ取り込みます。この PC からだけ接続できます。`--host 0.0.0.0` でほかの PC からも開けるようにするときは `MONITOR_API_KEY` も設定してください(下の「**実行時設定**」)。

**推論した設定を残して**、ふつうのプロジェクトとして続けるには:

```sh
monitor-app startproject myproject --from-csv ./my-csv-folder   # config.py と CSV のコピー
cd myproject
monitor-app runserver --import-csv-if-empty
```

(`serve ./my-csv-folder --write-config config.py` は CSV をコピーせずに `config.py` だけを書き出し、そのままいつもどおりダッシュボードを開きます。あとでその `config.py` で動かすには、表示される案内のとおり `.env` に `MONITOR_CSV_DIR`(CSV のフォルダ)を書き、`monitor-app runserver --config config.py --import-csv-if-empty` で起動します。)

## 🔧 クイックスタート B: プロジェクトを作る

```sh
monitor-app startproject myproject        # config.py・サンプル CSV・.env.example・README
cd myproject
monitor-app runserver --import-csv-if-empty
```

http://127.0.0.1:9990 を開きます(API ドキュメントは http://127.0.0.1:9990/docs)。`config.py` を編集したら、サーバーを再起動します。CSV は行がまだ無いテーブル(初回の起動)にだけ取り込むため、`csv/` のファイルを変えたら `monitor-app import-csv` で取り込みます(下を参照)。

**取り込みのオプションの選び方。** テーブルには CSV の行のほかに、入力フォーム・画面・API から入力した行も入ります。それらの行をどうするかで選びます:

| コマンド | 画面・フォーム・API で入力した行 | 使う場面 |
|---|---|---|
| `runserver --import-csv-if-empty` | **残る**: 行が 1 件も無いテーブル(初回の起動)にだけ CSV を取り込む | 普段の運用(おすすめ) |
| `runserver --import-csv` | **起動のたびに置き換わる**: 各テーブルが CSV の内容になる | CSV が正本(マスタ)のとき |
| `import-csv` | いま 1 回置き換える | CSV を意図して更新したとき |
| `import-csv --keep` | 残る。主キーが既にある CSV の行は読み飛ばす | CSV に行を書き足したとき |
| `import-csv --if-empty` | 残る(`--import-csv-if-empty` と同じ) | スクリプトから |

安全のための決まりが 2 つあります。取り込みは 1 つのトランザクションで行うため、失敗しても何も変わりません。また、置き換えの取り込みは、`config.py` に書いた列(主キー以外)が CSV の見出しに無いテーブルを置き換えず、既存の行を残します。ほかのテーブルは取り込み、`import-csv` は終了コード 1 で終わります。`--force` を付けると置き換え、その列は空になります。なお `--keep` は、画面で削除した行も CSV に残っていれば入れ直します。

## 🧰 コマンド一覧

| コマンド | 内容 | 主なオプション |
|---|---|---|
| `monitor-app demo` | 物流倉庫の在庫画面のデモを開く(準備不要・データは架空で終了すると消える) | `--host`、`--port`、`--open` |
| `monitor-app serve [PATH...]` | config.py なしで CSV からダッシュボードを開く | `--host`、`--port`、`--db FILE`、`--append`、`--no-watch`、`--write-config FILE`、`--open` |
| `monitor-app startproject NAME` | プロジェクトのフォルダを作る | `--template warehouse`(デモと同じ設定とデータ)、`--from-csv DIR`、`--deploy docker,systemd,windows`(または `all`) |
| `monitor-app runserver` | Web サーバーを起動する | `--import-csv-if-empty`、`--import-csv`、`--force`、`--host`、`--port`、`--reload`、`--config` |
| `monitor-app check` | 起動せずに `config.py`・SQL・実行時設定・DB の表定義を点検する | `--data`(CSV の中身も)、`--strict`(警告でも失敗。CI 向け)、`--fix-schema`、`--config` |
| `monitor-app import-csv` | `csv/` を DB へ取り込む(置き換え) | `--keep`、`--if-empty`、`--force`、`--config` |
| `monitor-app sync-sources` | `sources` を 1 回取り込む(cron 向け) | `--config` |
| `monitor-app migrate` | `config.py` に足した列を既存の表に追加する(2.3 で遅れたままの PostgreSQL の id の連番も直す) | `--dry-run`、`--config` |
| `monitor-app backup` | SQLite の一貫したバックアップ(稼働中でもよい) | `--dest DIR`、`--keep N`、`--config` |
| `monitor-app restore FILE` | バックアップから復元する(先にサーバーを止める) | `--yes` / `-y`、`--config` |
| `monitor-app deploy [TARGET...]` | 起動ファイルを書き出す: `docker`・`systemd`・`windows`・`all`。API キーを生成して `.env` に書く | `--project DIR`、`--port N`、`--config FILE`、`--force` |

- `monitor-app --version`(`-V`)。`monitor-app --debug <コマンド>`(または `MONITOR_DEBUG=1`)でトレースバックを表示します。ふだんはエラーを最大 3 行で stderr に出します。
- `--config` には `config.py` のパスかプロジェクトのフォルダを指定します(`~` も可)。ほかのフォルダから実行すると、そのプロジェクトの `.env` も読みます。設定の相対パス(`instances/`・`csv/`・`backups/`)は、コマンドを実行したフォルダが基準のままです。DB(取り込むときは CSV のフォルダも)がプロジェクトのフォルダにしか無いときは、空の DB を作らずに、そのフォルダへ `cd` するよう案内して止まります。
- すべてのオプションは `monitor-app <コマンド> --help`。終了コードは 0 が成功、1 がエラー、2 が使い方の誤り、143 は `import-csv` などの 1 回きりのコマンドが SIGTERM(`docker stop`・`systemctl stop`)で止まったときです。

## 📝 設定(`config.py`)

`startproject` がコメントつきの `config.py` を書き出します。生産ラインの実用的な例:

```python
from monitor_app import (
    AlertRule, CellStyle, ChartDef, DashboardConfig, FormDef, KioskConfig,
    KpiCard, MonitorConfig, Retention, TableDef, Threshold, ViewDef,
)

config = MonitorConfig(
    app_title="1 号ライン監視",
    refresh_interval_ms=2000,
    tables={
        # CSV ファイル 1 つにテーブル 1 つ: csv/readings.csv → テーブル "readings"
        "readings": TableDef(
            # 列の型は必ず書く: "int" "float" "str" "bool" "date" "datetime"
            columns={"id": "int", "ts": "datetime", "line": "str",
                     "temp": "float", "ok": "bool"},
            primary_key="id",
            indexes=[["ts"]],                                # ts での並べ替え・絞り込みを速くする
            retention=Retention(keep_days=90, column="ts"),  # 90 日より古い行を削除
            form=FormDef(title="計測値の入力",
                         labels={"temp": "温度 (℃)"},
                         choices={"line": ["L1", "L2"]}),     # 入力フォーム: /form/readings
        ),
    },
    views={
        # 任意の SELECT が書ける(JOIN・GROUP BY など)。新しい行から表示する
        "temp_trend": ViewDef(
            title="温度(直近 500 件)",
            query="SELECT ts, line, temp FROM readings ORDER BY ts DESC LIMIT 500",
            chart=ChartDef(type="line", x="ts", y="temp", y_label="℃", ucl=80, lcl=20),
            styles={"temp": CellStyle(
                greater_than=Threshold(value=80, **{"class": "bg-danger text-white"}),
            )},
        ),
        "all_readings": ViewDef(
            title="全計測値",
            query="SELECT * FROM readings ORDER BY ts DESC",
            max_rows=5000,  # ブラウザへ送るのは最大 5000 行
        ),
    },
    alerts=[
        AlertRule(view="temp_trend", column="temp", op=">", value=80,
                  level="critical", message="温度が 80 ℃ を超えました",
                  notify=["webhook"]),  # .env の MONITOR_WEBHOOK_URL へ通知
    ],
    kpis={
        "avg_temp": KpiCard(title="平均温度", unit="℃", format="{:.1f}",
                            query="SELECT AVG(temp) FROM readings",
                            target=60, higher_is_better=False, link_view="temp_trend"),
    },
    kiosk=KioskConfig(views=["temp_trend", "all_readings"], rotate_seconds=15),
    dashboard=DashboardConfig(views=["temp_trend"], columns=2),
)
```

- **列の型**: 上のように必ず書いてください。`columns=["id", "temp"]` のような列名だけの書き方も使えますが、型を列名から推測します(主キーと `*_id` は整数、`price`・`amount` を含む名前は小数、それ以外は文字列)。文字列として保存された `temp` は並べ替えや比較が正しくなりません(`"9" > "10"`)。`monitor-app check --data` がこのような列を警告します。
- **`datetime`** の列は ISO 8601(`2026-10-21T08:00:00`、`Z` や `+09:00` も可)と Excel の `2026/10/21 8:00` を受け付けます。タイムゾーンつきの時刻は PC のローカル時刻に変換して保存します。`date` の列は `2026-10-21` と `2026/10/21` を受け付けます。SQLite では日時を `2026-10-21 08:00:00` の形(1 秒未満があるときだけ `.ffffff` を付ける)で保存するため、`ORDER BY ts` や `WHERE ts >= '2026-10-21 08:00:00'` のような SQL が文字列のまま正しく動きます。2.4 で作る MySQL の表は 1 秒未満も保ちます(`DATETIME(6)`)。
- **`float`** の値 `NaN`・`inf`・`Infinity` は空(NULL)として保存するため、どの DB でも `AVG` や `MAX` が正しく計算されます。
- **`max_rows`** はビューが返す行数の上限です。指定のないビューが 50,000 行を超えるとログに警告を出します。
- **`Retention`**: `keep_days=`(`date` / `datetime` の列か、ISO 形式の文字列の列が必要)と `keep_rows=`(新しい N 行を残す)。10 分ごとに実行し、最初の実行は起動の 10 分後です(`MONITOR_RETENTION_INTERVAL_MINUTES`)。文字列の列では行ごとにその行の ISO の形で比べ、空の値や ISO 形式でない値は削除しません。
- **アラート** はビューの全データで、画面を誰も開いていなくてもバックグラウンドで評価します。通知先は `console`・`webhook`・`email`。
- あとからテーブルに足した列は、`monitor-app migrate` で既存の DB に追加します(表は初回の起動で作り、その後は変えません)。

**サーバーを起動する前に点検します:**

```sh
monitor-app check                 # config.py・全ビューと KPI の SQL・実行時設定・DB の表
monitor-app check --data          # CSV ファイル(見出し・型・文字コード)も
monitor-app check --strict        # 警告も失敗(終了コード 1)にする。CI 向け
```

綴りを誤ったキーは行番号つきで知らせ、設定はそのまま読み込みます:

```text
警告(1 件):
  - config.py 15 行目: TableDef の未知のキー 'primay_key' は無視されます('primary_key' の誤りではありませんか?)
```

`check` はほかに、`.env` の `MONITOR_*` の綴りの誤り(`MONITOR_API_KEY` の誤りは認証が無効のままになるためエラー)、説明用の値(`change-me`)・16 文字未満・ASCII 以外の API キー、ビューに無いアラート・グラフの列、設定されていない通知先も知らせます。`check --data` は `import-csv` と同じ方法で CSV を読むため、見出しの対応・変換できない値・主キーの重複を取り込みと同じように報告し、`MONITOR_DATABASE_URL` が PostgreSQL / MySQL なら、その DB が受け付けない値も知らせます。

旧形式(v1)の dict 形式の `config.py`(`ALLOWED_TABLES` など)もそのまま読めます。下の「**v0.x からの移行**」を参照してください。

## 🖱 画面で設定を変える・CSV を取り込む

サイドバーの「⚙️ 設定」(`/settings`)で、`config.py` を書き換えずに次の項目を変えられます。保存するとサーバーを止めずにすぐ反映され、開いている画面(大型表示を含む)も自動で読み直します。

| タブ | 変えられる項目 |
|---|---|
| 全般 | アプリの名前・画面上部の見出し・画面下部の文言・更新の間隔と方式 |
| ビュー | タイトル・説明・グループ・最大の行数・SQL・列の見出し・セルの色分け・グラフ(管理限界線・目標線)。ビューの追加と削除 |
| KPI | タイトル・SQL・単位・目標値・表示形式・クリックで開くビュー。KPI の追加と削除 |
| アラート | ビュー・列・条件・値・レベル・メッセージ・通知先。アラートの追加と削除 |
| 大型表示・ダッシュボード | 大型表示の切り替えの間隔・配色・表示するビュー、トップページのグラフの数と並べるグラフ |

- テーブルの定義・外部ソース(`sources`)・favicon は画面では変えられません(表の作り替えや、サーバーのファイルの読み取りになるため)。`config.py` で変えます。
- 画面で変えた値は、`config.py` との差分だけを DB(内部テーブル `_settings`)に保存し、再起動しても残ります。画面で変えた項目は `config.py` の同じ項目より優先し、変えていない項目は今までどおり `config.py` の値を使います(アラートの一覧のようなリストは丸ごと画面の値になります)。「config.py の設定に戻す」で差分を消せます。SQLite のバックアップにも含まれます。
- 保存の前に `monitor-app check` と同じ点検(SQL の空実行・アラートとグラフの列・通知先)を行い、エラーがあれば保存しません。「点検」ボタンで保存せずに確かめられます。画面から入れる SQL は `SELECT` 文 1 つだけで、`;` は書けません(文字列・コメントの中も。末尾の `;` は書けます)。読み取り専用で実行します。`;` が要る SQL は `config.py` に書いてください。
- ほかの人が先に保存していたら上書きせず、読み込み直すよう知らせます。`MONITOR_AUDIT_ENABLED=true` なら、変更を監査ログ(`/api/audit?table=_settings`)に残します。
- `config.py` からビューを消したなどで、保存してある差分を重ねられなくなったときは、`config.py` の設定だけで起動し、設定ページで知らせます。

「📤 CSV の取り込み」(`/upload`)では、CSV ファイルを選んでテーブルに取り込めます。`monitor-app import-csv` と同じ処理なので、文字コード(UTF-8・Excel の Shift_JIS)の自動判別、見出しの表記ゆれの吸収、1 回の処理での取り込み(途中で失敗したら何も変わらない)、見出しに列が丸ごと無い CSV での置き換えの拒否も同じです。「追記」(主キーが同じ行は読み飛ばす)と「置き換え」(今の行をすべて CSV の行にする)を選べます。送ったファイルはサーバーに残しません。

**だれが使えるか。** 設定と CSV の取り込みは「管理の機能」です。

| 設定 | 管理の機能を使える人 |
|---|---|
| `MONITOR_ADMIN_KEY` あり | 管理者の鍵でログインした人(または `X-API-Key` に管理者の鍵)。`MONITOR_API_KEY` を渡した作業者・閲覧者は使えない |
| `MONITOR_API_KEY` だけ | API キーでログインした人(書き込みと同じ) |
| どちらも無し | この PC から開いた人だけ(`MONITOR_HOST=127.0.0.1` で、リバースプロキシの設定も無いとき)。LAN やプロキシの先に公開しているときは使えない |

ほかのサイトのページから送られた要求(CSRF)は断ります。作業者の入力フォームに API キーでログインしてもらう現場では、`MONITOR_ADMIN_KEY` を別に設定すると、作業者は設定を変えられません。

## ⚙️ 実行時設定(`MONITOR_*` / `.env`)

PC ごとに変わる設定(DB の接続先・ポート・パスワード)は `config.py` に書きません。プロジェクトのフォルダの `.env` に書くか(`.env.example` をコピー。全設定の説明つき)、環境変数で設定します。環境変数は `.env` より優先します。

| 変数 | 既定値 | 説明 |
|---|---|---|
| **データベース** | | |
| `MONITOR_DATABASE_URL` | `sqlite:///instances/database.db` | DB の接続 URL(SQLAlchemy 形式)。PostgreSQL は `postgresql+psycopg://…`(`[postgres]` が必要)か `postgresql+pg8000://…`、MySQL は `mysql+pymysql://…`。パスワードの `@ : /` は URL エンコードする(`@` → `%40`) |
| `MONITOR_SQLITE_JOURNAL_MODE` | `wal` | SQLite のジャーナルモード。`wal`(読み書きが互いを待たない)/ `delete` / `truncate` / `persist` / `memory` / `off`。ネットワーク共有上の DB は自動で `delete` |
| `MONITOR_SQLITE_SYNCHRONOUS` | `normal` | `off` / `normal` / `full` / `extra`。`normal` は WAL のときだけ使う(停電で直近のコミットが失われ得るが DB は壊れない)。WAL 以外は SQLite 既定の `full` |
| `MONITOR_SQLITE_BUSY_TIMEOUT_MS` | `5000` | SQLite のロック待ちの上限(ミリ秒) |
| `MONITOR_DB_WAIT_TIMEOUT` | `15` | PostgreSQL / MySQL に接続できるまで起動時に待つ最大秒数(`0` で待たない)。パスワード違いなどはすぐに失敗する。1 回の接続は、URL で指定しない限り 10 秒で諦める |
| **サーバー・Web 配信** | | |
| `MONITOR_HOST` | `127.0.0.1` | 待ち受けるアドレス。`127.0.0.1` はこの PC からだけ、`0.0.0.0` はほかの PC からも(その場合は `MONITOR_API_KEY` を設定) |
| `MONITOR_PORT` | `9990` | 待ち受けるポート(0〜65535) |
| `MONITOR_ROOT_PATH` | `""` | リバースプロキシ配下のサブパス。例: `/monitor`(`monitor` や `/monitor/` も可) |
| `MONITOR_FORWARDED_ALLOW_IPS` | (未設定) | `X-Forwarded-*` ヘッダを信頼するプロキシのアドレス(カンマ区切り、`*` で全て)。未設定なら uvicorn の既定(`FORWARDED_ALLOW_IPS`、なければ `127.0.0.1`) |
| `MONITOR_COMPRESS_RESPONSES` | `true` | 1 KiB 以上のレスポンスを gzip 圧縮する(リアルタイム更新は圧縮しない) |
| `MONITOR_ACCESS_LOG` | `true` | HTTP リクエストごとに 1 行のログを出す |
| `MONITOR_API_DOCS` | `true` | API ドキュメント(`/docs`・`/redoc`・`/openapi.json`)を出す。`false` で 3 つとも出さない |
| **認証・セキュリティ** | | |
| `MONITOR_API_KEY` | (未設定) | 設定すると認証が有効になる。書き込み(API・フォーム・取り込み)にキーが必要(`X-API-Key` ヘッダ、またはブラウザでログイン。ログインは 12 時間有効)。16 文字以上の推測されにくい文字列にする。`monitor-app deploy` が生成して `.env` に書く。空の値は未設定と同じ |
| `MONITOR_PROTECT_READS` | `false` | 閲覧(画面・読み取り系 API・API ドキュメント)にもキーを必要にする |
| `MONITOR_ADMIN_KEY` | (未設定) | 管理の機能(画面での設定の編集・CSV の取り込み)だけに使う鍵。設定すると、管理の機能はこの鍵でだけ使える(この鍵でログインすれば閲覧・書き込みもできる)。未設定なら `MONITOR_API_KEY` で使える。上の「**画面で設定を変える・CSV を取り込む**」を参照 |
| `MONITOR_ALLOWED_HOSTS` | (未設定) | 受け付けるホスト名(カンマ区切り。`*.example.com` でサブドメイン、`*` で確かめない)。未設定なら、`127.0.0.1` で待ち受けるサーバーは `localhost`・自分の名前・IP アドレス以外のホスト名を断る(DNS リバインディング対策)。LAN・Docker・下のリバースプロキシの設定では確かめない。一覧を書くと、どのアドレスで待ち受けても確かめる |
| `MONITOR_CORS_ORIGINS` | `[]` | API を呼べる別オリジンの Web アプリ(JSON の配列。例: `["http://localhost:3000"]`)。空なら同一オリジンのみ |
| **CSV の取り込み** | | |
| `MONITOR_CSV_DIR` | `csv` | CSV を置くフォルダ(コマンドを実行したフォルダからの相対パス) |
| `MONITOR_CSV_ENCODING` | `auto` | `auto` は UTF-8(BOM の有無を問わない)として読み、読めなければ代わりの文字コードで読む。決まっているなら `utf-8-sig` / `cp932` / `cp1252` など |
| `MONITOR_CSV_ENCODING_FALLBACK` | `cp932` | `auto` が UTF-8 の次に試す文字コード(カンマ区切り) |
| `MONITOR_INGEST_WATCH` | `false` | CSV のフォルダを監視し、変更されたファイルを自動で取り込む(`sources` も定期的に取り込む) |
| `MONITOR_INGEST_INTERVAL` | `5` | CSV のフォルダを確かめる間隔(秒) |
| `MONITOR_INGEST_MODE` | `replace` | `replace`(テーブルを CSV の内容にする)/ `append`(CSV に追記された行だけ。読み取り位置を DB に保存するため、サーバーを止めている間に追記された行も次の起動で取り込む) |
| **ビューのキャッシュ** | | |
| `MONITOR_VIEW_CACHE_TTL_MS` | `1000` | ビューの結果をクライアント間で共有する時間(ミリ秒、`0` でキャッシュしない)。PostgreSQL / MySQL と時刻に依存する SQL に使う |
| `MONITOR_VIEW_CACHE_MAX_AGE_MS` | `15000` | ファイルの SQLite では変更をすぐ検知する。データが変わらない間に結果を使い回す最大時間(ミリ秒) |
| `MONITOR_VIEW_CACHE_MAX_MB` | `256` | ビューのキャッシュが使うメモリのおおよその上限(MB)。超えたら最近使っていない結果から捨てる(`0` で上限なし)。Raspberry Pi などメモリの少ない PC では小さめにする |
| **バックアップ・保持期間** | | |
| `MONITOR_BACKUP_DIR` | (未設定) | SQLite の定期バックアップの保存先。未設定なら定期バックアップしない |
| `MONITOR_BACKUP_INTERVAL_HOURS` | `24` | 定期バックアップの間隔(時間、`0` で無効)。期限を過ぎていれば起動時にすぐ取る |
| `MONITOR_BACKUP_KEEP` | `7` | 新しい順に残すバックアップの数(`0` ですべて残す)。`backup --keep` の既定値にもなる |
| `MONITOR_RETENTION_INTERVAL_MINUTES` | `10` | `TableDef.retention` による古い行の削除を実行する間隔(分、`0` で無効) |
| **アラート通知** | | |
| `MONITOR_WEBHOOK_URL` | (未設定) | `notify=["webhook"]` の送り先(Slack・Teams・汎用の JSON POST) |
| `MONITOR_SMTP_HOST` | (未設定) | `notify=["email"]` に使う SMTP サーバー |
| `MONITOR_SMTP_PORT` | `587` | SMTP のポート。指定しなければ `MONITOR_SMTP_SECURITY` に合わせて 587 / 465 / 25 |
| `MONITOR_SMTP_SECURITY` | `starttls` | `starttls` / `ssl`(SMTPS)/ `none`(暗号化なし。社内の中継サーバー向け) |
| `MONITOR_SMTP_TLS_VERIFY` | `true` | SMTP サーバーの証明書を検証する(`starttls`・`ssl`)。社内の CA なら環境変数 `SSL_CERT_FILE` に CA のファイルを指定する(`.env` に書いても効かない)。検証できない自己署名の中継サーバーのときだけ `false` |
| `MONITOR_SMTP_USER` | (未設定) | SMTP のユーザー名 |
| `MONITOR_SMTP_PASSWORD` | (未設定) | SMTP のパスワード |
| `MONITOR_ALERT_EMAIL_TO` | (未設定) | 宛先(カンマ区切りで複数可) |
| `MONITOR_NOTIFY_RETRIES` | `2` | 通知の送信に失敗したときの再試行回数(1・2・4…秒待つ。最大 30 秒) |
| **監査ログ** | | |
| `MONITOR_AUDIT_ENABLED` | `false` | テーブルの API と入力フォームからの変更(誰が・いつ・変更前後)を記録する。`/api/audit` で見られる。一括登録(ingest)・CSV の取り込み・`sources`・保持期間による削除は記録しない |
| **ログ** | | |
| `MONITOR_LOG_LEVEL` | `INFO` | `critical` / `error` / `warning` / `info` / `debug` / `trace`(大文字・小文字は問わない) |
| `MONITOR_LOG_FORMAT` | `text` | `text` / `json`(1 行 1 つの JSON。ログ収集基盤向け) |

- 次の 2 つは **環境変数でだけ** 効き、`.env` に書いても効きません: `MONITOR_DEBUG=1`(トレースバックを表示。`--debug` と同じ)と `MONITOR_CONFIG_PATH`(`uvicorn monitor_app.main:app_factory --factory` が読む `config.py`。プロジェクトのフォルダや `~` で始まるパスも可。`runserver` は `--config` の値を設定します)。
- `$` を含む値はシングルクォートで囲みます(`MONITOR_API_KEY='ab$cd'`)。Docker Compose も `.env` を読むためです。
- 値の誤りはトレースバックではなく短いメッセージで説明します。例: `MONITOR_PORT='abc'(整数を指定してください)`。パスワード・キー・URL の値は表示しません。
- `monitor-app check` は設定として読まれない `MONITOR_*`(綴りの誤り)を知らせます。

## ⚡ 性能とデータの保全

**SQLite の WAL モード。** ファイルの DB は `journal_mode=wal` と `synchronous=normal` で動きます。読み取りと書き込みが互いを待たなくなり、書き込みのたびのディスクへの同期も大幅に減ります。停電で直前のいくつかのコミットが失われることはありますが、DB は壊れません。すべてのコミットを確実に残したいときは `MONITOR_SQLITE_SYNCHRONOUS=full` にします。WAL では DB の隣に 2 つのファイル(`database.db-wal`・`database.db-shm`)ができます。移動やコピーは 3 つ一緒に行うか、`monitor-app backup` を使ってください。ネットワーク共有では WAL が使えないため、UNC パス・ネットワークドライブ・NFS / SMB・WSL の `/mnt/c`・Docker Desktop の共有フォルダでは自動で `delete` にします。macOS の共有フォルダは判別できないため `MONITOR_SQLITE_JOURNAL_MODE=delete` を設定します。`delete` にすると 2.3 と同じ動きに戻ります。

**バックアップ。** サーバーの稼働中に `database.db` をコピーしないでください。WAL モードでは、最新の行が `-wal` ファイルにしか無いことがあります。

```sh
monitor-app backup                       # 今すぐ(稼働中でもよい)→ backups/database-YYYYmmdd-HHMMSS.db
monitor-app restore backups/database-20261021-172955.db         # 置き換わる内容を表示するだけ
monitor-app restore backups/database-20261021-172955.db --yes   # 先にサーバーを止める
```

- `.env` に `MONITOR_BACKUP_DIR=backups` を書くと定期的にバックアップします(`MONITOR_BACKUP_INTERVAL_HOURS` ごと。新しい `MONITOR_BACKUP_KEEP` 個を残す)。夜は電源を切る PC でも、次の起動時に遅れていた分を取ります。
- バックアップは 1 つのファイルで、所有者だけが読めます。`restore` は先にバックアップを点検し(SQLite の `quick_check`。壊れたバックアップは拒否し、DB はそのまま)、次に今の DB をバックアップのフォルダへ退避します。開けなくなった DB(SD カードでの停電のあとなど)も復旧でき、壊れたファイルは `backups/database-damaged-YYYYmmdd-HHMMSS.db` として残します。表の無いファイル(コピーに失敗した空のファイルなど)は拒否します。
- 複数のプロジェクトで 1 つのバックアップのフォルダを共有できます。各 DB はファイルの先頭に印(ID)を付けてバックアップし、世代の整理と「最後のバックアップ」には自分のファイルだけを数えます。中断したバックアップの一時ファイルは 1 時間たつと消し、時計が狂って未来の日時になったバックアップがあっても定期バックアップは止まりません。
- PostgreSQL と MySQL は `pg_dump` / `mysqldump` を使います(コマンドが案内します)。

**大きく増え続けるテーブル。** 時系列のテーブルには `Retention(...)` を付け、並べ替え・絞り込みに使う列に `indexes=[["ts"]]` を張り、ビューが返す行を絞ります(`ORDER BY ts DESC LIMIT 1000` や `max_rows=`)。内部では、各ビューを 1 回だけ計算してすべてのブラウザ・大型表示・アラートの評価で共有します。SQLite では、ほかのプログラム(cron の `sync-sources` など)による変更もすぐ検知し、変わらない結果は最大 `MONITOR_VIEW_CACHE_MAX_AGE_MS`(15 秒)使い回します。PostgreSQL / MySQL では `MONITOR_VIEW_CACHE_TTL_MS`(1 秒)使い回します。サーバー自身を通した書き込み(API・フォーム・フォルダの監視)はすぐに表示されます。キャッシュが使うメモリは `MONITOR_VIEW_CACHE_MAX_MB`(256 MB)までです。データが変わらなければ `304 Not Modified` を返し、レスポンスは gzip で圧縮します(ビューの圧縮した本文はキャッシュした結果と一緒に残すので、圧縮はデータが変わったときに 1 回だけです)。

**CSV の文字コード。** 既定の `MONITOR_CSV_ENCODING=auto` では、各ファイルを UTF-8(BOM の有無を問わない)として読み、読めなければ cp932(Excel の「CSV(コンマ区切り)」)として読みます。そのときはファイル名と使った文字コードを警告します。注意: 西欧の Windows で作った CSV(cp1252)も cp932 として読まれて文字化けします(`25°C` → `25ｰC`)。そのようなファイルには `MONITOR_CSV_ENCODING=cp1252`(または `MONITOR_CSV_ENCODING_FALLBACK=cp1252`)を設定します。UTF-8 のファイルに壊れたバイトが少しあっても cp932 には切り替えず、そのバイトを含む行だけを、行番号つきの警告を出して読み飛ばします。NUL(SD カードでの停電で残る)は取り除き、CSV として解析できない行は警告して読み飛ばします。

**安全な取り込み。** 取り込みは 1 つのトランザクションで行います(全部か無しか)。見出しの表記ゆれ(`Name` と `name`、全角文字、余分な空白、BOM)は宣言した列に合わせて取り込みます。置き換えの取り込みでは、宣言した列(主キー以外)が見出しに無いテーブルを置き換えずに残します(`--force` で置き換え)。どのテーブルにも合わない CSV ファイルは一覧にし、大文字・小文字だけの違いなら正しい名前を提案します。値がすべて空の行(Excel の `,,,` の行)は読み飛ばします。1 つの CSV の中で主キーが重複していれば、両方の行番号を示して取り込みを中止します。PostgreSQL / MySQL では、列に入らない値(255 文字を超える文字列・範囲外の数値)はその行だけを警告して読み飛ばします。SQLite の置き換えの取り込みは、先に CSV を一時的な表に読み込み、最後の入れ替えのときだけ DB をロックするため、大きな取り込みの間もフォームや API が使えます(表のデータと同じくらいの一時的なディスク容量が必要です)。

**フォルダの監視。** `MONITOR_INGEST_WATCH=true` にすると、サーバーの稼働中に変更された CSV を取り込みます。`MONITOR_INGEST_MODE=replace` はテーブルを置き換えます(同時に変わった CSV はまとめて置き換え)。`append` はファイルに追加されたバイトだけを読むため、100 万行のログでも負担が増えず、書きかけの最後の行は完成するまで待ちます。ファイルがローテートされたり書き直されたりしたときは先頭から読み直し、主キーが既にある行は読み飛ばしますが、主キーの列が無いファイルではそれらの行がもう一度入ります。読み取り位置は DB に保存するため、サーバーを止めている間に追記された行も次の起動で取り込みます。例外が 2 つあります。起動時に全体を取り込んだ直後(`runserver --import-csv`、または `--import-csv-if-empty` の初回の起動)は、監視が始まるまでの数秒間に追記された行を取り込みません(`monitor-app serve --append` にはこの隙間がありません)。また、2.3 から更新した最初の起動はファイルの末尾から読み始めます。取り込みに失敗したファイル(「database is locked」など)は次の確認で取り込み直し、その後は間隔を延ばしながら(最大 60 秒)繰り返します。成功するまで `/api/health` がエラーを示します。

**停止。** `docker stop`・`systemctl stop`・Ctrl+C では、実行中のバックグラウンドの処理(フォルダの監視・保持期間による削除・アラートの評価・`sources`)を次の SQL の実行時に打ち切り、トランザクションを取り消します。サーバーが待つのは最大約 4.5 秒なので、長い取り込みのせいでコンテナが強制終了されるまで停止が遅れることはありません。

## 🐳 公開・運用

### Docker

```sh
docker run --rm -p 9990:9990 ghcr.io/mikawa-bushi/monitor-app       # 物流倉庫の在庫画面のデモ(ファイル不要)
```

自分のプロジェクト(`config.py` のあるフォルダ)を動かす:

```sh
cd myproject
docker run -d --name monitor-app -p 9990:9990 -v "$PWD":/project \
    --restart unless-stopped ghcr.io/mikawa-bushi/monitor-app
```

(Windows の PowerShell では `-v "${PWD}:/project"`。)コンテナはマウントしたフォルダの所有者のユーザーに切り替え、Linux の特権(capability)をすべて捨てて動くため、ホストのユーザー ID が何であっても、DB やバックアップはホストの利用者のファイルになります。起動コマンドは `runserver --import-csv-if-empty` なので、画面で入力した行は更新しても残ります。空のフォルダをマウントするとサンプルのプロジェクトを書き込みます(何もマウントしなければ、上のデモが一時的な DB で動きます)。さらに守りを固めるなら `--cap-drop ALL --cap-add SETUID --cap-add SETGID --cap-add CHOWN --security-opt no-new-privileges` を付けます(生成する `compose.yaml` には設定済み)。`docker run --rm ghcr.io/mikawa-bushi/monitor-app --version` で版を表示します。

Docker Compose を使うときは、Monitor App にファイルを書かせます:

```sh
monitor-app deploy docker      # compose.yaml と .dockerignore を書き出す
docker compose up -d           # http://localhost:9990/
docker compose ps              # サーバーが応答すると (healthy)
docker compose logs -f
docker compose pull && docker compose up -d     # 最新のイメージに更新
```

- `monitor-app deploy` は、`.env` に `MONITOR_API_KEY` が無ければ推測されにくいキーを生成して書きます(書いた場所を表示し、キーそのものは表示しません)。閲覧はキーなしでできますが、行の追加・変更・削除にはキーが必要です(画面でログインするか、`X-API-Key` を送る)。
- `.env` はコンテナの中のアプリがふだんどおり読みます。生成した `compose.yaml` は `.env` から `MONITOR_PORT`(ホスト側のポート)・`TZ`(例: `TZ=Asia/Tokyo`)・`MONITOR_IMAGE`(イメージの名前)・`MONITOR_IMAGE_TAG`(例: `2.5.0` で版を固定)も読みます。
- ほかのコマンドも同じく所有者のユーザーで実行できます: `docker compose run --rm monitor-app check`、`... backup`、`... import-csv`。`docker compose exec monitor-app monitor-app backup` は使わないでください。root で動くため、バックアップのファイルを root しか読めなくなります。
- イメージのタグ: `latest`・`2`・`2.5`・`2.5.0`。プラットフォームは `linux/amd64` と `linux/arm64`(64 ビットの Raspberry Pi OS)。ヘルスチェック(`/api/health`)つきで、停止は約 1 秒です。
- Docker Desktop(Windows・macOS)では、速度のためと「database is locked」のエラーを避けるため、`instances/` を名前付きボリュームにします(`compose.yaml` のコメントを参照)。イメージを自分でビルドするには `docker build -t monitor-app .` のあと、`.env` に `MONITOR_IMAGE=monitor-app` を書き、`pull` せずに `docker compose up -d` します。詳しくは[docker/README.md](https://github.com/mikawa-bushi/monitor-app/blob/main/docker/README.md)(英語)。

### Linux・Raspberry Pi(systemd)

先に pipx か `uv tool` で Monitor App をインストールします(`uvx` は不可。サービスには場所の変わらない Python が必要です)。そのあとプロジェクトのフォルダで:

```sh
monitor-app deploy systemd       # このフォルダ・ユーザー・Python 用の monitor-app.service を書き出す
sudo cp monitor-app.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now monitor-app
systemctl status monitor-app
journalctl -u monitor-app -f     # ログ
```

サービスは `runserver --import-csv-if-empty` をあなたのユーザーで動かし、異常終了したら再起動し、プロジェクトの `.env`(`deploy` が生成した API キーを書いたファイル。`sudo` で実行したときは、新しく作る `.env` の所有者は `sudo` を実行したユーザー)を読みます。`deploy systemd` は、フォルダに `config.py` が無ければ止まります(`--force` で書き出す。`--config FILE` で別の設定ファイルを使う)。複数のプロジェクトを動かすときはユニットファイルの名前を変えます(`monitor-app-line2.service`)。

### Windows(PC の起動時に開始)

プロジェクトのフォルダで:

```powershell
monitor-app deploy windows       # start-monitor.bat と install-task.ps1 を書き出す
.\start-monitor.bat              # 動作確認(ログ: logs\monitor.log)
# 管理者として: PC の起動のたびに開始(あなたのユーザーで動く。パスワードは保存しない)
powershell -ExecutionPolicy Bypass -File .\install-task.ps1
# 管理者権限なし: サインインしたときに開始
powershell -ExecutionPolicy Bypass -File .\install-task.ps1 -AtLogon
# タスクを削除
powershell -ExecutionPolicy Bypass -File .\install-task.ps1 -Uninstall
```

プロジェクトはローカルのドライブに置きます(起動時のタスクからはネットワークドライブを使えません)。プロジェクトのフォルダ・`config.py`・`start-monitor.bat` をこの PC のほかのユーザーも変更できるときは、スクリプトが警告します。`-AsSystem` を付けると SYSTEM アカウントで動かします(おすすめしません。プロジェクトのフォルダにあるものがすべての権限で動きます)。タスク スケジューラは異常終了したサーバーを再起動しません。止まったときは `logs\monitor.log` を確かめてください。

### リバースプロキシ配下のサブパスで公開する

`https://server/monitor/` のように公開するには、`.env` に `MONITOR_ROOT_PATH=/monitor` を書きます。プロキシがプレフィックスを外す設定でも外さない設定でも動きます。nginx:

```nginx
location /monitor/ {
    proxy_pass http://127.0.0.1:9990/;    # 末尾の "/" ありは /monitor を外す。なしは残す(どちらも動く)
    proxy_http_version 1.1;
    proxy_set_header Host $http_host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_buffering off;                  # リアルタイム更新(SSE)をすぐ届ける
    proxy_read_timeout 1h;
}
```

Caddy:

```caddyfile
# ":80" はポート 80 の HTTP で公開する。代わりにホスト名(例: monitor.example.com)を
# 書くと、Caddy が HTTPS の証明書も取得する。
:80 {
    handle /monitor* {
        reverse_proxy 127.0.0.1:9990
    }
}
```

- `X-Forwarded-*` ヘッダは `MONITOR_FORWARDED_ALLOW_IPS`(既定 `127.0.0.1`)からのものだけを信頼します。プロキシが別のマシンにあるとき、または Monitor App を Docker で動かすとき(接続は Docker のゲートウェイ、例えば `172.17.0.1` から届く)は、プロキシのアドレスを設定します。そのポートにプロキシしか接続できないなら `*` でもかまいません。これを設定して初めて、HTTPS の配下でログインの Cookie に `Secure` が付きます。
- アプリ自身のパス(`/api`・`/static`・`/table`・`/form`・`/kiosk`・`/login`・`/docs`)と同じサブパスは選ばないでください(選ぶとログに警告が出ます)。
- ログインの Cookie はサブパスに限定されるため、同じホストの複数のインスタンスでセッションが混ざりません。
- 同じマシンのプロキシが Monitor App をルートのパスで(`MONITOR_ROOT_PATH` なしで)公開し、元の `Host` ヘッダのまま `127.0.0.1` へ転送するときは、`MONITOR_ALLOWED_HOSTS=<公開する名前>` が必要です。設定しないと要求が `400 Invalid host header` になり、ログに設定の名前が出ます。

### PostgreSQL・MySQL

```sh
pipx install "monitor-app[postgres]"   # 高速な PostgreSQL ドライバ psycopg
```

```sh
# .env
MONITOR_DATABASE_URL=postgresql+psycopg://monitor:change-me@db-server:5432/monitor
# MONITOR_DATABASE_URL=postgresql+pg8000://monitor:change-me@db-server:5432/monitor   # extra 不要
# MONITOR_DATABASE_URL=mysql+pymysql://monitor:change-me@db-server:3306/monitor
```

- パスワードの記号は URL エンコードして書きます: `@` → `%40`、`:` → `%3A`、`/` → `%2F`。エンコードしていない `@` があると、どのコマンドも短いメッセージで止まります(パスワードは表示しません)。`postgres://...` の URL(一部のクラウドサービスが示す形式)は、`postgresql+psycopg://...`(psycopg が無ければ `postgresql+pg8000://...`)に書き換えるよう案内して止まります。
- 起動時に最大 `MONITOR_DB_WAIT_TIMEOUT`(15 秒)まで DB サーバーを待つため、同時に起動しても動きます(Docker Compose・PC の起動時)。応答しないホストでも、1 回の接続は 10 秒で諦めます。
- 表は初回の起動で作ります。`monitor-app check` が `config.py` と既存の表を比べ、`monitor-app migrate` が新しい列を追加します。
- **2.4 で作る表** は、主キー・外部キー以外の整数の列を `BIGINT` にし(Unix 時刻のミリ秒も入る)、MySQL では日時の 1 秒未満も保ちます(`DATETIME(6)`)。以前に作った表で列に入らない値の行は、ファイル・行・列を示す警告を出して読み飛ばします。
- **2.3.x 以前に作った MySQL の表** は、小数の列が単精度です(`1234567.89` が `1234570` になる)。小数の列ごとに `ALTER TABLE <表> MODIFY <列> DOUBLE;` を実行します。起動時の警告と `migrate` が正確な文を表示します(日時の列には `check` が `DATETIME(6)` の文を表示します)。PostgreSQL と SQLite は対応不要です。
- **2.3.x で入れた PostgreSQL の DB**: CSV から取り込んだ行より遅れている id の連番を 1 回だけ進めます(初回の起動・取り込み・`sync-sources`・`migrate` のとき)。フォームや API の追加が主キーの重複で失敗しなくなります。DB のユーザーに連番を変える権限が無いときは、管理者に実行してもらう SQL を警告に表示します。
- MariaDB では、`max_rows` や `?limit=` に頼らず、ビューの SQL の中に `LIMIT` を書いてください(MariaDB はサブクエリの中の `ORDER BY` を無視することがあります)。
- 連携プリセット(下の「**外部ツール連携**」)は SQLite の日付関数を使うため、SQLite が必要です。

### 死活監視: `/api/health`

```sh
curl http://127.0.0.1:9990/api/health
```

```json
{"status":"ok","version":"2.5.0","database":"ok","tasks":{"alert scheduler":{"running":true,"last_tick":"2026-10-21T17:30:44+09:00","last_error":null}}}
```

DB が `SELECT 1` に応答すれば `200`、応答しなければ `503`(`"status":"error"`)を返します。ログインは不要で、`HEAD` にも応答します。`tasks` はバックグラウンドの処理(`ingest watcher`・`alert scheduler`・`retention`・`backup`)が動いているか、最後に成功した時刻、直近の失敗の種類を示します。

## 🌐 エンドポイント

| パス | 説明 |
|---|---|
| `/` | ホーム: ミニグラフのダッシュボード・KPI カード・ビュー一覧 |
| `/table/{view}` | リアルタイムの表 / グラフ |
| `/kiosk` | Andon 大型表示(全画面・自動で切り替え) |
| `/form/{table}` | 作業者の入力フォーム |
| `/settings` | 設定ページ(管理の機能) |
| `/upload` | CSV の取り込み(管理の機能) |
| `/login` | ログイン画面(`MONITOR_API_KEY`・`MONITOR_ADMIN_KEY` を設定したときに使う) |
| `/docs`、`/redoc` | OpenAPI ドキュメント(この 2 つのページは版を固定した Swagger UI / ReDoc を CDN から読み込む。`MONITOR_PROTECT_READS` ではログインが必要。`MONITOR_API_DOCS=false` で出さない) |
| `GET/POST /api/tables/{table}` | 行の一覧(`?limit=`、`?offset=`。ページは主キーの順)/ 行の追加 |
| `GET/PUT/DELETE /api/tables/{table}/{id}` | 1 行 |
| `POST /api/ingest/{table}` | 一括登録(オブジェクト 1 つか配列)。`?on_conflict=error`(既定。主キーの重複は `409` で何も保存しない)/ `ignore`(既にあるキーは読み飛ばす。再送しても安全)/ `update`(上書き) |
| `GET /api/views/{view}` | ビューのデータ(`?limit=`、`?offset=`)。`ETag` を返し、変わっていなければ `304` |
| `GET /api/views/batch?names=a,b` | 複数のビューを 1 回で取得 |
| `GET /api/views/{view}/stream` | リアルタイム更新(Server-Sent Events) |
| `GET /api/views/{view}/export` | `?format=csv` か `xlsx`。`?bom=1` で UTF-8 の BOM を付ける(Excel で日本語が化けない)。`=` `+` `-` `@` で始まる文字列は、表計算ソフトが数式として実行しない形で書き出す。`?raw=1` なら保存した値そのまま |
| `GET /api/kpis`、`/api/alerts`、`/api/schema` | KPI の値・発生中のアラート・表定義 |
| `GET /api/audit` | 変更履歴(`?table=`、`?limit=`)。`MONITOR_AUDIT_ENABLED=true` のとき |
| `GET/PUT/DELETE /api/settings` | 画面で編集できる設定の取得 / 点検して保存(`{"settings": {...}, "revision": "..."}`。エラーは `422`、ほかの人が先に保存していたら `409`)/ `config.py` の設定に戻す。管理の機能 |
| `POST /api/settings/check` | 保存せずに点検する(`monitor-app check` と同じ判定)。管理の機能 |
| `POST /api/upload/{table}` | CSV の取り込み(multipart の `file`、`mode=append` か `replace`、`force`)。管理の機能 |
| `GET/HEAD /api/health` | 死活監視(上を参照) |

`MONITOR_API_KEY` を設定したら、キーを `X-API-Key` ヘッダで送ります:

```sh
curl -X POST "http://127.0.0.1:9990/api/ingest/readings?on_conflict=ignore" \
     -H "X-API-Key: YOUR-KEY" -H "Content-Type: application/json" \
     -d '[{"id": 100, "ts": "2026-10-21T08:00:00+09:00", "line": "L1", "temp": 25.5, "ok": true}]'
# {"success":true,"inserted":1,"skipped":0}
```

エラーはコードつきの JSON で返します。例: `{"detail": {"code": "CONFLICT", "message": "..."}}`。`404`(`TABLE_NOT_FOUND`・`VIEW_NOT_FOUND`・`RECORD_NOT_FOUND`)、`409 CONFLICT`(主キーの重複・参照されている行)、`422 INVALID_PAYLOAD`(長すぎる・範囲外・形式の誤った値)、`503 DATABASE_BUSY`(大きな取り込みなどで DB がロックされている。`Retry-After` の秒数の後にやり直す)、`500`(`DB_SCHEMA_MISMATCH`・`DATABASE_ERROR`・`INTERNAL_ERROR`)。`500` にはサーバーのログの詳細を探すための `ref=` が付きます。SQL や送った値は応答に含めません。

## 🔌 外部ツール連携

procdiag・netdiag・network-checker を数行で Monitor App に接続できます。外部ツール側の変更は不要です(Monitor App はファイルを読むだけ)。

```python
from monitor_app.integrations import procdiag, netdiag, network_checker

config = procdiag.attach(config, db_path="/opt/procdiag/data/procdiag.db")
config = netdiag.attach(config)
config = network_checker.attach(config, hosts=["plc-01", "gateway"])
```

各 `attach()` は既存の config にテーブル・ビュー・KPI・アラートを合成して返します。`monitor-app sync-sources` で必要なときに取り込み(cron からなど)、`MONITOR_INGEST_WATCH=true` で常時取り込みます。詳細: [docs/design/integrations_v2_2.md](https://github.com/mikawa-bushi/monitor-app/blob/main/docs/design/integrations_v2_2.md)

## 🐍 Python API

ふつうは `monitor-app` コマンドだけで足ります。自分のプログラムに Monitor App を組み込むときは、次の名前を使ってください。互換性のテストで守っているため、2.x の間は使い方が変わりません:

| 名前 | 用途 |
|---|---|
| `monitor_app.__all__` のすべて(`MonitorConfig`・`TableDef`・`ViewDef`・`Retention` など) | `config.py` のモデル |
| `monitor_app.main.create_app(config, settings=None)` | `MonitorConfig` と `AppSettings` から FastAPI のアプリを作る |
| `monitor_app.main.app_factory` | 同じものを `MONITOR_CONFIG_PATH` から作る(`uvicorn monitor_app.main:app_factory --factory`) |
| `monitor_app.settings.loader.load_config(path)`、`ConfigError` | `config.py`(またはプロジェクトのフォルダ)を読み込んで検証する |
| `monitor_app.settings.runtime.AppSettings` | `MONITOR_*` の実行時設定 |
| `monitor_app.integrations.merge`、`procdiag.attach`・`netdiag.attach`・`network_checker.attach` | 連携プリセット |

```python
import uvicorn
from monitor_app.main import create_app
from monitor_app.settings.loader import load_config
from monitor_app.settings.runtime import AppSettings

app = create_app(load_config("myproject"), AppSettings(database_url="sqlite:///data.db"))
uvicorn.run(app, host="127.0.0.1", port=9990)
```

ロガーの名前(`monitor_app`・`monitor_app.importer` など)も変わりません。これ以外のモジュール(`monitor_app.services`・`monitor_app.db`・`monitor_app.checks`・`monitor_app.cli` など)は内部用で、どの版でも変わることがあります。

---

## ⬆️ 2.3.x からの更新

```sh
pipx upgrade monitor-app          # または: uv tool upgrade monitor-app / pip install -U monitor-app
monitor-app check --data          # 読み取りだけ。直すべき点を知らせる
```

既存のプロジェクトと DB はそのまま動きます。気づく可能性のある変更:

- **SQLite が WAL になります**(初回の起動時)。DB の隣に `database.db-wal` と `database.db-shm` ができます。`database.db` だけをコピーせず、`monitor-app backup` でバックアップしてください。`MONITOR_SQLITE_JOURNAL_MODE=delete` で従来のモードに戻ります。
- **CSV の文字コードを自動判別します**(`MONITOR_CSV_ENCODING=auto`)。失敗していた cp932 の CSV が取り込めるようになりました。cp1252 の CSV は失敗せず文字化けして取り込まれるため、`MONITOR_CSV_ENCODING=cp1252` を設定してください。
- **`runserver --import-csv-if-empty` を使ってください。** `--import-csv` は従来どおり起動のたびにテーブルを置き換え、入力フォームのあるテーブルを置き換えると警告を出すようになりました。Docker イメージは `--import-csv-if-empty` で起動します。
- **取り込みが慎重・安全になりました**: 置き換えの取り込みでは、CSV の見出しに宣言した列(主キー以外)が無いテーブルの既存の行を残します(`import-csv` は終了コード 1。`--force` で置き換え)。`--keep` は既にあるキーを読み飛ばし、失敗しなくなりました。値がすべて空の行は読み飛ばします。
- **コマンドの出力**: エラーは stderr への短いメッセージになりました(`--debug` でトレースバック)。uvicorn の行に時刻が付き、アクセスログは stdout ではなく stderr に出ます。`runserver` は取り込みの前にポートが空いているか確かめ、常に 1 プロセスで動きます。
- **`/api/health`** は DB に接続できないと `503` を返し、本文に `version`・`database`・`tasks` が加わりました。
- **一度ログインし直してください**: 2.3.x のログインのセッションは使えなくなります。セッションは 12 時間で切れ、ログアウトするとサーバー側でも無効になります。
- **空の `MONITOR_API_KEY=` の行** は「キーなし」になりました。従来は認証が有効に見えて、実際には回避できました。本物のキーを書くか、行を消してください。
- **メールの通知は SMTP サーバーの証明書を検証します。** 自己署名などの正しくない証明書の中継サーバーは、環境変数 `SSL_CERT_FILE`(社内の CA)か `MONITOR_SMTP_TLS_VERIFY=false` を設定するまで送れません。
- **ホスト名を確かめます**(`127.0.0.1` で待ち受けるとき。`MONITOR_ALLOWED_HOSTS` を参照)。設定が必要なのは、同じマシンのリバースプロキシがルートのパスで公開し、元の `Host` ヘッダのまま転送する場合だけです(`MONITOR_ALLOWED_HOSTS=<公開する名前>`)。
- **2.3.x で入れた PostgreSQL の DB** は、id の連番を初回の起動で自動的に進めます(作業は不要)。
- **アラート** はビューの全データでだけ評価します。`?limit=` のページ取得でアラートが発火・復帰しなくなりました(同じ通知が繰り返されません)。
- **レスポンスを gzip で圧縮します**(`MONITOR_COMPRESS_RESPONSES=false` で無効)。`POST /api/ingest` の主キーの重複は `409` の JSON を返します。PostgreSQL / MySQL では `/api/views/batch` も `GET /api/views/{view}` と同じく DECIMAL を文字列で返します。DB のロックは `503 DATABASE_BUSY`(従来は `500 DATABASE_ERROR`)、そのほかの予期しないエラーはテキストではなく JSON(`500 INTERNAL_ERROR`)で返します。エクスポートは `=` `+` `-` `@` で始まる文字列を表計算ソフトから守ります(`?raw=1` でそのまま)。
- **`date` / `datetime` の値** の形式が広がりました(`2026/10/21`、`Z`、`+09:00`)。タイムゾーンつきの値は PC のローカル時刻で保存します。
- **config.py**: 未知のキーは警告になります(`python -W error` ではエラー)。ビューと KPI の SQL の末尾の `;` を許します。
- **MySQL**: 2.3 で作った表の小数の列は単精度です。起動時に表示される `ALTER TABLE ... DOUBLE` を実行してください。
- **Docker**: Dockerfile はリポジトリの直下に移り(`docker build -t monitor-app .`)、`docker/docker-compose.yml` は無くなりました(`monitor-app deploy docker` を使う)。コンテナは固定のユーザーではなく、プロジェクトのフォルダの所有者で動きます。
- **削除**: 使われていなかった画像 `img/background.webp` と `img/logo.webp`。
- **2.3.x に戻すとき**: `"datetime"` 型の列・`retention`・`max_rows` や、`monitor_app` からの `Retention`・`DashboardConfig`・`SourceDef` の import を使った `config.py` は 2.3.x では読み込めません。`config.py` で `sources` や procdiag・netdiag・network-checker のプリセットを使っているときは、2.4 がそれらの読み取り位置を 2.3.x の比べられない形で保存しています。**`_ingest_state` の行は削除しないでください**(2.3.x がすべてを取り込み直し、ポーリングのたびに失敗するか、行が二重に入ります)。代わりに、サーバーを止め、2.4 が入っているうちに、プロジェクトのフォルダで読み取り位置を戻します:

  ```sh
  python -c "from monitor_app.db.ingest_state import unwrap_typed_cursors as u; print(u('sqlite:///instances/database.db'))"
  ```

  monitor-app 2.4 の入った Python で実行します(仮想環境ならその `python`。pipx や `uv tool` で入れたなら `uvx --from monitor-app python -c "..."` で動きます)。PostgreSQL / MySQL では SQLite の URL の代わりに `MONITOR_DATABASE_URL` の値を書きます。戻した行数を表示します。SQL で行うなら、SQLite は `UPDATE _ingest_state SET cursor = json_extract(cursor, '$.v') WHERE cursor LIKE '{"v":%'`、PostgreSQL は `UPDATE _ingest_state SET cursor = (cursor::json ->> 'v') WHERE cursor LIKE '{"v":%'`、MySQL は ``UPDATE _ingest_state SET `cursor` = JSON_UNQUOTE(JSON_EXTRACT(`cursor`, '$.v')) WHERE `cursor` LIKE '{"v":%'`` です。そのあと 2.3.x を入れると、2.4 が止まったところから続けて取り込みます。2.4 も戻した形を読めるため、あとで再び更新しても問題ありません。WAL モードと、SQLite のファイルの先頭に付けたバックアップの印は 2.3.x に影響しません。

詳細と理由: [CHANGELOG.md](https://github.com/mikawa-bushi/monitor-app/blob/main/CHANGELOG.md)

## 🔄 v0.x からの移行

v2 で FastAPI + Pydantic に再構築しました。旧 dict 形式の `config.py`(`ALLOWED_TABLES` / `VIEW_TABLES` / `TABLE_CELL_STYLES`)も互換ローダーで**そのまま動きます**が、新規プロジェクトでは `MonitorConfig` を推奨します。主な変更:

- `python app.py runserver` → `monitor-app runserver`(アプリ本体はプロジェクト内に複製されません)
- DB 設定(`DB_TYPE`・パスワード)→ 環境変数 `MONITOR_DATABASE_URL`
- API パス: `/api/<table>` → `/api/tables/{table}`、`/api/view/<v>` → `/api/views/{view}`
- v0.x で作った表には主キーが無いことがあります。`monitor-app check` が知らせ、作り直す手順を示します。

設計ドキュメント: [再設計](https://github.com/mikawa-bushi/monitor-app/blob/main/docs/design/redesign_v2.md) · [v2.1 機能設計](https://github.com/mikawa-bushi/monitor-app/blob/main/docs/design/feature_expansion_v2.1.md) · [v2.2 外部連携](https://github.com/mikawa-bushi/monitor-app/blob/main/docs/design/integrations_v2_2.md) · [v2.3 UI 刷新](https://github.com/mikawa-bushi/monitor-app/blob/main/docs/design/ui_redesign_v2_3.md) · [ドキュメント一覧](https://github.com/mikawa-bushi/monitor-app/blob/main/docs/README.md)

## 🛠 開発

```sh
git clone https://github.com/mikawa-bushi/monitor-app.git
cd monitor-app
uv sync --all-extras                  # uv.lock から、全 extras と開発ツール入りの .venv を作る
uv run pytest                         # 全テスト
uv run pre-commit install             # フック(下を参照。Python 3.11 以上)

# 手元のコードを別フォルダのプロジェクトで試す:
uv run monitor-app startproject ../demo
cd ../demo
uv run --project ../monitor-app monitor-app runserver --import-csv-if-empty
```

テストのマーカー・互換性のテスト・コーディング規約は[CONTRIBUTING.md](https://github.com/mikawa-bushi/monitor-app/blob/main/CONTRIBUTING.md)(英語)を参照してください。

### push 前のセキュリティ / 品質チェック

[pre-commit](https://pre-commit.com/) 設定を同梱しており、**コードが手元を離れる前に**秘密情報スキャンと整形を行います。クローンごとに一度だけ有効化してください:

```sh
uv run pre-commit install          # commit と pre-push の両フックを設定
uv run pre-commit run --all-files  # 任意: いますぐ全ファイルに実行
```

- **gitleaks** — ハードコードされた秘密情報を検出してブロック(PyPI/AWS/GCP トークン・GitHub PAT・秘密鍵 等)
- **detect-private-key / check-added-large-files / check-merge-conflict**、および **black / ruff**(フックのバージョンは `.pre-commit-config.yaml` で固定。CI は `uv.lock` のバージョンを使う)
- **check-commit-identity**(pre-push)— author/committer メールが許可リスト外のコミットを拒否し、社用/別 identity の混入を防止(`scripts/check_commit_identity.py`、`MONITOR_ALLOWED_EMAILS` 環境変数で拡張可)
- **check-ai-traces**(commit + pre-push)— AI エージェントツールの痕跡(設定ディレクトリ・ファイル内容・コミットメタデータ)を検出し、リポジトリへの混入を防止。検出対象は `scripts/check_ai_traces.py` の `TOOLS`(データ駆動・編集可、`MONITOR_AI_TRACE_EXTRA` で追加、`.ai-trace-ignore` で除外)

**ワークツリー全体＋全 git 履歴**をオンデマンドで監査:

```sh
python3 scripts/check_ai_traces.py --all   # exit 0 = 痕跡なし
```

> ⚠️ ローカルフックは `git commit/push --no-verify` で回避できます。回避不可のゲートが必要なら、同じ `gitleaks` を GitHub Actions の必須チェックにも入れてください。

## 📜 ライセンス

[MIT](https://github.com/mikawa-bushi/monitor-app/blob/main/LICENSE)
