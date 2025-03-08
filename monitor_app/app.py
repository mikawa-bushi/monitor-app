import os
import sys
from flask import Flask, render_template, abort
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text

# config/config.py の親ディレクトリを sys.path に追加
CONFIG_PARENT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "config"))
if CONFIG_PARENT_DIR not in sys.path:
    sys.path.append(CONFIG_PARENT_DIR)

from config import (
    SQLALCHEMY_DATABASE_URI,
    SQLALCHEMY_TRACK_MODIFICATIONS,
    ALLOWED_TABLES,
    APP_TITLE,
    HEADER_TEXT,
    FOOTER_TEXT,
    FAVICON_PATH,  # ✅ Favicon を追加
    TABLE_CELL_STYLES,
    TABLE_REFRESH_INTERVAL,
)


app = Flask(__name__)
CORS(app)

# 設定を `config.py` から読み込む
app.config["SQLALCHEMY_DATABASE_URI"] = SQLALCHEMY_DATABASE_URI
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = SQLALCHEMY_TRACK_MODIFICATIONS
print(SQLALCHEMY_DATABASE_URI)

db = SQLAlchemy(app)


@app.route("/")
def index():
    """トップページ"""
    tables = list(ALLOWED_TABLES.keys())  # 📌 許可されたテーブルのみ表示
    return render_template(
        "index.html",
        tables=tables,
        app_title=APP_TITLE,
        header_text=HEADER_TEXT,
        footer_text=FOOTER_TEXT,
        favicon_path=FAVICON_PATH,  # ✅ Favicon を追加
        title=APP_TITLE,  # ✅ タイトルを設定
    )


@app.route("/table/<table_name>")
def show_table(table_name):
    """指定されたテーブルのデータを表示"""
    if table_name not in ALLOWED_TABLES:  # 📌 許可されていないテーブルは 404
        abort(404)

    table_info = ALLOWED_TABLES[table_name]

    # 📌 `join` 設定があれば JOIN クエリを実行
    query = (
        text(table_info["join"])
        if "join" in table_info
        else text(f"SELECT * FROM {table_name}")
    )

    result = db.session.execute(query)
    columns = result.keys()
    data = [dict(zip(columns, row)) for row in result.fetchall()]

    return render_template(
        "table.html",
        table_name=table_name,
        columns=columns,
        data=data,
        cell_styles=TABLE_CELL_STYLES,
        app_title=APP_TITLE,
        header_text=HEADER_TEXT,
        footer_text=FOOTER_TEXT,
        favicon_path=FAVICON_PATH,  # ✅ Favicon を追加
        title=f"{table_name} - {APP_TITLE}",  # ✅ タイトルを設定
        refresh_interval=TABLE_REFRESH_INTERVAL,
    )


def run_server(host="0.0.0.0", port=9990, debug=False):
    """Flask Web アプリを起動"""
    app.run(host=host, port=port, debug=debug)


def main():
    run_server()


if __name__ == "__main__":
    main()
