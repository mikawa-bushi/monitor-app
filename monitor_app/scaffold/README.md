# Monitor App プロジェクト

## 使い方

```sh
# 1. (任意)設定の検証(キーの綴り・SQL・列名・DB の表定義の誤りを見つける)
#    --data で CSV の中身も、--strict で警告も失敗(終了コード 1)にする
monitor-app check

# 2. サーバー起動 → http://127.0.0.1:9990
#    初回だけ CSV を取り込む。画面・フォーム・API で入力した行は再起動しても残る
monitor-app runserver --import-csv-if-empty
```

CSV を更新して取り込み直すときは `monitor-app import-csv`(対象テーブルの既存行を
CSV の内容で置き換える)。`runserver --import-csv` は起動のたびに置き換えるため、
画面やフォームで入力した行が消える。残したい運用では `--import-csv-if-empty` を使う
(`import-csv --if-empty` も同じ。追記は `import-csv --keep`)。CSV の見出しに
config.py の列が無いテーブルは置き換えずに残す(`--force` で置き換える)。

config.py にあとから列を足したら `monitor-app migrate` で DB の表に列を追加する
(作成済みの表は起動しても変わらない)。`--dry-run` で実行する SQL を確かめられる。

ほかのフォルダから操作するときは `--config <このフォルダ>` を付ける(このフォルダの
`.env` も読む)。DB や csv/ の相対パスは、コマンドを実行したフォルダが基準になる
(DB がこのフォルダにしか無ければ、空の DB を作らずに `cd` するよう案内して止まる)。

## ほかの PC から使う・PC の起動時に動かす

`monitor-app deploy`(Docker・systemd・Windows 向け)が起動ファイルを書き出し、
`.env` に API キーが無ければ推測されにくいキーを生成して書く。閲覧はキーなしで
でき、行の追加・変更・削除には画面でのログイン(このキー)が必要になる。

## ファイル構成

| パス | 役割 |
|---|---|
| `config.py` | テーブル・ビュー・表示設定(ここを編集する) |
| `csv/` | 取り込む CSV ファイル(ファイル名がテーブル名になる) |
| `instances/` | SQLite データベースの保存先 |
| `.env` | DB 接続情報など環境ごとの設定(`.env.example` をコピー) |

## CSV の文字コード

既定(`MONITOR_CSV_ENCODING=auto`)では UTF-8 として読み、読めなければ cp932
(Excel の「CSV(コンマ区切り)」形式)として読む。Excel の「CSV UTF-8」形式も
そのまま読める。文字コードが決まっている場合は `.env` に
`MONITOR_CSV_ENCODING=cp932` のように指定する。

## バックアップ

SQLite の DB は、稼働中にファイルを単純にコピーすると最新の書き込みが欠けることがある。
`.env` に `MONITOR_BACKUP_DIR=backups` を設定すると、稼働中でも安全な方法で定期的に
バックアップする(世代数は `MONITOR_BACKUP_KEEP`)。

```sh
monitor-app backup                 # 今すぐバックアップ(稼働中でもよい。既定: backups/)
monitor-app restore backups/database-YYYYmmdd-HHMMSS.db --yes   # サーバーを止めてから
```

復元の前に、今の DB をバックアップの保存先へ退避する。停電などで DB が壊れて開けなく
なったときも `restore` で戻せる(壊れたファイルは `database-damaged-日時.db` として残す)。

## CSV だけで試す(設定ファイルなし)

`monitor-app serve <CSV のフォルダ>` は、config.py なしで CSV の中身から列の型・
グラフを推論してダッシュボードを開く(データは一時ファイルに入れ、終了時に消える)。
`--write-config new_config.py` を付けると推論した設定を書き出せるので、新しい CSV の
テーブル定義を作る雛形にもなる。

## エンドポイント

- Web UI: `/`
- REST API: `/api/tables/{table}`、`/api/views/{view}`、`/api/schema`
- API ドキュメント: `/docs`(Swagger UI)、`/redoc`
