"""monitor-app の設定ファイル。

ここで `config` という名前の MonitorConfig を組み立てると、アプリがそれを読み込む。
DB 接続情報(パスワード等)はこのファイルに書かず、環境変数 MONITOR_DATABASE_URL
または .env に書く(.env.example を参照)。
書き換えたら `monitor-app check` で誤り(キーの綴り・SQL・列名)を確かめられる。
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
    Retention,
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
    # columns は {"列名": "型"} で型を明示する。型は "int" / "float" / "str" /
    # "bool" / "date" / "datetime"(日時。時刻順の並べ替え・期間の絞り込みが正しく動く)。
    # ["id", "name"] のように列名だけでも書けるが、その場合は列名から型を推論する
    # (id・*_id は int、price・amount を含む列は float、それ以外は str)ため、
    # 数値の列(温度・数量など)は必ず型を明示すること。
    tables={
        "users": TableDef(
            columns={"id": "int", "name": "str", "email": "str"},
            primary_key="id",
        ),
        "products": TableDef(
            columns={"id": "int", "name": "str", "price": "float"},
            primary_key="id",
        ),
        "orders": TableDef(
            columns={
                "id": "int",
                "user_id": "int",
                "product_id": "int",
                "amount": "float",
            },
            primary_key="id",
            foreign_keys={"user_id": "users.id", "product_id": "products.id"},
            # 作業者向け入力フォーム(/form/orders で自動生成)。
            form=FormDef(
                title="注文の入力",
                labels={
                    "user_id": "ユーザーID",
                    "product_id": "商品ID",
                    "amount": "数量",
                },
                submit_label="注文を登録",
            ),
        ),
        # 時系列データ(センサ・PLC の計測値)の例。使うときはコメントを外す。
        # retention で古い行を自動で削除し、表が際限なく大きくなるのを防ぐ。
        # "readings": TableDef(
        #     columns={"id": "int", "ts": "datetime", "line": "str", "temp": "float"},
        #     primary_key="id",
        #     indexes=[["ts"]],  # 時刻で絞り込む・並べ替える列には索引を張る
        #     retention=Retention(keep_days=90, column="ts"),  # 90 日より古い行を削除
        # ),
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
            group="販売",  # サイドバーとビュー一覧を「販売」グループにまとめる例
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
            # x_label / y_label で軸タイトル(単位)を付けると目盛りが読みやすい。
            chart=ChartDef(
                type="bar",
                x="name",
                y="price",
                x_label="商品名",
                y_label="単価 (円)",
                ucl=5000,
                target=1000,
            ),
        ),
        "orders_summary": ViewDef(
            # 行が増え続けるテーブルは、新しい順に件数を絞ると画面が重くならない
            # (アラートもこの範囲=直近の注文で評価する)。
            query=(
                "SELECT o.id, u.name AS user, p.name AS product, o.amount "
                "FROM orders o "
                "JOIN users u ON o.user_id = u.id "
                "JOIN products p ON o.product_id = p.id "
                "ORDER BY o.id DESC LIMIT 500"
            ),
            title="注文サマリー",
            description="ユーザーと商品を JOIN した注文一覧(新しい順に 500 件)",
            styles={
                "amount": CellStyle(
                    greater_than=Threshold(
                        value=10, **{"class": "bg-danger text-white"}
                    ),
                    align="center",
                    bold=True,
                ),
            },
            # max_rows=1000,  # 返す行数の上限(SQL に LIMIT を書かない場合の歯止め)
        ),
        # 時系列データのビューの例(上の readings と合わせて使う)。
        # "readings_view": ViewDef(
        #     query="SELECT ts, line, temp FROM readings ORDER BY ts DESC LIMIT 1000",
        #     title="計測値(新しい順に 1000 件)",
        #     chart=ChartDef(type="line", x="ts", y="temp", y_label="温度 (℃)"),
        # ),
    },
    # ---- 閾値アラート(違反すると画面に赤バナー + 通知)----
    # notify は ["console"]/["webhook"]/["email"](.env で接続情報を設定)。
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
            title="注文件数",
            query="SELECT COUNT(*) FROM orders",
            unit="件",
            link_view="orders_summary",  # クリックで関連ビューへ遷移
        ),
        "total_amount": KpiCard(
            title="合計数量",
            query="SELECT SUM(amount) FROM orders",
            unit="個",
            target=20,
            link_view="orders_summary",
        ),
    },
    # ---- Andon / 大型表示(/kiosk)。ライン大型モニタ向け ----
    kiosk=KioskConfig(rotate_seconds=15, theme="dark"),
    # ---- ダッシュボード(ホームのミニチャートグリッド設定)----
    # views は chart 付きビューのみ指定可。省略時は chart 付き全ビューを表示。
    dashboard=DashboardConfig(
        views=["products_view"],  # chart を持つビューのみ指定すること
        columns=2,  # グリッド列数(1〜4)
    ),
)

# ---- 外部ツール連携(integrations)の例 ----------------------------------------
# procdiag / netdiag / network-checker の出力を宣言だけで可視化できます。
# 以下のコメントを外して必要なツールを有効にしてください。
# 詳細: https://github.com/mikawa-bushi/monitor-app/blob/main/docs/design/integrations_v2_2.md
#
# from monitor_app.integrations import procdiag, netdiag, network_checker
#
# # procdiag: ホストの CPU / メモリ / PSI を SQLite から取り込む
# config = procdiag.attach(config, db_path="/opt/procdiag/data/procdiag.db")
#
# # netdiag: netdiag watch のアラートログ(JSONL)を差分取り込みする
# config = netdiag.attach(config)
#
# # network_checker: ping 結果の SQLite をホスト別に可視化する
# config = network_checker.attach(config, hosts=["plc-01", "gateway"])
