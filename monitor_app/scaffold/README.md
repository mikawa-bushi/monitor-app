# Monitor App プロジェクト

## 使い方(3ステップ)

```sh
# 1. (任意)設定の検証
monitor-app check

# 2. CSV をデータベースへ取り込み
monitor-app import-csv

# 3. サーバー起動 → http://127.0.0.1:9990
monitor-app runserver
```

`monitor-app runserver --import-csv` なら取り込みと起動を同時に行う。

## ファイル構成

| パス | 役割 |
|---|---|
| `config.py` | テーブル・ビュー・表示設定(ここを編集する) |
| `csv/` | 取り込む CSV ファイル(ファイル名がテーブル名になる) |
| `instances/` | SQLite データベースの保存先 |
| `.env` | DB 接続情報など環境ごとの設定(`.env.example` をコピー) |

## エンドポイント

- Web UI: `/`
- REST API: `/api/tables/{table}`、`/api/views/{view}`、`/api/schema`
- API ドキュメント: `/docs`(Swagger UI)、`/redoc`

## 注意

`import-csv` は既定で対象テーブルの既存行を置き換える。Web UI / API で
追加したデータも消えるため、残したい場合は `--keep` を付ける。
