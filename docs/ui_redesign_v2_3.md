# Monitor App UI 刷新 v2.3 設計ドキュメント

## 1. 刷新の動機

v2.1〜v2.2 のホームページはビュー名のリンク一覧を並べるシンプルな形でした。ビューが増えてくると「どこから見ればよいか」が分かりにくくなり、現場担当者がダッシュボードとして使えるようにする必要がありました。

v2.3 の目標:

- ホームページを **ミニチャートグリッド主体のダッシュボード** に刷新する
- グラフのないビューはリンク一覧として下部に残す
- ダークテーマを正式サポートして視認性を向上する
- サイドバーナビのグループ機能(v2.3 追加)でビューを整理できるようにする

---

## 2. ダッシュボードの構成

ホームページ(`/`)は上から次の順に配置されます。

### 2-1. ステータスバー(`#status-bar`)

```html
<div id="status-bar" class="status-bar" data-refresh-interval="2000"></div>
```

`dashboard.js` が `/api/alerts` を定期取得し、アクティブなアラートの件数と最高レベル(info / warning / critical)をドットとテキストで表示します。アラートがなければ緑ドット+「正常」と表示されます。

### 2-2. KPI カード(`#kpi-grid`)

`config.py` に `kpis` が 1 件以上ある場合のみ描画されます。`kpis.js` が `/api/kpis` から値を取得し、色分けカードを並べます。

### 2-3. ミニチャートグリッド(`#dashboard-grid`)

`chart` を持つビューのミニチャートをカード形式でグリッド表示します。

- グリッドの列数は `DashboardConfig.columns`(既定 3、最大 4)で変更できます
- 表示するビューは `DashboardConfig.views` で明示的に指定できます。省略時は `chart` を持つ全ビューが対象です
- 各カードは `<canvas>` に Chart.js が描画し、カードタイトルをクリックするとビューページ(`/table/<name>`)に遷移します
- `compact: true` オプションで凡例・軸タイトルを省略し、目盛り数を絞ったコンパクト表示になります

### 2-4. ビューリンク一覧

`chart` を持たないビューのリンク一覧を「ビュー一覧」見出しの下にグループ別に表示します。グループがない場合は見出しを省略してリストのみ表示します。

---

## 3. `ViewDef.group` と `DashboardConfig` のリファレンス

### `ViewDef.group`

```python
class ViewDef(BaseModel):
    query: str
    title: str = ""
    description: str = ""
    labels: Dict[str, str] = {}      # 列名 → 表示見出し(単位込み可)
    styles: Dict[str, CellStyle] = {}
    chart: ChartDef | None = None
    group: str = ""  # ナビ・一覧でのセクション名(空は「その他」扱い)
```

| フィールド | 型 | 説明 |
|---|---|---|
| `group` | `str` | サイドバーナビとホームのビュー一覧でのセクション名。空文字(`""`)は「その他」として他のグループの後ろに集約される。全ビューが `group=""` の場合はグループ見出しを表示しない |

設定例:

```python
views={
    "temp_trend": ViewDef(
        query="SELECT ts, temp FROM measurements ORDER BY id",
        title="温度トレンド",
        group="製造ライン1",
        chart=ChartDef(type="line", x="ts", y="temp"),
    ),
    "pressure_trend": ViewDef(
        query="SELECT ts, pressure FROM measurements ORDER BY id",
        title="圧力トレンド",
        group="製造ライン1",
        chart=ChartDef(type="line", x="ts", y="pressure"),
    ),
    "orders_summary": ViewDef(
        query="SELECT id, product, amount FROM orders",
        title="注文サマリー",
        # group 未指定 → 「その他」扱い
    ),
}
```

### `DashboardConfig`

```python
class DashboardConfig(BaseModel):
    views: List[str] = []   # 空なら chart 付き全ビュー
    columns: int = 3        # 1〜4(Field ge=1, le=4)
```

| フィールド | 型 | 規定値 | 説明 |
|---|---|---|---|
| `views` | `List[str]` | `[]` | ダッシュボードに表示するビュー名のリスト。**chart を持つビューのみ指定可**(検証あり)。空のときは chart 付き全ビューを出現順に表示 |
| `columns` | `int` | `3` | グリッドの列数。1〜4 の範囲 |

設定例:

```python
from monitor_app import DashboardConfig, MonitorConfig

config = MonitorConfig(
    ...,
    dashboard=DashboardConfig(
        views=["temp_trend", "pressure_trend"],  # chart 付きビューのみ
        columns=2,
    ),
)
```

`dashboard.views` に chart を持たないビューを指定すると起動時に `ValidationError` が発生します。

---

## 4. サイドバーナビ

`base.html` の左サイドバーに全ビューのナビリンクが表示されます。

### グループ化の動作

`config.py` に記述されたビューの出現順でグループ化します。ルールは次のとおりです:

| 状態 | 表示 |
|---|---|
| 全ビューが `group=""` | グループ見出しなしでリンクを並べる |
| `group` が 1 つ以上ある | グループ名を見出しとして表示。`group=""` のビューは末尾に「その他」として集約 |

### 現在ページの強調

`current_view` コンテキスト変数に一致するビューのリンクに `active` クラスと `aria-current="page"` が付与されます。

### モバイル対応

ビューポート幅が 900px 未満でサイドバーが折りたたまれ、ハンバーガーボタン(`#hamburger`)で開閉できます。

---

## 5. ダークテーマ

### 動作の概要

`static/js/theme.js` が `<head>` 内で同期読み込みされるため、ページ表示と同時にテーマが適用されます(FOUC なし)。

### localStorage キーと `data-theme` 属性

| 項目 | 値 |
|---|---|
| localStorage キー | `"monitor-theme"` |
| 保存される値 | `"dark"` または `"light"` |
| HTML 属性 | `<html data-theme="dark">` |

テーマを適用するとき、`document.documentElement.setAttribute("data-theme", theme)` で `<html>` 要素に属性を付与します。`app.css` は `[data-theme="dark"]` セレクタで `:root` の CSS 変数一式を上書きします。

### フォールバック順

1. `localStorage` に `"dark"` または `"light"` があればそれを使う
2. なければ `prefers-color-scheme` メディアクエリを参照する
3. どちらも該当しなければ `"light"` を使う

### `CustomEvent`

テーマトグル時に次のイベントが発火します:

```js
document.dispatchEvent(new CustomEvent("monitor:themechange", {
    detail: { theme: "dark" | "light" }
}));
```

chart-view.js はこのイベントを listen し、`--chart-grid` / `--chart-tick` CSS 変数を読み直してグリッド・目盛り色を再適用します(`chart.update("none")`)。

### CSS 変数(ライト / ダーク両方で定義)

| 変数名 | ライト既定値 | 用途 |
|---|---|---|
| `--chart-grid` | `rgba(100,116,139,0.18)` | グリッド線の色 |
| `--chart-tick` | `#475569` | 目盛りテキストの色 |

### テーマトグルボタン

`base.html` のヘッダーに配置:

```html
<button id="theme-toggle" class="theme-toggle-btn"
        onclick="window.MonitorTheme.toggle()" aria-label="テーマ切替">🌙</button>
```

現在のテーマに合わせてアイコンが切り替わります: ライト → 🌙、ダーク → ☀️

### 公開 API

```js
window.MonitorTheme.toggle();   // トグル
window.MonitorTheme.current();  // 現在のテーマ文字列を返す
```

---

## 6. ビューページ(`/table/<view>`)

### チャート付きビュー

`ViewDef.chart` が設定されたビューのページ(`table.html`)では、チャートを主役として最上部に表示し、データテーブルは `<details>` 要素で折りたたみます。

```html
<!-- チャートカード(常時表示) -->
<div class="card chart-card">
    <canvas id="chart"></canvas>
</div>

<!-- データテーブル(折りたたみ) -->
<details class="table-collapse">
    <summary>データテーブル <span id="row-count"></span></summary>
    <div class="card">
        <table class="data" id="data-table">...</table>
    </div>
</details>
```

`#row-count` バッジには「全 N 行」のテキストが `app.js` によって更新されます。

### チャートなしビュー

`ViewDef.chart` が `None` のビューは従来どおりテーブルを直接表示します(折りたたみなし)。

### 数値整形規則

`app.js` が API レスポンスのセル値を次のルールで整形します:

| 値の型 | 整形 |
|---|---|
| `number` かつ非整数 | `toLocaleString("ja-JP", { maximumFractionDigits: 2 })` |
| `number` かつ整数 | そのまま文字列化 |
| `string` | そのまま |
| `null` / `undefined` | 空文字列 |

例: `1234.567` → `"1,234.57"`、`42` → `"42"`

### `MonitorChart.create()` API

```js
// 通常
var chart = MonitorChart.create(canvasEl, chartConfig);

// compact モード(ダッシュボードのミニカード用)
var chart = MonitorChart.create(canvasEl, chartConfig, { compact: true });

// データ更新(/api/views/<v> のレスポンスを渡す)
chart.update(payload);
```

`compact: true` 時:

- 凡例を非表示
- 軸タイトルを非表示
- 横軸目盛り上限: 6 本
- 縦軸目盛り上限: 5 本
- `pointRadius: 0`(折れ線の点なし)

### 時系列ラベルの短縮

`chart-view.js` は横軸ラベルが `YYYY-MM-DD HH:MM:SS` 形式の場合、目盛り表示を `HH:MM` に短縮します。ツールチップでは元の完全な文字列を表示します。

### 管理限界線(UCL / LCL / target)

`ChartDef` に `ucl` / `lcl` / `target` を設定すると点線の水平線を追加します。管理限界を外れた点は赤色(`#ef4444`)に強調されます(単一系列のとき)。

---

## 7. スコープ外(見送り事項)

| 項目 | 理由 |
|---|---|
| KPI スパークライン(ダッシュボード上の KPI カードに時系列ミニグラフ) | 履歴 API(`/api/kpis/<name>/history` 相当)が未実装のため v2.3 では見送り |

---

## 8. 関連ドキュメント

- [docs/feature_expansion_v2.1.md](feature_expansion_v2.1.md) — v2.1 フェーズ設計(グラフ・KPI の元設計)
- [docs/integrations_v2_2.md](integrations_v2_2.md) — 外部ツール連携(v2.2)
- [docs/redesign_v2.md](redesign_v2.md) — v2 の再設計ドキュメント
