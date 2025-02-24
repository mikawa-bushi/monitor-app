import pandas as pd
from sqlalchemy import (
    create_engine,
    MetaData,
    Table,
    Column,
    Integer,
    String,
    ForeignKey,
    inspect,
)
import os
from dotenv import load_dotenv
import glob
import json

# 環境変数をロード
load_dotenv()

# SQLite データベース設定
DB_NAME = os.getenv("DB_NAME", "database.db")
DATABASE_URL = f"sqlite:///{DB_NAME}"

# SQLAlchemy エンジン作成
engine = create_engine(DATABASE_URL)
metadata = MetaData()


# スキーマ設定を JSON から読み込む
def load_schema():
    try:
        with open("schema.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        print("schema.json not found. Proceeding without schema validation.")
        return {}


schema_config = load_schema()


# 既存のカラムを確認
def get_existing_columns(table_name):
    inspector = inspect(engine)
    if table_name in inspector.get_table_names():
        return {col["name"] for col in inspector.get_columns(table_name)}
    return set()


# 依存関係を考慮してテーブルを作成
def create_tables():
    created_tables = set()

    while len(created_tables) < len(schema_config):
        for table_name, table_schema in schema_config.items():
            if table_name in created_tables:
                continue  # 既に作成済み

            foreign_keys = table_schema.get("foreign_keys", {})
            missing_dependencies = [
                ref.split(".")[0]
                for ref in foreign_keys.values()
                if ref.split(".")[0] not in created_tables
            ]

            # 依存関係が解決済みのテーブルのみ作成
            if not missing_dependencies:
                print(f"Creating table: {table_name}")

                columns = []
                existing_columns = get_existing_columns(
                    table_name
                )  # 既存のカラムを取得

                # CSV のヘッダーからカラムを取得
                csv_file = f"csv_data/{table_name}.csv"
                if not os.path.exists(csv_file):
                    print(f"Skipping {table_name}: CSV file not found.")
                    continue

                df = pd.read_csv(csv_file)
                csv_columns = set(df.columns)

                # スキーマに基づきカラムを作成
                for col_name in csv_columns:
                    col_type = table_schema.get(col_name, "String")
                    if col_type == "Integer":
                        columns.append(Column(col_name, Integer))
                    else:
                        columns.append(Column(col_name, String))

                # 主キーの設定（CSV に存在する場合のみ）
                primary_keys = table_schema.get("primary_keys", [])
                for column in columns:
                    if column.name in primary_keys:
                        column.primary_key = True

                # 外部キーの設定（CSV に存在する場合のみ & 既存のカラムに `ForeignKey` を追加）
                for col_name, ref in foreign_keys.items():
                    if col_name in csv_columns:
                        ref_table, ref_column = ref.split(".")
                        # 既存カラムの `ForeignKey` を更新
                        for column in columns:
                            if column.name == col_name:
                                column.foreign_keys.add(
                                    ForeignKey(f"{ref_table}.{ref_column}")
                                )

                # テーブル作成
                table = Table(table_name, metadata, *columns)
                metadata.create_all(engine)
                created_tables.add(table_name)


# CSV を個別のテーブルに登録
def import_csv_to_db(csv_folder):
    files = glob.glob(f"{csv_folder}/*.csv")
    if not files:
        print("No CSV files found in the folder.")
        return

    create_tables()  # 先にテーブルを作成

    for file in files:
        table_name = os.path.splitext(os.path.basename(file))[
            0
        ]  # ファイル名をテーブル名として使用
        print(f"Processing {file} -> Table: {table_name}")

        try:
            # CSV を DataFrame に変換
            df = pd.read_csv(file)

            # 既存のカラムを取得
            existing_columns = get_existing_columns(table_name)

            # データを挿入
            df.to_sql(table_name, con=engine, if_exists="append", index=False)
            print(f"Imported {len(df)} rows into {table_name}")

        except Exception as e:
            print(f"Error processing {file}: {e}")


# 実行
if __name__ == "__main__":
    import_csv_to_db("csv_data")
