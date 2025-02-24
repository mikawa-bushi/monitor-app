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
