import os
import shutil
import subprocess
import click
from monitor_app.app import run_server  # `app.py` の `run_server()` を直接呼び出す

TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), "project_template")


@click.group()
def cli():
    """Monitor App CLI ツール"""
    pass


@click.command()
@click.argument("project_name")
def startproject(project_name):
    """新しい Monitor App プロジェクトを作成"""
    project_path = os.path.abspath(project_name)

    if os.path.exists(project_path):
        click.echo(f"⚠️  既に '{project_name}' が存在します！")
        return

    # 📂 プロジェクトフォルダ作成
    os.makedirs(project_path)

    # 📂 monitor_app アプリフォルダ作成
    app_dir = os.path.join(project_path, "monitor_app")
    os.makedirs(app_dir)

    # 📂 CSV・インスタンスフォルダ作成
    os.makedirs(os.path.join(app_dir, "csv"), exist_ok=True)
    os.makedirs(os.path.join(app_dir, "instances"), exist_ok=True)
    os.makedirs(os.path.join(app_dir, "templates"), exist_ok=True)
    os.makedirs(os.path.join(app_dir, "static/js"), exist_ok=True)
    os.makedirs(os.path.join(app_dir, "static/css"), exist_ok=True)

    # 📄 必要なファイルをコピー
    for file in ["app.py", "config.py", "csv_to_db.py", "models.py"]:
        shutil.copy(os.path.join(TEMPLATE_DIR, file), app_dir)

    # 📄 Favicon コピー
    shutil.copy(
        os.path.join(TEMPLATE_DIR, "favicon.ico"), os.path.join(app_dir, "static")
    )

    # 📄 `pyproject.toml` の作成
    with open(os.path.join(project_path, "pyproject.toml"), "w") as f:
        f.write(
            f"""\
[tool.poetry]
name = "{project_name}"
version = "0.1.0"
description = "Monitor App Project"
authors = ["Your Name <your@email.com>"]
readme = "README.md"

[tool.poetry.dependencies]
python = "^3.10"
flask = "^3.1.0"
flask-sqlalchemy = "^3.1.1"
pandas = "^2.2.3"
flask-cors = "^4.0.0"
click = "^8.1.3"
pymysql = "^1.1.0"
psycopg2-binary = "^2.9.10"

[build-system]
requires = ["poetry-core"]
build-backend = "poetry.core.masonry.api"
"""
        )

    click.echo(f"✅ プロジェクト '{project_name}' を作成しました！")


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
def runserver(host, port, csv, debug):
    """Flask Web アプリを起動"""

    if csv:
        click.echo("🔄 CSV をデータベースに登録中...")
        run_command(["monitor_app/csv_to_db.py"])
        click.echo("✅ CSV 登録完了！アプリを起動します...")

    click.echo(f"🚀 Web アプリを {host}:{port} で起動")
    run_server(host=host, port=port, debug=debug)  # `run_server()` を直接呼び出す


@click.command()
def import_csv():
    """CSV をデータベースにインポート"""
    click.echo("📂 CSV をデータベースに登録中...")
    run_command(["monitor_app/csv_to_db.py"])
    click.echo("✅ CSV 登録完了！")


cli.add_command(startproject)
cli.add_command(runserver)
cli.add_command(import_csv)

if __name__ == "__main__":
    cli()
