import os
import sys
from flask import Flask, render_template, abort, jsonify, request
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
    """指定されたテーブルのデータを表示（Jinja 用）"""
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

    # ✅ デバッグログを追加（Flask のログに出力）
    print(f"Columns: {columns}")
    print(f"Data: {data}")
    print(f"cell_styles: {TABLE_CELL_STYLES}")

    if not data:
        return f"データが見つかりませんでした: {table_name}", 500  # エラーハンドリング

    return render_template(
        "table.html",
        table_name=table_name,
        columns=columns,
        data=data,
        cell_styles=TABLE_CELL_STYLES.get(table_name, {}),
    )


@app.route("/api/table/<table_name>")
def get_table_data(table_name):
    """テーブルデータを JSON で返す API"""
    if table_name not in ALLOWED_TABLES:
        return jsonify({"error": "テーブルが見つかりません"}), 404

    table_info = ALLOWED_TABLES[table_name]
    query = (
        text(table_info["join"])
        if "join" in table_info
        else text(f"SELECT * FROM {table_name}")
    )

    result = db.session.execute(query)
    columns = result.keys()
    data = [dict(zip(columns, row)) for row in result.fetchall()]

    return jsonify(
        {
            "table_name": table_name,
            "columns": list(columns),
            "data": data,
            "cell_styles": TABLE_CELL_STYLES.get(table_name, {}),
        }
    )


# CRUD API endpoints
@app.route("/api/<table_name>", methods=["GET"])
def get_all_records(table_name):
    """Get all records from a table"""
    if table_name not in ALLOWED_TABLES:
        return jsonify({"error": "Table not found"}), 404
    
    try:
        query = text(f"SELECT * FROM {table_name}")
        result = db.session.execute(query)
        columns = result.keys()
        data = [dict(zip(columns, row)) for row in result.fetchall()]
        
        return jsonify({
            "success": True,
            "data": data,
            "count": len(data)
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/<table_name>/<int:record_id>", methods=["GET"])
def get_record(table_name, record_id):
    """Get a specific record by ID"""
    if table_name not in ALLOWED_TABLES:
        return jsonify({"error": "Table not found"}), 404
    
    try:
        primary_key = ALLOWED_TABLES[table_name].get("primary_key", "id")
        query = text(f"SELECT * FROM {table_name} WHERE {primary_key} = :id")
        result = db.session.execute(query, {"id": record_id})
        columns = result.keys()
        row = result.fetchone()
        
        if not row:
            return jsonify({"error": "Record not found"}), 404
        
        data = dict(zip(columns, row))
        return jsonify({
            "success": True,
            "data": data
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/<table_name>", methods=["POST"])
def create_record(table_name):
    """Create a new record"""
    if table_name not in ALLOWED_TABLES:
        return jsonify({"error": "Table not found"}), 404
    
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "No data provided"}), 400
        
        # Get allowed columns for this table
        allowed_columns = ALLOWED_TABLES[table_name]["columns"]
        primary_key = ALLOWED_TABLES[table_name].get("primary_key", "id")
        
        # Filter data to only include allowed columns (exclude primary key for auto-increment)
        filtered_data = {k: v for k, v in data.items() 
                        if k in allowed_columns and k != primary_key}
        
        if not filtered_data:
            return jsonify({"error": "No valid data provided"}), 400
        
        # Build INSERT query
        columns = list(filtered_data.keys())
        placeholders = ", ".join([f":{col}" for col in columns])
        column_names = ", ".join(columns)
        
        query = text(f"INSERT INTO {table_name} ({column_names}) VALUES ({placeholders})")
        db.session.execute(query, filtered_data)
        db.session.commit()
        
        return jsonify({
            "success": True,
            "message": "Record created successfully",
            "data": filtered_data
        }), 201
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500


@app.route("/api/<table_name>/<int:record_id>", methods=["PUT"])
def update_record(table_name, record_id):
    """Update a specific record"""
    if table_name not in ALLOWED_TABLES:
        return jsonify({"error": "Table not found"}), 404
    
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "No data provided"}), 400
        
        # Get allowed columns for this table
        allowed_columns = ALLOWED_TABLES[table_name]["columns"]
        primary_key = ALLOWED_TABLES[table_name].get("primary_key", "id")
        
        # Filter data to only include allowed columns (exclude primary key)
        filtered_data = {k: v for k, v in data.items() 
                        if k in allowed_columns and k != primary_key}
        
        if not filtered_data:
            return jsonify({"error": "No valid data provided"}), 400
        
        # Check if record exists
        check_query = text(f"SELECT COUNT(*) FROM {table_name} WHERE {primary_key} = :id")
        result = db.session.execute(check_query, {"id": record_id})
        if result.scalar() == 0:
            return jsonify({"error": "Record not found"}), 404
        
        # Build UPDATE query
        set_clause = ", ".join([f"{col} = :{col}" for col in filtered_data.keys()])
        query = text(f"UPDATE {table_name} SET {set_clause} WHERE {primary_key} = :record_id")
        
        # Add record_id to the parameters
        params = filtered_data.copy()
        params["record_id"] = record_id
        
        db.session.execute(query, params)
        db.session.commit()
        
        # Get the updated record
        get_query = text(f"SELECT * FROM {table_name} WHERE {primary_key} = :id")
        get_result = db.session.execute(get_query, {"id": record_id})
        columns = get_result.keys()
        row = get_result.fetchone()
        
        return jsonify({
            "success": True,
            "message": "Record updated successfully",
            "data": dict(zip(columns, row))
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500


@app.route("/api/<table_name>/<int:record_id>", methods=["DELETE"])
def delete_record(table_name, record_id):
    """Delete a specific record"""
    if table_name not in ALLOWED_TABLES:
        return jsonify({"error": "Table not found"}), 404
    
    try:
        primary_key = ALLOWED_TABLES[table_name].get("primary_key", "id")
        
        # Check if record exists
        check_query = text(f"SELECT COUNT(*) FROM {table_name} WHERE {primary_key} = :id")
        result = db.session.execute(check_query, {"id": record_id})
        if result.scalar() == 0:
            return jsonify({"error": "Record not found"}), 404
        
        # Delete the record
        query = text(f"DELETE FROM {table_name} WHERE {primary_key} = :id")
        db.session.execute(query, {"id": record_id})
        db.session.commit()
        
        return jsonify({
            "success": True,
            "message": "Record deleted successfully"
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500


@app.route("/api/tables", methods=["GET"])
def get_tables():
    """Get list of available tables and their schema"""
    tables_info = {}
    for table_name, table_info in ALLOWED_TABLES.items():
        tables_info[table_name] = {
            "columns": table_info["columns"],
            "primary_key": table_info.get("primary_key", "id"),
            "foreign_keys": table_info.get("foreign_keys", {})
        }
    
    return jsonify({
        "success": True,
        "tables": tables_info
    })


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
