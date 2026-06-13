# T05: integrations/netdiag — ネットワーク診断 watch アラートのプリセット

先に `tasks/00-overview.md` を読むこと。本タスクは Wave 2(T01 完了済み:
`SourceDef`・`integrations.merge` は実装済み。コネクタ(T02)は並列実装中なので
**本タスクのテストでコネクタを使わない**こと — 生成された config の検証と、
手動投入したデータに対するビュー/KPI/アラートの動作確認に限定する)。

## 所有ファイル(これ以外は変更禁止)

- `monitor_app/integrations/netdiag.py`(新規)
- `tests/test_integration_netdiag.py`(新規)

## 対象ツールの出力(参照不可のためここに転記。これが真実)

`netdiag watch` が `~/.netdiag/alerts.jsonl` に **1 行 1 JSON** で状態遷移を追記する。
状態が確定変化したときだけ書かれる(degraded/recovered が主)。

```json
{"event": "degraded", "from_state": "healthy", "to_state": "degraded",
 "ts": 1760000000.0, "cause": "dns", "confidence": "high",
 "summary": "DNS 解決が失敗しています",
 "evidence": ["resolv.conf: 192.168.1.1", "query timeout"],
 "consequences": ["http", "tls"],
 "runbook": ["dig @192.168.1.1 example.com", "ルーターの DNS 設定を確認"],
 "duration_s": 86400.0, "iso": "2026-06-12 10:00:00"}
{"event": "recovered", "from_state": "degraded", "to_state": "healthy",
 "ts": 1760003600.0, "cause": null, "confidence": null, "summary": "",
 "evidence": [], "consequences": [], "runbook": [], "duration_s": 3600.0,
 "iso": "2026-06-12 11:00:00"}
```

- `event` は "degraded" | "recovered" | "started" | "stopped"
- list 値(evidence/consequences/runbook)はコネクタの共通規則で
  `"; "` 連結の str になる(00-overview.md 参照)→ テーブル側は str 列でよい

## 実装する公開 API(凍結。T07 のドキュメントがこの signature を参照する)

```python
def attach(
    config: MonitorConfig,
    *,
    alerts_jsonl: str = "~/.netdiag/alerts.jsonl",
    prefix: str = "netdiag",
    with_alerts: bool = True,
) -> MonitorConfig:
    """netdiag watch のアラートログ可視化一式を合成する。"""
```

実装は `from . import merge` を使う。

## フラグメントの内容

declarative.py の各モデルと `services/kpi_service.py`・`services/alert_service.py` を
読み、実フィールドに合わせること。ビュー SQL は SQLite 方言前提でよい。

- **テーブル** `{prefix}_alerts`: id(int 主キー、自動採番)、ts(float)、iso(str)、
  event/from_state/to_state/cause/confidence/summary/evidence/consequences/runbook(str)、
  duration_s(float)。mapping は空(キー名がそのまま列名)でよい — id は JSONL に
  存在しないので自動採番に任せる。
- **ソース** `{prefix}_alerts`: kind=jsonl, path=alerts_jsonl, mode=append
- **ビュー**:
  - `{prefix}_timeline`: 直近 100 件を新しい順に。
    `CASE WHEN event='degraded' THEN 1 ELSE 0 END AS is_degraded` を出し、
    is_degraded に CellStyle で赤系強調。iso/event/cause/summary/duration_s を表示
  - `{prefix}_state`: 最新 1 行(`ORDER BY ts DESC LIMIT 1`)から現在状態を返す。
    is_degraded 列(上と同じ CASE)を含める — アラートルールの監視対象
- **KPI**: 直近 24h の degraded 回数(`ts > strftime('%s','now')-86400`)・
  recovered イベントの平均復旧時間(duration_s の AVG、分換算)
- **アラート**(with_alerts 時): `{prefix}_state` ビューの is_degraded == 1 を
  critical で 1 件(エッジ検出は alert_service 側の仕組みに任せる)

## tests/test_integration_netdiag.py

- attach() が返す config の構造検証(テーブル・jsonl ソース・ビュー・KPI・アラート)
- with_alerts=False でアラートなし
- 実データ検証: create_app(in-memory SQLite)し、degraded/recovered の代表行を
  API/repository 経由で投入 → timeline・state ビューが期待値(is_degraded、並び順)
  → KPI が数値を返す → alert_service のチェックを 1 回走らせ、degraded 状態で
  アラートが発火することを確認(既存 test_features.py のアラート系テストの流儀に倣う)

## 完了条件

```bash
.venv/bin/python -m pytest tests/test_integration_netdiag.py -q
.venv/bin/python -m pytest -q
.venv/bin/black --check monitor_app/integrations/netdiag.py tests/test_integration_netdiag.py
```
