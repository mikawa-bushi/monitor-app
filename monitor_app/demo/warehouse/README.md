# 物流センター在庫モニター(monitor-app のデモから作ったプロジェクト)

`monitor-app startproject <名前> --template warehouse` で作ったプロジェクトです。
`monitor-app demo` と同じ画面を、自分の PC の DB で動かします(入出庫の自動発生はしません)。

```sh
monitor-app runserver --import-csv-if-empty     # → http://127.0.0.1:9990
```

## データの形

| ファイル | 中身 | 自分のデータに替えるには |
|---|---|---|
| `csv/items.csv` | 品目マスタ(品番・入数・安全在庫・賞味期間) | WMS / 基幹システムの品目マスタを同じ見出しで出力 |
| `csv/locations.csv` | 棚番マスタ(温度帯・保管ケース数) | 棚番マスタを出力 |
| `csv/lots.csv` | ロット(入荷日・賞味期限) | ロットの一覧を出力 |
| `csv/movements.csv` | 入出庫の台帳(数量は入庫 +・出庫 −) | 入出庫実績を追記していく CSV にする |

在庫は movements の合計として SQL(config.py の views)で求めています。在庫の表を
別に持たないので、入出庫の行を足すだけで在庫一覧・KPI・アラートが追従します。

入出庫実績を CSV の追記で取り込み続けるには、`.env` に次を書きます:

```sh
MONITOR_INGEST_WATCH=true
MONITOR_INGEST_MODE=append
```

生成時の lots.csv・movements.csv は、作った日までの 7 日分の架空のデータです。
`date('now', 'localtime')` を使う集計(本日の出荷など)は、日がたつと 0 になります。
PostgreSQL / MySQL で使うときは、config.py の日付の関数を書き換えてください。
