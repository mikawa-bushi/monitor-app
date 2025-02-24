from flask import Flask, render_template
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect, text  # ✅ text を追加
import os
import json
import importlib.resources as resources
import click
from dotenv import load_dotenv

# 環境変数をロード
load_dotenv()

app = Flask(__name__)
CORS(app)

# ✅ データベースを `monitor_app/instances/database.db` に保存
INSTANCE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "instances")
os.makedirs(INSTANCE_DIR, exist_ok=True)  # `instances/` フォルダがなければ作成
DB_PATH = os.path.join(INSTANCE_DIR, "database.db")
app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{DB_PATH}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)


# JSONファイルの読み込み
def load_json(filename):
    """インストールされたパッケージ内の JSON ファイルを読み込む"""
    with resources.open_text("monitor_app", filename) as f:
        return json.load(f)


@app.route("/")
def index():
    inspector = inspect(db.engine)
    tables = inspector.get_table_names()
    print(tables)  # ✅ デバッグ用に表示
    return render_template("index.html", tables=tables)


@app.route("/table/<table_name>")
def show_table(table_name):
    query = text(f"SELECT * FROM {table_name}")  # ✅ text() を使う
    result = db.session.execute(query)  # ✅ `execute` のみ実行

    columns = result.keys()  # ✅ `.keys()` は `fetchall()` の前に実行
    data = [
        dict(zip(columns, row)) for row in result.fetchall()
    ]  # ✅ `fetchall()` はここで実行

    return render_template(
        "table.html", table_name=table_name, columns=columns, data=data
    )


@click.command()
@click.option("--host", default="0.0.0.0", help="ホストアドレス")
@click.option(
    "--port", default=9990, help="ポート番号"
)  # ✅ デフォルトポートを 9990 に変更
def run(host, port):
    """Flask Web アプリを起動"""
    app.run(host=host, port=port, debug=True)


def main():
    run()


if __name__ == "__main__":
    main()
