import os
import sys
from flask import Flask, render_template, abort
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text
import shutil
import subprocess
import click

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
    FAVICON_PATH,
    TABLE_CELL_STYLES,
    TABLE_REFRESH_INTERVAL,
)

from csv_to_db import create_tables, import_csv_to_db

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
    tables = list(ALLOWED_TABLES.keys())
    return render_template(
        "index.html",
        tables=tables,
        app_title=APP_TITLE,
        header_text=HEADER_TEXT,
        footer_text=FOOTER_TEXT,
        favicon_path=FAVICON_PATH,
        title=APP_TITLE,
    )


@app.route("/table/<table_name>")
def show_table(table_name):
    """指定されたテーブルのデータを表示"""
    if table_name not in ALLOWED_TABLES:
        abort(404)

    table_info = ALLOWED_TABLES[table_name]
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
        favicon_path=FAVICON_PATH,
        title=f"{table_name} - {APP_TITLE}",
        refresh_interval=TABLE_REFRESH_INTERVAL,
    )


def run_command(command_list):
    """
    📌 poetry がインストールされていれば `poetry run` を使用し、なければ `python` を使用
    """
    if shutil.which("poetry"):
        command_list.insert(0, "poetry")
        command_list.insert(1, "run")
    else:
        command_list.insert(0, "python")

    subprocess.run(command_list, check=True)


@click.command()
@click.option("--host", default="0.0.0.0", help="ホストアドレス")
@click.option("--port", default=9990, help="ポート番号")
@click.option("--csv", is_flag=True, help="CSV をデータベースに登録してから起動")
@click.option("--debug", is_flag=True, help="デバッグモードを有効化")
def run_server(host, port, csv, debug):
    """Flask Web アプリを起動"""
    if csv:
        click.echo("🔄 CSV をデータベースに登録中...")
        create_tables()
        import_csv_to_db()
        click.echo("✅ CSV 登録完了！アプリを起動します...")

    app.run(host=host, port=port, debug=debug)


@click.command()
def import_csv():
    """CSV をデータベースにインポート"""
    click.echo("📂 CSV をデータベースに登録中...")
    create_tables()
    import_csv_to_db()
    click.echo("✅ CSV 登録完了！")


@click.group()
def cli():
    """コマンドラインインターフェースのエントリーポイント"""
    pass


cli.add_command(run_server)
cli.add_command(import_csv)

if __name__ == "__main__":
    cli()
