# T04: integrations/procdiag — ホストリソース診断ツールのプリセット

先に `tasks/00-overview.md` を読むこと。本タスクは Wave 2(T01 完了済み:
`SourceDef`・`integrations.merge` は実装済み。コネクタ(T02)は並列実装中なので
**本タスクのテストでコネクタを使わない**こと — 生成された config の検証と、
手動投入したデータに対するビュー/KPI の動作確認に限定する)。

## 所有ファイル(これ以外は変更禁止)

- `monitor_app/integrations/procdiag.py`(新規)
- `tests/test_integration_procdiag.py`(新規)

## 対象ツールの出力(参照不可のためここに転記。これが真実)

### SQLite `procdiag.db` の `sys_rollup_1m` テーブル(1 分ロールアップ時系列)

```sql
CREATE TABLE sys_rollup_1m (
  ts_min REAL PRIMARY KEY,          -- epoch 秒(分単位)
  cpu_avg REAL, cpu_max REAL, cpu_p95 REAL,
  cpu_max_core_max REAL,
  mem_used_avg INTEGER, mem_used_max INTEGER,   -- バイト
  psi_cpu_max REAL, psi_mem_max REAL, psi_io_max REAL,
  swap_out_sum INTEGER
);
```

### findings JSON(`procdiag check --format json` の出力ファイル。毎回再生成される)

```json
{
  "findings": [
    {
      "rule_id": "MEM-LEAK-01",
      "severity": "critical",          // "critical" | "warning" | "info"
      "subject": "myapp.service",
      "summary": "RSS が単調増加しています(リーク疑い)",
      "evidence": {"slope_mb_h": 12.5},
      "detected_at": [1760000000.0, 1760086400.0],   // [start_ts, end_ts]
      "confidence": "high",            // "high" | "med" | "low"
      "related": [],
      "proposal": "対処の提案文(複数行)"
    }
  ],
  "quality": {"...": "無視してよい"}
}
```

## 実装する公開 API(凍結。T07 のドキュメントがこの signature を参照する)

```python
def attach(
    config: MonitorConfig,
    *,
    db_path: str,                      # procdiag.db のパス
    findings_json: str | None = None,  # findings JSON のパス(None なら所見系を省略)
    prefix: str = "procdiag",
    with_alerts: bool = True,
) -> MonitorConfig:
    """procdiag の可視化一式(テーブル・ソース・ビュー・KPI・アラート)を合成する。"""
```

実装は `from . import merge` を使い、フラグメントを組み立てて 1 回の merge で返す。

## フラグメントの内容(列名・型は上記スキーマに正確に合わせる)

declarative.py の各モデル(ViewDef/ChartDef/KpiCard/AlertRule/CellStyle/Threshold)と
`services/kpi_service.py`・`services/alert_service.py` を読み、実際のフィールド名・
評価方法に合わせて書くこと。ビュー SQL は SQLite 方言前提でよい
(monitor-app の既定 DB。この前提は T07 がドキュメントに明記する)。

- **テーブル** `{prefix}_sys`: sys_rollup_1m と同じ列(ts_min を主キー、型は
  float/int を上記 DDL に対応させる)
- **テーブル** `{prefix}_findings`: `id`(int 主キー、自動採番に任せるため mapping
  には含めない)+ rule_id/severity/subject/summary/confidence/proposal(str)、
  start_ts/end_ts(float)
- **ソース** `{prefix}_sys`: kind=sqlite, source_table="sys_rollup_1m",
  watermark_column="ts_min", table=`{prefix}_sys`
- **ソース** `{prefix}_findings`(findings_json 指定時のみ): kind=json,
  record_path="findings", mode="replace",
  mapping は `{"rule_id": "rule_id", ..., "detected_at.0": "start_ts",
  "detected_at.1": "end_ts"}` のように明示
- **ビュー**(時刻は `datetime(ts_min, 'unixepoch', 'localtime')` で文字列化):
  - `{prefix}_cpu`: 直近 24h の CPU line チャート(cpu_avg / cpu_max)
  - `{prefix}_memory`: メモリ使用量 MB 換算 line チャート(mem_used_avg / mem_used_max)
  - `{prefix}_psi`: PSI line チャート(psi_cpu_max / psi_mem_max / psi_io_max)
  - `{prefix}_findings`(findings_json 指定時): 所見一覧。SQL で
    `CASE severity WHEN 'critical' THEN 2 WHEN 'warning' THEN 1 ELSE 0 END AS sev_rank`
    を出し、sev_rank に CellStyle(>=2 相当を赤系、>=1 を黄系。Threshold の意味論は
    declarative.py を読んで合わせる)
- **KPI**: 直近の cpu_avg(%)・直近の mem_used_avg(MB)・critical 所見件数
  (findings_json 指定時のみ)
- **アラート**(with_alerts かつ findings_json 指定時): `{prefix}_findings` ビューの
  sev_rank >= 2 で critical レベル 1 件

直近 24h の絞り込みは `WHERE ts_min > strftime('%s','now') - 86400` を使う。

## tests/test_integration_procdiag.py

- attach() が返す config が検証済みで、期待したテーブル/ソース/ビュー/KPI/アラートを
  含む(名前・ソースの watermark 設定などを assert)
- findings_json=None でも有効な config(所見系が無い)
- 既存 config のテーブルと衝突しない(prefix 変更で共存できる)
- 実データ検証: attach 済み config で `create_app`(in-memory SQLite)し、
  `{prefix}_sys` / `{prefix}_findings` に代表行を **CRUD/ingest API または repository
  経由で**直接投入 → 各ビューの API レスポンスが返り、sev_rank・チャート列が期待どおり
  → KPI API が数値を返す(kpi_service の流儀は既存テスト test_features.py を参考に)

## 完了条件

```bash
.venv/bin/python -m pytest tests/test_integration_procdiag.py -q
.venv/bin/python -m pytest -q
.venv/bin/black --check monitor_app/integrations/procdiag.py tests/test_integration_procdiag.py
```
