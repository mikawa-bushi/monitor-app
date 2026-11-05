"""デモ: 物流センターの在庫画面(monitor-app demo / startproject --template warehouse)。

在庫は「入出庫の台帳(movements)の合計」として SQL で求める。入荷・出荷・移動・棚卸の
1 行を movements に足すだけで、在庫一覧・品目別在庫・KPI・アラートがすべて追従する
(在庫の表を別に持って同期させる必要がない)。

- items / locations … マスタ(csv/items.csv・csv/locations.csv)
- lots              … ロット(入荷ごとに 1 件。賞味期限を持つ)
- movements         … 入出庫の台帳。数量は符号つき(入庫 +、出庫 −)

日付の計算は SQLite の関数(date('now', 'localtime') など)で書いている。
PostgreSQL / MySQL で使うときは CURRENT_DATE などに書き換えること。
"""

from monitor_app import (
    AlertRule,
    CellStyle,
    ChartDef,
    DashboardConfig,
    FormDef,
    KioskConfig,
    KpiCard,
    MonitorConfig,
    TableDef,
    Threshold,
    ViewDef,
)

# 在庫(ロット × ロケーションごとの台帳の合計)。複数のビューで使う共通の FROM 句。
STOCK = """
    FROM movements m
    JOIN lots lt ON lt.lot_no = m.lot_no
    JOIN items i ON i.sku = lt.sku
    JOIN locations l ON l.code = m.location
"""

config = MonitorConfig(
    app_title="物流センター 在庫モニター(デモ)",
    header_text="📦 第1物流センター 在庫モニター",
    footer_text="monitor-app デモ ― データは架空です",
    refresh_interval_ms=2000,
    refresh_mode="sse",
    # ---- テーブル -------------------------------------------------------------
    tables={
        "items": TableDef(
            columns={
                "sku": "str",
                "name": "str",
                "category": "str",
                "temp_zone": "str",  # 常温 / 冷蔵 / 冷凍
                "case_qty": "int",  # 入数(1 ケースあたりの個数)
                "safety_stock": "int",  # 安全在庫(個)
                "shelf_life_days": "int",  # 賞味期間(日)。期限の無い品目は空欄
            },
            primary_key="sku",
        ),
        "locations": TableDef(
            columns={
                "code": "str",  # 棚番(例: A-01-03 = A 列 01 番 3 段)
                "zone": "str",  # 常温 / 冷蔵 / 冷凍
                "capacity_cases": "int",  # 保管できるケース数
            },
            primary_key="code",
        ),
        "lots": TableDef(
            columns={
                "lot_no": "str",
                "sku": "str",
                "received_on": "date",
                "expiry_date": "date",
            },
            primary_key="lot_no",
            foreign_keys={"sku": "items.sku"},
        ),
        "movements": TableDef(
            columns={
                "id": "int",
                "ts": "datetime",
                "kind": "str",  # 入荷 / 出荷 / 移動 / 棚卸調整 / 廃棄 / 繰越(締め)
                "lot_no": "str",
                "location": "str",
                "qty": "int",  # 符号つき: 入庫は +、出庫は −
                "partner": "str",  # 仕入先・出荷先
                "ref_no": "str",  # 伝票番号
                "operator": "str",
            },
            primary_key="id",
            foreign_keys={"lot_no": "lots.lot_no", "location": "locations.code"},
            indexes=[["ts"]],
            # ハンディ端末の代わりの入力フォーム(/form/movements)。
            form=FormDef(
                fields=["ts", "kind", "lot_no", "location", "qty", "operator"],
                labels={
                    "ts": "日時",
                    "kind": "区分",
                    "lot_no": "ロット番号(在庫一覧を参照)",
                    "location": "棚番",
                    "qty": "数量(入庫は +、出庫は −)",
                    "operator": "作業者",
                },
                choices={"kind": ["入荷", "出荷", "移動", "棚卸調整", "廃棄"]},
                submit_label="入出庫を登録",
            ),
        ),
    },
    # ---- ビュー ---------------------------------------------------------------
    views={
        "stock_list": ViewDef(
            title="在庫一覧",
            group="在庫",
            description="棚番 × ロットごとの現在庫(台帳の合計)。期限の近い順",
            query=f"""
                SELECT m.location, l.zone, i.sku, i.name, m.lot_no, lt.expiry_date,
                       CAST(julianday(lt.expiry_date)
                            - julianday('now', 'localtime', 'start of day') AS INTEGER)
                           AS days_left,
                       SUM(m.qty) / i.case_qty AS cases,
                       SUM(m.qty) % i.case_qty AS loose,
                       SUM(m.qty) AS qty
                {STOCK}
                GROUP BY m.location, m.lot_no
                HAVING SUM(m.qty) > 0
                ORDER BY l.zone, m.location, lt.expiry_date
            """,
            labels={
                "location": "棚番",
                "zone": "温度帯",
                "sku": "品番",
                "name": "品名",
                "lot_no": "ロット",
                "expiry_date": "賞味期限",
                "days_left": "残日数",
                "cases": "ケース",
                "loose": "バラ",
                "qty": "在庫数(個)",
            },
            styles={
                "days_left": CellStyle(
                    less_than=Threshold(value=3, css_class="bg-danger text-white"),
                    align="right",
                ),
                "cases": CellStyle(align="right"),
                "loose": CellStyle(align="right"),
                "qty": CellStyle(align="right", bold=True),
            },
        ),
        "item_stock": ViewDef(
            title="品目別在庫と安全在庫",
            group="在庫",
            description="品目ごとの在庫合計。充足率 100% 未満は発注が必要",
            query="""
                SELECT i.sku, i.name, i.category, i.temp_zone,
                       COALESCE(SUM(m.qty), 0) AS qty,
                       i.safety_stock,
                       CAST(ROUND(100.0 * COALESCE(SUM(m.qty), 0) / i.safety_stock)
                            AS INTEGER) AS cover_pct
                FROM items i
                LEFT JOIN lots lt ON lt.sku = i.sku
                LEFT JOIN movements m ON m.lot_no = lt.lot_no
                GROUP BY i.sku
                ORDER BY cover_pct
            """,
            labels={
                "sku": "品番",
                "name": "品名",
                "category": "カテゴリ",
                "temp_zone": "温度帯",
                "qty": "在庫数(個)",
                "safety_stock": "安全在庫(個)",
                "cover_pct": "充足率(%)",
            },
            styles={
                "cover_pct": CellStyle(
                    less_than=Threshold(value=100, css_class="bg-danger text-white"),
                    greater_than=Threshold(value=300, css_class="bg-info text-dark"),
                    align="right",
                    bold=True,
                ),
                "qty": CellStyle(align="right"),
                "safety_stock": CellStyle(align="right"),
            },
            chart=ChartDef(
                type="bar",
                x="name",
                y=["qty", "safety_stock"],
                x_label="品目",
                y_label="個",
            ),
        ),
        "expiring": ViewDef(
            title="賞味期限が近いロット",
            group="在庫",
            description="残り 14 日以内のロット(先入れ先出しの確認用)",
            query=f"""
                SELECT lt.lot_no, i.name, lt.expiry_date,
                       CAST(julianday(lt.expiry_date)
                            - julianday('now', 'localtime', 'start of day') AS INTEGER)
                           AS days_left,
                       SUM(m.qty) AS qty
                {STOCK}
                WHERE lt.expiry_date <= date('now', 'localtime', '+14 days')
                GROUP BY lt.lot_no
                HAVING SUM(m.qty) > 0
                ORDER BY lt.expiry_date
            """,
            labels={
                "lot_no": "ロット",
                "name": "品名",
                "expiry_date": "賞味期限",
                "days_left": "残日数",
                "qty": "在庫数(個)",
            },
            styles={
                "days_left": CellStyle(
                    less_than=Threshold(value=3, css_class="bg-danger text-white"),
                    align="right",
                    bold=True,
                ),
            },
        ),
        "location_usage": ViewDef(
            title="棚列別の保管率",
            group="倉庫",
            description="棚列ごとの使用ケース数 ÷ 保管できるケース数",
            query="""
                SELECT substr(l.code, 1, 4) AS aisle, l.zone,
                       CAST(ROUND(100.0 * SUM(COALESCE(s.cases, 0))
                                  / SUM(l.capacity_cases)) AS INTEGER) AS usage_pct
                FROM locations l
                LEFT JOIN (
                    SELECT m.location, SUM(1.0 * m.qty / i.case_qty) AS cases
                    FROM movements m
                    JOIN lots lt ON lt.lot_no = m.lot_no
                    JOIN items i ON i.sku = lt.sku
                    GROUP BY m.location
                ) s ON s.location = l.code
                GROUP BY aisle
                ORDER BY aisle
            """,
            labels={"aisle": "棚列", "zone": "温度帯", "usage_pct": "保管率(%)"},
            styles={
                "usage_pct": CellStyle(
                    greater_than=Threshold(value=90, css_class="bg-warning text-dark"),
                    align="right",
                ),
            },
            chart=ChartDef(
                type="bar",
                x="aisle",
                y="usage_pct",
                x_label="棚列",
                y_label="保管率 (%)",
                ucl=90,
                target=75,
            ),
        ),
        "hourly_today": ViewDef(
            title="本日の時間帯別 出荷・入荷",
            group="入出荷",
            description="今日 0 時からの 1 時間ごとの数量(個)",
            query="""
                SELECT strftime('%H:00', ts) AS hour,
                       COALESCE(-SUM(CASE WHEN kind = '出荷' THEN qty END), 0)
                           AS shipped,
                       COALESCE(SUM(CASE WHEN kind = '入荷' THEN qty END), 0)
                           AS received
                FROM movements
                WHERE ts >= date('now', 'localtime') AND kind IN ('出荷', '入荷')
                GROUP BY hour
                ORDER BY hour
            """,
            labels={"hour": "時刻", "shipped": "出荷(個)", "received": "入荷(個)"},
            chart=ChartDef(
                type="line",
                x="hour",
                y=["shipped", "received"],
                x_label="時刻",
                y_label="個",
            ),
        ),
        "daily_flow": ViewDef(
            title="日別 入荷・出荷(直近 7 日)",
            group="入出荷",
            query="""
                SELECT date(ts) AS day,
                       COALESCE(SUM(CASE WHEN kind = '入荷' THEN qty END), 0)
                           AS received,
                       COALESCE(-SUM(CASE WHEN kind = '出荷' THEN qty END), 0)
                           AS shipped,
                       COUNT(CASE WHEN kind = '出荷' THEN 1 END) AS ship_lines
                FROM movements
                WHERE ts >= date('now', 'localtime', '-6 days')
                  AND kind IN ('入荷', '出荷')
                GROUP BY day
                ORDER BY day
            """,
            labels={
                "day": "日付",
                "received": "入荷(個)",
                "shipped": "出荷(個)",
                "ship_lines": "出荷明細数",
            },
            chart=ChartDef(
                type="bar",
                x="day",
                y=["received", "shipped"],
                x_label="日付",
                y_label="個",
            ),
        ),
        "recent_moves": ViewDef(
            title="入出庫履歴",
            group="入出荷",
            description="新しい順に 300 件。フォームや API で登録した行もここに出る",
            query="""
                SELECT m.ts, m.kind, m.ref_no, m.partner, i.name, m.lot_no,
                       m.location, m.qty, m.operator
                FROM movements m
                JOIN lots lt ON lt.lot_no = m.lot_no
                JOIN items i ON i.sku = lt.sku
                WHERE m.kind <> '繰越'
                ORDER BY m.ts DESC, m.id DESC
                LIMIT 300
            """,
            labels={
                "ts": "日時",
                "kind": "区分",
                "ref_no": "伝票番号",
                "partner": "取引先",
                "name": "品名",
                "lot_no": "ロット",
                "location": "棚番",
                "qty": "数量(個)",
                "operator": "作業者",
            },
            styles={
                "qty": CellStyle(
                    less_than=Threshold(value=0, css_class="text-danger"),
                    greater_than=Threshold(value=0, css_class="text-primary"),
                    align="right",
                    bold=True,
                ),
            },
        ),
    },
    # ---- アラート(画面上部のバナー)--------------------------------------------
    # notify=["email"] や ["webhook"] を足すと通知も送れる(接続先は .env で設定)。
    alerts=[
        AlertRule(
            view="item_stock",
            column="cover_pct",
            op="<",
            value=100,
            level="warning",
            message="安全在庫を下回っている品目があります",
        ),
        AlertRule(
            view="expiring",
            column="days_left",
            op="<",
            value=3,
            level="critical",
            message="賞味期限まで 2 日以内のロットが残っています",
        ),
    ],
    # ---- KPI カード(ホーム上部)----------------------------------------------
    kpis={
        "shipped_today": KpiCard(
            title="本日の出荷",
            query="""
                SELECT COALESCE(-SUM(qty), 0) FROM movements
                WHERE kind = '出荷' AND ts >= date('now', 'localtime')
            """,
            unit="個",
            format="{:,.0f}",
            link_view="hourly_today",
        ),
        "received_today": KpiCard(
            title="本日の入荷",
            query="""
                SELECT COALESCE(SUM(qty), 0) FROM movements
                WHERE kind = '入荷' AND ts >= date('now', 'localtime')
            """,
            unit="個",
            format="{:,.0f}",
            link_view="hourly_today",
        ),
        "below_safety": KpiCard(
            title="安全在庫割れ",
            query="""
                SELECT COUNT(*) FROM (
                    SELECT i.sku FROM items i
                    LEFT JOIN lots lt ON lt.sku = i.sku
                    LEFT JOIN movements m ON m.lot_no = lt.lot_no
                    GROUP BY i.sku
                    HAVING COALESCE(SUM(m.qty), 0) < i.safety_stock
                )
            """,
            unit="品目",
            target=0,
            higher_is_better=False,
            link_view="item_stock",
        ),
        "expiring_lots": KpiCard(
            title="期限 14 日以内のロット",
            query=f"""
                SELECT COUNT(*) FROM (
                    SELECT lt.lot_no {STOCK}
                    WHERE lt.expiry_date <= date('now', 'localtime', '+14 days')
                    GROUP BY lt.lot_no HAVING SUM(m.qty) > 0
                )
            """,
            unit="ロット",
            higher_is_better=False,
            link_view="expiring",
        ),
        "usage": KpiCard(
            title="保管率",
            query="""
                SELECT 100.0 * SUM(1.0 * m.qty / i.case_qty)
                       / (SELECT SUM(capacity_cases) FROM locations)
                FROM movements m
                JOIN lots lt ON lt.lot_no = m.lot_no
                JOIN items i ON i.sku = lt.sku
            """,
            unit="%",
            target=75,
            format="{:.1f}",
            link_view="location_usage",
        ),
    },
    # ---- 大型モニター(/kiosk)-------------------------------------------------
    kiosk=KioskConfig(
        views=["hourly_today", "item_stock", "expiring", "location_usage"],
        rotate_seconds=15,
        theme="dark",
    ),
    # ---- ホームのグラフ -------------------------------------------------------
    dashboard=DashboardConfig(
        views=["hourly_today", "daily_flow", "item_stock", "location_usage"],
        columns=2,
    ),
)
