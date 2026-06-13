# 外部ツール連携ガイド (v2.2)

本ドキュメントは Monitor App v2.2 で追加された **外部データソース取り込み機能** と
**ツール別プリセット** について説明します。

## 目的

製造現場では procdiag(ホストリソース診断)・netdiag(ネットワーク状態診断)・
network-checker(ping 監視)などのツールが各機器上で独立して動作しています。
v2.2 の integrations 機能は、これらツールが書き出すファイル(SQLite / JSONL / JSON)
を **Monitor App の `config.py` に宣言を追加するだけ** で可視化する仕組みです。
ツール側は無改造で済み、Monitor App は読み取り専用(pull 型)でアクセスします。

---

## アーキテクチャ — データフロー

```
[外部ツール]                 [Monitor App]
  procdiag.db  ─────────┐
  alerts.jsonl ─────────┼─► SourceConnector.poll_once()
  network_check_tool.db ┘         │
                                  │ レコード変換(mapping / 平坦化 / 型変換)
                                  ▼
                          monitor-app 側 DB
                          (取り込み先テーブル)
                                  │
                        _ingest_state テーブル
                        (カーソル永続化)
                                  │
                     IngestWatcher(常時) or
                     sync-sources CLI(手動/cron)
                                  │
                                  ▼
                          ビュー / KPI / アラート
                          → ダッシュボード表示
```

### コンポーネント詳細

| コンポーネント | 実装場所 | 役割 |
|---|---|---|
| `SourceDef` | `monitor_app/settings/declarative.py` | 宣言的ソース定義(Pydantic) |
| `SourceConnector` 系 | `monitor_app/services/connectors.py` | 実際の読み取り・変換・挿入 |
| `IngestStateStore` | `monitor_app/db/ingest_state.py` | カーソル永続化テーブル |
| `IngestWatcher` | `monitor_app/services/ingest_watcher.py` | ループ・ポーリング管理 |
| `merge()` | `monitor_app/integrations/__init__.py` | config フラグメント合成 |
| プリセット | `monitor_app/integrations/{procdiag,netdiag,network_checker}.py` | ツール別定義生成 |

### カーソル永続化(`_ingest_state`)

取り込みの再開ポイントは monitor-app 側 DB の `_ingest_state` テーブルで管理されます。

```
テーブル: _ingest_state(source TEXT 主キー, cursor TEXT, updated_at TEXT)
```

カーソルの中身はコネクタの種類によって異なります:

| コネクタ | cursor の内容 |
|---|---|
| sqlite (watermark あり) | watermark 列の最大値 |
| sqlite (watermark なし) | `{mtime_ns}:{size}` (ファイル変化検出) |
| jsonl | バイトオフセット(末尾追記を差分読み) |
| json | `{mtime_ns}:{size}` (変化なしはスキップ) |

---

## `SourceDef` フィールドリファレンス

`monitor_app/settings/declarative.py` の `SourceDef` クラスで定義されています。

### 共通フィールド

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `kind` | `"sqlite" \| "jsonl" \| "json"` | ○ | ソース種別 |
| `path` | `str` | ○ | ファイルパス。`~` はコネクタ側で展開 |
| `table` | `str` | ○ | 取り込み先テーブル名(`tables` に存在必須) |
| `mapping` | `dict[str, str]` | - | `{dest_col: src_key_dotted}` ドット記法マッピング |
| `mode` | `"append" \| "replace"` | - | 既定 `"append"` |

### kind 別の必須/不可の組み合わせ

| フィールド | sqlite | jsonl | json |
|---|---|---|---|
| `source_table` | `query` 省略時は必須 | 不可(無視) | 不可(無視) |
| `query` | `source_table` 省略時は必須 | 不可 | 不可 |
| `watermark_column` | 任意 | **禁止** | **禁止** |
| `record_path` | 不可 | 不可 | 任意 |
| `mode` | `"append"` / `"replace"` | `"append"` 固定 | `"append"` / `"replace"` |

> **注意**: `watermark_column` を指定する場合は `mode="append"` が必須です。

---

## レコード変換規則

### mapping のドット記法

`mapping = {"dest_col": "parent.child.0"}` のように書くと、
ソース側の `{"parent": {"child": ["val"]}}` から `"val"` を取り出せます。
数値トークンはリストインデックスとして解釈されます。

### list / dict の平坦化

| ソース値の型 | 変換後 |
|---|---|
| `list` | 空白区切り文字列 (`" ".join(str(v) for v in value)`) |
| `dict` | JSON 文字列 (`json.dumps(value)`) |
| その他 | そのまま |

### 型変換とスキップ

変換後の値は取り込み先テーブルの `ColumnType`(`int` / `float` / `str` / `bool` / `date`)
へ強制変換されます。変換に失敗した行は warning ログに記録されてスキップされます
(ループは継続)。

`mapping` が空の場合、ソース側のキーをそのまま列名として使います。
`tables` に存在しない列は黙って破棄されます。

---

## プリセットの使い方

### 共通パターン

```python
from monitor_app.integrations import procdiag, netdiag, network_checker

# 各 attach() は既存 config を受け取り、新しい MonitorConfig を返す(非破壊)
config = procdiag.attach(config, db_path="/opt/procdiag/data/procdiag.db")
config = netdiag.attach(config)
config = network_checker.attach(config, hosts=["plc-01", "gateway"])
```

---

### procdiag プリセット

`monitor_app/integrations/procdiag.py`

#### シグネチャ

```python
def attach(
    config: MonitorConfig,
    *,
    db_path: str,                    # procdiag.db のパス(必須)
    findings_json: str | None = None,  # findings JSON のパス(None なら所見系を省略)
    prefix: str = "procdiag",
    with_alerts: bool = True,
) -> MonitorConfig:
```

#### 生成される定義一覧

**テーブル**

| テーブル名 | ソース | 条件 |
|---|---|---|
| `{prefix}_sys` | SQLite `sys_rollup_1m` テーブル、watermark=`ts_min` | 常時 |
| `{prefix}_findings` | JSON `findings_json`、`findings` キー配下、mode=replace | `findings_json` 指定時 |

**ビュー**

| ビュー名 | 内容 | 条件 |
|---|---|---|
| `{prefix}_cpu` | CPU 使用率トレンド(直近 24h) | 常時 |
| `{prefix}_memory` | メモリ使用量 MB(直近 24h) | 常時 |
| `{prefix}_psi` | PSI(cpu/mem/io、直近 24h) | 常時 |
| `{prefix}_findings` | 診断所見一覧(severity 順) | `findings_json` 指定時 |

**KPI**

| KPI 名 | 内容 | 条件 |
|---|---|---|
| `{prefix}_cpu_avg` | 直近 CPU 使用率(%) | 常時 |
| `{prefix}_mem_used_avg` | 直近メモリ使用量(MB) | 常時 |
| `{prefix}_critical_count` | critical 所見件数 | `findings_json` 指定時 |

**アラート**

| 条件 | レベル | 備考 |
|---|---|---|
| `{prefix}_findings.sev_rank >= 2` | critical | `with_alerts=True` かつ `findings_json` 指定時 |

#### 使用例

```python
# findings JSON なし (CPU/メモリ/PSI のみ)
config = procdiag.attach(config, db_path="/opt/procdiag/data/procdiag.db")

# findings JSON あり (所見・KPI・アラートも追加)
config = procdiag.attach(
    config,
    db_path="/opt/procdiag/data/procdiag.db",
    findings_json="/opt/procdiag/data/latest_findings.json",
)
```

---

### netdiag プリセット

`monitor_app/integrations/netdiag.py`

#### シグネチャ

```python
def attach(
    config: MonitorConfig,
    *,
    alerts_jsonl: str = "~/.netdiag/alerts.jsonl",
    prefix: str = "netdiag",
    with_alerts: bool = True,
) -> MonitorConfig:
```

#### 生成される定義一覧

**テーブル**

| テーブル名 | ソース | 備考 |
|---|---|---|
| `{prefix}_alerts` | JSONL、mode=append | `netdiag watch` の出力を差分追記 |

**ビュー**

| ビュー名 | 内容 |
|---|---|
| `{prefix}_timeline` | 直近 100 件のアラートイベント(新しい順) |
| `{prefix}_state` | 最新 1 件(現在の状態) |

**KPI**

| KPI 名 | 内容 |
|---|---|
| `{prefix}_degraded_24h` | 直近 24h の障害回数 |
| `{prefix}_avg_recovery_min` | 平均復旧時間(分) |

**アラート**

| 条件 | レベル | 備考 |
|---|---|---|
| `{prefix}_state.is_degraded == 1` | critical | `with_alerts=True` 時 |

#### 使用例

```python
# デフォルト(~/.netdiag/alerts.jsonl)
config = netdiag.attach(config)

# パス指定
config = netdiag.attach(config, alerts_jsonl="/var/log/netdiag/alerts.jsonl")
```

---

### network_checker プリセット

`monitor_app/integrations/network_checker.py`

#### シグネチャ

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
```

#### 生成される定義一覧

**テーブル**

| テーブル名 | ソース | 備考 |
|---|---|---|
| `{prefix}_ping` | SQLite `ping_results` テーブル、watermark=`id` | 増分取り込み |

**ビュー**

| ビュー名 | 内容 | 条件 |
|---|---|---|
| `{prefix}_summary` | ホスト別サマリー(成功率・平均 ms・直近 1h ロス率、直近 24h) | 常時 |
| `{prefix}_rt_{sanitized_host}` | ホスト別応答時間トレンド(折れ線チャート、直近 24h) | `hosts` 指定時 |

**KPI**

| KPI 名 | 内容 |
|---|---|
| `{prefix}_success_pct_24h` | 全体成功率(24h、%) |
| `{prefix}_avg_ms_24h` | 全体平均応答時間(24h、ms) |

**アラート**

| 条件 | レベル | 備考 |
|---|---|---|
| `{prefix}_summary.loss_pct_1h > loss_threshold_pct` | warning | `with_alerts=True` 時 |

#### 使用例

```python
# ホスト別ビューあり
config = network_checker.attach(
    config,
    db_path="/opt/network_check_tool.db",
    hosts=["plc-01", "gateway", "hmi-server"],
    loss_threshold_pct=10.0,
)
```

---

## 運用ガイド

### 常時取り込み (`MONITOR_INGEST_WATCH=1`)

```sh
MONITOR_INGEST_WATCH=1 monitor-app runserver
```

`IngestWatcher` がバックグラウンドで全ソースを定期 poll します。
既存の CSV 監視(`watchdog`)と並列動作します。

### 手動/cron 同期 (`monitor-app sync-sources`)

T03 で実装される CLI コマンドです。

```sh
# 全ソースを一括同期
monitor-app sync-sources

# cron で 1 分ごとに実行
* * * * * cd /project && monitor-app sync-sources
```

サーバーを起動せず一回だけ取り込んで終了するため、cron や CI パイプラインに
組み込むのに適しています。

### ツール側は無改造

Monitor App はファイルを読み取り専用(`file:path?mode=ro`)でオープンします。
外部ツール(procdiag / netdiag / network-checker)の動作を変える必要はありません。

---

## 制約

- **ビュー SQL は SQLite 方言前提**: プリセットが生成するビューは
  `datetime('now', '-1 day')` や `strftime('%s','now')` など SQLite 固有の関数を使います。
  MySQL / PostgreSQL の monitor-app DB で使う場合は、プリセットを使わず
  ビューを自前定義してください。

- **`source_table` / `watermark_column` は識別子のみ**: 正規表現
  `^[A-Za-z_][A-Za-z0-9_]*$` に合致しない値は Pydantic の ValidationError になります。

- **JSONL の `mode` は `"append"` 固定**: `mode="replace"` を指定すると
  ValidationError になります。

- **`watermark_column` と `mode="replace"` の併用禁止**:
  `watermark_column` がある場合は必ず `mode="append"` を使ってください。

---

## 関連ドキュメント

- [再設計ドキュメント (v2)](redesign_v2.md)
- [機能拡張設計 (v2.1)](feature_expansion_v2.1.md)
