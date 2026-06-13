# T06: integrations/network_checker — ping 監視 DB のプリセット

先に `tasks/00-overview.md` を読むこと。本タスクは Wave 2(T01 完了済み:
`SourceDef`・`integrations.merge` は実装済み。コネクタ(T02)は並列実装中なので
**本タスクのテストでコネクタを使わない**こと — 生成された config の検証と、
手動投入したデータに対するビュー/KPI の動作確認に限定する)。

## 所有ファイル(これ以外は変更禁止)

- `monitor_app/integrations/network_checker.py`(新規)
- `tests/test_integration_netcheck.py`(新規)

## 対象ツールの出力(参照不可のためここに転記。これが真実)

network-checker は SQLite(既定 `network_check_tool.db`)の `ping_results` に蓄積する:

- `id`: INTEGER 主キー(自動採番・単調増加 → watermark に最適)
- `target_host`: VARCHAR(255)、インデックスあり
- `timestamp`: DATETIME(UTC の `"YYYY-MM-DD HH:MM:SS.ffffff"` 形式文字列)
- `response_time`: FLOAT(ミリ秒、失敗時 NULL)
- `packet_loss`: BOOLEAN(0/1。1 = 失敗)
- `error_message`: TEXT(NULL 可)

## 実装する公開 API(凍結。T07 のドキュメントがこの signature を参照する)

```python
def attach(
    config: MonitorConfig,
    *,
    db_path: str = "~/network_check_tool.db",
    hosts: list[str] | None = None,   # ホスト別ビューを作る対象。None なら集約ビューのみ
    prefix: str = "netcheck",
    with_alerts: bool = True,
    loss_threshold_pct: float = 5.0,  # 直近 1h のロス率アラート閾値(%)
) -> MonitorConfig:
    """network-checker の ping 結果可視化一式を合成する。"""
```

実装は `from . import merge` を使う。

## フラグメントの内容

declarative.py の各モデルと `services/kpi_service.py`・`services/alert_service.py` を
読み、実フィールドに合わせること。ビュー SQL は SQLite 方言前提でよい。

- **テーブル** `{prefix}_ping`: id(int 主キー)、target_host(str)、timestamp(str)、
  response_time(float)、packet_loss(bool)、error_message(str)。
  ※ timestamp を date 型にしない(時刻部が落ちるため str で保持)
- **ソース** `{prefix}_ping`: kind=sqlite, source_table="ping_results",
  watermark_column="id", table=`{prefix}_ping`
- **ビュー**:
  - `{prefix}_summary`: ホスト別の直近 24h 集計
    (`timestamp > datetime('now','-1 day')`)— 件数、成功率
    `AVG(CASE WHEN packet_loss THEN 0.0 ELSE 100.0 END) AS success_pct`、
    平均応答 `AVG(response_time) AS avg_ms`、直近 1h ロス率
    `loss_pct_1h`(同様の AVG を `datetime('now','-1 hour')` 窓で。サブクエリ可)。
    success_pct / loss_pct_1h に CellStyle(劣化を赤系)
  - ホスト別ビュー `{prefix}_rt_{sanitized_host}`(hosts 指定時のみ、ホストごと):
    直近 24h の応答時間 line チャート(x=timestamp, y=response_time)。
    ホスト名の埋め込みは `host.replace("'", "''")` でエスケープし、ビュー名は
    `re.sub(r"[^0-9A-Za-z_]", "_", host)` でサニタイズ
- **KPI**: 全体成功率(24h)・全体平均応答 ms(24h)
- **アラート**(with_alerts 時): `{prefix}_summary` の loss_pct_1h >
  loss_threshold_pct を warning レベルで 1 件

## tests/test_integration_netcheck.py

- attach() が返す config の構造検証(watermark_column="id" のソース、
  hosts=["plc-01", "8.8.8.8"] でサニタイズ済みビュー名が 2 つ等)
- hosts=None でホスト別ビューなし、集約系のみ
- 実データ検証: create_app(in-memory SQLite)し、成功/失敗の ping 行
  (timestamp は `datetime('now')` 近傍の文字列を Python 側で生成)を投入 →
  summary ビューの success_pct / loss_pct_1h が期待値 → ホスト別ビューが
  該当ホストの行だけ返す → KPI が数値を返す
- ホスト名に `'` を含むケースで SQL が壊れない(ビューが実行できる)

## 完了条件

```bash
.venv/bin/python -m pytest tests/test_integration_netcheck.py -q
.venv/bin/python -m pytest -q
.venv/bin/black --check monitor_app/integrations/network_checker.py tests/test_integration_netcheck.py
```
