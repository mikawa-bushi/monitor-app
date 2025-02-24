import click
import os
import subprocess  # ✅ `csv_to_db.py` を実行するために追加
from flask import Flask, render_template, abort
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text
from monitor_app.config.config import (
    SQLALCHEMY_DATABASE_URI,
    SQLALCHEMY_TRACK_MODIFICATIONS,
    ALLOWED_TABLES,
    APP_TITLE,
    HEADER_TEXT,
    FOOTER_TEXT,
    FAVICON_PATH,  # ✅ Favicon を追加
    TABLE_CELL_STYLES,
)

app = Flask(__name__)
CORS(app)

# 設定を `config.py` から読み込む
app.config["SQLALCHEMY_DATABASE_URI"] = SQLALCHEMY_DATABASE_URI
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = SQLALCHEMY_TRACK_MODIFICATIONS

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
    )


@click.command()
@click.option("--host", default="0.0.0.0", help="ホストアドレス")
@click.option("--port", default=9990, help="ポート番号")
@click.option("--csv", is_flag=True, help="CSV をデータベースに登録してから起動")
@click.option("--debug", is_flag=True, help="デバッグモードを有効化")
def run(host, port, csv, debug):
    """Flask Web アプリを起動"""

    if csv and not os.environ.get("FLASK_RUN_FROM_CLI"):
        print("🔄 CSV をデータベースに登録中...")
        subprocess.run(
            ["poetry", "run", "python", "monitor_app/csv_to_db.py"], check=True
        )  # ✅ `check=True` を追加
        print("✅ CSV 登録完了！アプリを起動します...")

    # ✅ `use_reloader=False` にすることで、Flask の再起動時に `csv_to_db.py` が再実行されるのを防ぐ
    app.run(host=host, port=port, debug=debug, use_reloader=debug)


def main():
    run()


if __name__ == "__main__":
    main()
