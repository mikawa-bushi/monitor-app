from flask import Flask, render_template
from sqlalchemy import create_engine, MetaData, Table, select
import os
from dotenv import load_dotenv

# 環境変数をロード
load_dotenv()

# Flask アプリのセットアップ
app = Flask(__name__)

# SQLite データベース設定
DB_NAME = os.getenv("DB_NAME", "database.db")
DATABASE_URL = f"sqlite:///{DB_NAME}"

# SQLAlchemy エンジン作成
engine = create_engine(DATABASE_URL)
metadata = MetaData()
metadata.reflect(bind=engine)  # 既存のDBテーブルを取得


@app.route("/")
def index():
    tables = metadata.tables.keys()  # すべてのテーブル名を取得
    return render_template("index.html", tables=tables)


@app.route("/table/<table_name>")
def show_table(table_name):
    if table_name not in metadata.tables:
        return f"Table '{table_name}' not found.", 404

    table = metadata.tables[table_name]
    with engine.connect() as conn:
        result = conn.execute(select(table)).fetchall()

    columns = table.columns.keys()  # テーブルのカラム名を取得
    data = [dict(zip(columns, row)) for row in result]  # データを辞書形式に変換

    return render_template(
        "table.html", table_name=table_name, columns=columns, data=data
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=9990, debug=True)
