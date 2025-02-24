import os
from dotenv import load_dotenv

# 環境変数をロード
load_dotenv()

# プロジェクトのルートディレクトリ
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# インスタンスディレクトリ（データベースの保存先）
INSTANCE_DIR = os.path.join(BASE_DIR, "instances")
os.makedirs(INSTANCE_DIR, exist_ok=True)

# SQLite データベースのパス
DB_PATH = os.path.join(INSTANCE_DIR, "database.db")

# 環境変数
SQLALCHEMY_DATABASE_URI = os.getenv("DATABASE_URL", f"sqlite:///{DB_PATH}")
SQLALCHEMY_TRACK_MODIFICATIONS = False

# 📌 許可されたテーブルのみを表示
ALLOWED_TABLES = {
    "users": {"columns": ["id", "name", "email"], "primary_key": "id"},
    "products": {"columns": ["id", "name", "price"], "primary_key": "id"},
    "orders": {
        "columns": ["id", "user_id", "product_id", "amount"],
        "primary_key": "id",
        "foreign_keys": {"user_id": "users.id", "product_id": "products.id"},
        "join": """
            SELECT orders.id, users.name AS user_name, products.name AS product_name, orders.amount
            FROM orders
            JOIN users ON orders.user_id = users.id
            JOIN products ON orders.product_id = products.id
        """,
    },
}

# 📌 ヘッダーとフッターの設定
APP_TITLE = "Monitor App"
HEADER_TEXT = "📊 Monitor Dashboard"
FOOTER_TEXT = "© 2025 Monitor App - Powered by Flask & Bootstrap"
FAVICON_PATH = "favicon.ico"  # 📌 Favicon のパスを設定

# 📌 セルの色のルール設定
TABLE_CELL_STYLES = {
    "orders": {  # 📌 orders テーブルのセルスタイル
        "amount": {  # 📌 `amount` カラムのルール
            "greater_than": {"value": 10, "class": "bg-danger text-white"},
            "less_than": {"value": 5, "class": "bg-warning text-dark"},
            "equal_to": {"value": 7, "class": "bg-success text-white"},
        }
    },
    "products": {  # 📌 products テーブルのセルスタイル
        "price": {
            "greater_than": {"value": 1000, "class": "bg-primary text-white"},
            "less_than": {"value": 500, "class": "bg-info text-dark"},
            "equal_to": {"value": 750, "class": "bg-secondary text-white"},
        }
    },
}
