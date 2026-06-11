"""monitor-app の設定ファイル。

ここで `config` という名前の MonitorConfig を組み立てると、アプリがそれを読み込む。
DB 接続情報(パスワード等)はこのファイルに書かず、環境変数 MONITOR_DATABASE_URL
または .env に書く(.env.example を参照)。
"""

from monitor_app import (
    AlertRule,
    CellStyle,
    ChartDef,
    FormDef,
    KioskConfig,
    KpiCard,
    MonitorConfig,
    TableDef,
    Threshold,
    ViewDef,
)

config = MonitorConfig(
    # ---- 画面の見た目 ----
    app_title="Monitor App",
    header_text="📊 Monitor Dashboard",
    footer_text="© 2026 Monitor App",
    refresh_interval_ms=2000,
    refresh_mode="sse",  # "sse"(差分配信)または "polling"
    # ---- CRUD 対象テーブル ----
    # columns は ["id", "name"](命名規則で型推論)でも
    # {"id": "int", "name": "str"}(明示)でも書ける。
    tables={
        "users": TableDef(columns=["id", "name", "email"], primary_key="id"),
        "products": TableDef(columns=["id", "name", "price"], primary_key="id"),
        "orders": TableDef(
            columns=["id", "user_id", "product_id", "amount"],
            primary_key="id",
            foreign_keys={"user_id": "users.id", "product_id": "products.id"},
            # 作業者向け入力フォーム(/form/orders で自動生成)。
            form=FormDef(
                labels={
                    "user_id": "ユーザーID",
                    "product_id": "商品ID",
                    "amount": "数量",
                },
                submit_label="注文を登録",
            ),
        ),
    },
    # ---- 表示用ビュー(生 SQL の SELECT、JOIN や集計が書ける)----
    views={
        "users_view": ViewDef(
            query="SELECT id, name, email FROM users",
            title="ユーザー一覧",
        ),
        "products_view": ViewDef(
            query="SELECT id, name, price FROM products",
            title="商品一覧",
            styles={
                "price": CellStyle(
                    greater_than=Threshold(
                        value=1000, **{"class": "bg-primary text-white"}
                    ),
                    less_than=Threshold(value=500, **{"class": "bg-info text-dark"}),
                    align="right",
                ),
            },
            # グラフ表示(管理限界線つき)。表とグラフの両方が出る。
            chart=ChartDef(type="bar", x="name", y="price", ucl=5000, target=1000),
        ),
        "orders_summary": ViewDef(
            query=(
                "SELECT o.id, u.name AS user, p.name AS product, o.amount "
                "FROM orders o "
                "JOIN users u ON o.user_id = u.id "
                "JOIN products p ON o.product_id = p.id"
            ),
            title="注文サマリー",
            description="ユーザーと商品を JOIN した注文一覧",
            styles={
                "amount": CellStyle(
                    greater_than=Threshold(
                        value=10, **{"class": "bg-danger text-white"}
                    ),
                    align="center",
                    bold=True,
                ),
            },
        ),
    },
    # ---- 閾値アラート(違反すると画面に赤バナー + 通知)----
    # notify は ["console"]/["webhook"]/["line"]/["email"](.env で接続情報を設定)。
    alerts=[
        AlertRule(
            view="orders_summary",
            column="amount",
            op=">",
            value=10,
            level="critical",
            message="注文数量が上限を超えました",
            notify=["console"],
        ),
    ],
    # ---- KPI サマリーカード(ホーム上部に表示。スカラー1値を返す SELECT)----
    kpis={
        "order_count": KpiCard(
            title="注文件数", query="SELECT COUNT(*) FROM orders", unit="件"
        ),
        "total_amount": KpiCard(
            title="合計数量",
            query="SELECT SUM(amount) FROM orders",
            unit="個",
            target=20,
        ),
    },
    # ---- Andon / 大型表示(/kiosk)。ライン大型モニタ向け ----
    kiosk=KioskConfig(rotate_seconds=15, theme="dark"),
)
