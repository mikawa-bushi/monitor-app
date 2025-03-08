import os
import sys
import shutil
import subprocess
import click

# `monitor_app` のパスを `sys.path` に追加
CLI_DIR = os.path.dirname(
    os.path.abspath(__file__)
)  # `monitor_app/cli.py` のあるディレクトリ
if CLI_DIR not in sys.path:
    sys.path.append(CLI_DIR)


from app import run_server  # `app.py` の `run_server()` を直接呼び出す

# TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), "monitor_app")
PARENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MONITOR_APP_DIR = os.path.dirname(__file__)
CONFIG_DIR = os.path.join(MONITOR_APP_DIR, "config")
CSV_DIR = os.path.join(MONITOR_APP_DIR, "csv")
TEMPLATES_DIR = os.path.join(MONITOR_APP_DIR, "templates")
STATIC_DIR = os.path.join(MONITOR_APP_DIR, "static")
STATIC_CSS_DIR = os.path.join(STATIC_DIR, "css")
STATIC_JS_DIR = os.path.join(STATIC_DIR, "js")
STATIC_IMG_DIR = os.path.join(STATIC_DIR, "img")
PYPROJECT_TEMPLATE = os.path.join(MONITOR_APP_DIR, "pyproject.toml")


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
    # DEST_MONITOR_APP_DIR = os.path.join(project_path, "monitor_app")
    DEST_MONITOR_APP_DIR = project_path
    DEST_PARENT_DIR = os.path.dirname(DEST_MONITOR_APP_DIR)
    DEST_CONFIG_DIR = os.path.join(DEST_MONITOR_APP_DIR, "config")
    DEST_CSV_DIR = os.path.join(DEST_MONITOR_APP_DIR, "csv")
    DEST_INSTANCES_DIR = os.path.join(DEST_MONITOR_APP_DIR, "instances")
    DEST_TEMPLATES_DIR = os.path.join(DEST_MONITOR_APP_DIR, "templates")
    DEST_STATIC_DIR = os.path.join(DEST_MONITOR_APP_DIR, "static")
    DEST_STATIC_CSS_DIR = os.path.join(DEST_STATIC_DIR, "css")
    DEST_STATIC_JS_DIR = os.path.join(DEST_STATIC_DIR, "js")
    DEST_STATIC_IMG_DIR = os.path.join(DEST_STATIC_DIR, "img")
    # os.makedirs(DEST_MONITOR_APP_DIR)

    # 📂 CSV・インスタンスフォルダ作成
    os.makedirs(DEST_CSV_DIR, exist_ok=True)
    os.makedirs(DEST_CONFIG_DIR, exist_ok=True)
    os.makedirs(DEST_INSTANCES_DIR, exist_ok=True)
    os.makedirs(DEST_TEMPLATES_DIR, exist_ok=True)
    os.makedirs(DEST_STATIC_JS_DIR, exist_ok=True)
    os.makedirs(DEST_STATIC_CSS_DIR, exist_ok=True)
    os.makedirs(DEST_STATIC_IMG_DIR, exist_ok=True)

    # 📄 `project_template` からファイルをコピー
    monitor_app_files = ["app.py", "cli.py", "csv_to_db.py"]
    for file in monitor_app_files:
        src_path = os.path.join(MONITOR_APP_DIR, file)
        dest_path = os.path.join(DEST_MONITOR_APP_DIR, file)
        if os.path.exists(src_path):
            shutil.copy(src_path, dest_path)
        else:
            click.echo(
                f"⚠️  {file} が `project_template` に見つかりません。スキップします。"
            )

    config_files = ["config.py"]
    for file in config_files:
        src_path = os.path.join(CONFIG_DIR, file)
        dest_path = os.path.join(DEST_CONFIG_DIR, file)
        if os.path.exists(src_path):
            shutil.copy(src_path, dest_path)
        else:
            click.echo(
                f"⚠️  {file} が `project_template` に見つかりません。スキップします。"
            )

    templates_files = ["base.html", "index.html", "table.html"]
    for file in templates_files:
        src_path = os.path.join(TEMPLATES_DIR, file)
        dest_path = os.path.join(DEST_TEMPLATES_DIR, file)
        if os.path.exists(src_path):
            shutil.copy(src_path, dest_path)
        else:
            click.echo(
                f"⚠️  {file} が `project_template` に見つかりません。スキップします。"
            )

    css_files = ["bootstrap.min.css", "jquery.datatables.min.css"]
    for file in css_files:
        src_path = os.path.join(STATIC_CSS_DIR, file)
        dest_path = os.path.join(DEST_STATIC_CSS_DIR, file)
        if os.path.exists(src_path):
            shutil.copy(src_path, dest_path)
        else:
            click.echo(
                f"⚠️  {file} が `project_template` に見つかりません。スキップします。"
            )

    js_files = [
        "bootstrap.bundle.min.js",
        "jquery.datatables.min.js",
        "jquery.min.js",
        "refresh_table.js",
    ]
    for file in js_files:
        src_path = os.path.join(STATIC_JS_DIR, file)
        dest_path = os.path.join(DEST_STATIC_JS_DIR, file)
        if os.path.exists(src_path):
            shutil.copy(src_path, dest_path)
        else:
            click.echo(
                f"⚠️  {file} が `project_template` に見つかりません。スキップします。"
            )

    img_files = ["background.webp", "logo.webp"]
    for file in img_files:
        src_path = os.path.join(STATIC_IMG_DIR, file)
        dest_path = os.path.join(DEST_STATIC_IMG_DIR, file)
        if os.path.exists(src_path):
            shutil.copy(src_path, dest_path)
        else:
            click.echo(
                f"⚠️  {file} が `project_template` に見つかりません。スキップします。"
            )
    # 📄 Favicon コピー
    favicon_src = os.path.join(MONITOR_APP_DIR, "static/favicon.ico")
    favicon_dest = os.path.join(DEST_MONITOR_APP_DIR, "static/favicon.ico")
    if os.path.exists(favicon_src):
        shutil.copy(favicon_src, favicon_dest)
    else:
        click.echo(
            "⚠️  favicon.ico が `project_template` に見つかりません。スキップします。"
        )

    # parent_files = ["pyproject.toml"]
    # for file in parent_files:
    #     src_path = os.path.join(PARENT_DIR, file)
    #     dest_path = os.path.join(DEST_PARENT_DIR, file)
    #     if os.path.exists(src_path):
    #         shutil.copy(src_path, dest_path)
    #     else:
    #         click.echo(
    #             f"⚠️  {file} が `project_template` に見つかりません。スキップします。"
    #         )


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
