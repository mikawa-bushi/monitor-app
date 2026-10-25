"""v1(0.1.x)形式の config.py の後方互換フィクスチャ。

0.1.x の ``startproject`` が生成した ``config/config.py`` を、読み込み時の副作用
(``os.makedirs`` によるディレクトリ作成)だけ除いて写し、利用者が画面の文言と更新間隔を
書き換えた状態にしたもの(v2 の既定値と異なる値にして、対応付けを検証できるようにする)。
v2 のローダーは ``ALLOWED_TABLES`` / ``VIEW_TABLES`` / ``TABLE_CELL_STYLES`` と画面系の
定数を読み、DB 種別などの v1 専用変数は無視する。内容を書き換えないこと。
"""

import os

# 📌 プロジェクトのルートディレクトリ
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 📌 インスタンスディレクトリ(データベースの保存先)
INSTANCE_DIR = os.path.join(BASE_DIR, "instances")

# 📌 使用するデータベースの種類(sqlite, mysql, postgresql)
DB_TYPE = "sqlite"
SQLALCHEMY_DATABASE_URI = f"sqlite:///{os.path.join(INSTANCE_DIR, 'database.db')}"
SQLALCHEMY_TRACK_MODIFICATIONS = False

# 📌 CRUD操作で許可されたテーブル(データ操作用)
ALLOWED_TABLES = {
    "users": {"columns": ["id", "name", "email"], "primary_key": "id"},
    "products": {"columns": ["id", "name", "price"], "primary_key": "id"},
    "orders": {
        "columns": ["id", "user_id", "product_id", "amount"],
        "primary_key": "id",
        "foreign_keys": {"user_id": "users.id", "product_id": "products.id"},
    },
}

# 📌 ビュー表示設定(画面表示用)
VIEW_TABLES = {
    "users_view": {
        "query": "SELECT id, name, email FROM users",
        "title": "ユーザー一覧",
        "description": "システムに登録されているユーザーの一覧",
    },
    "products_view": {
        "query": "SELECT id, name, price FROM products",
        "title": "商品一覧",
        "description": "システムに登録されている商品の一覧",
    },
    "orders_summary": {
        "query": """
            SELECT
                orders.id,
                users.name AS user_name,
                products.name AS product_name,
                orders.amount
            FROM orders
            JOIN users ON orders.user_id = users.id
            JOIN products ON orders.product_id = products.id
        """,
        "title": "注文サマリー",
        "description": "ユーザー名と商品名を含む注文の詳細一覧",
    },
}

# 📌 ビューセルのスタイル(VIEW_TABLESのみ)
TABLE_CELL_STYLES = {
    "users_view": {
        # ユーザービューには特別なスタイル設定なし(将来的に追加可能)
    },
    "products_view": {
        "price": {
            "greater_than": {"value": 1000, "class": "bg-primary text-white"},
            "less_than": {"value": 500, "class": "bg-info text-dark"},
            "equal_to": {"value": 750, "class": "bg-secondary text-white"},
            "width": "20%",
            "font_size": "16px",
            "align": "right",
            "bold": False,
        }
    },
    "orders_summary": {
        "amount": {
            "greater_than": {"value": 10, "class": "bg-danger text-white"},
            "less_than": {"value": 5, "class": "bg-warning text-dark"},
            "equal_to": {"value": 7, "class": "bg-success text-white"},
            "width": "15%",
            "font_size": "32px",
            "align": "center",
            "bold": True,
        }
    },
}

# 📌 ヘッダーとフッターの設定
APP_TITLE = "第1工場 モニタ"
HEADER_TEXT = "📊 第1工場 ダッシュボード"
FOOTER_TEXT = "© 2025 Monitor App - Powered by Flask & Bootstrap"
FAVICON_PATH = "favicon.ico"

# 📌 テーブルのデータ更新間隔(ミリ秒単位)
TABLE_REFRESH_INTERVAL = 5000
