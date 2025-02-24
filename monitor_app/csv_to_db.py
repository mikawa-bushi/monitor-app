import os
import pandas as pd
from sqlalchemy import (
    create_engine,
    MetaData,
    Table,
    Column,
    Integer,
    String,
    Float,
    ForeignKey,
)
from monitor_app.config.config import (
    SQLALCHEMY_DATABASE_URI,
    ALLOWED_TABLES,
)  # ✅ `ALLOWED_TABLES` を使用

# データベースエンジンの作成
engine = create_engine(SQLALCHEMY_DATABASE_URI)
metadata = MetaData()

# 📌 CSV ディレクトリのパスを修正
CSV_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "csv_data")


def create_tables():
    """ALLOWED_TABLES に基づいてテーブルを作成"""
    for table_name, table_info in ALLOWED_TABLES.items():
        columns = []

        # カラムの型を判定
        for col in table_info["columns"]:
            if col.endswith("_id"):  # ID系のカラムは Integer 型
                columns.append(Column(col, Integer))
            elif "price" in col or "amount" in col:  # 金額や数量系のカラムは Float 型
                columns.append(Column(col, Float))
            else:  # それ以外は String 型
                columns.append(Column(col, String(255)))

        # プライマリーキーの追加
        if "primary_key" in table_info:
            columns.append(Column(table_info["primary_key"], Integer, primary_key=True))

        # 外部キーの設定
        if "foreign_keys" in table_info:
            for col, ref in table_info["foreign_keys"].items():
                columns.append(Column(col, Integer, ForeignKey(ref)))

        # テーブルを作成
        Table(table_name, metadata, *columns, extend_existing=True)

    metadata.create_all(engine)  # データベースに適用
    print("✅ テーブルを作成しました")


def import_csv_to_db():
    """CSV をデータベースにインポート"""
    for file in os.listdir(CSV_DIR):
        if file.endswith(".csv"):
            table_name = os.path.splitext(file)[0]
            if (
                table_name in ALLOWED_TABLES
            ):  # 📌 `ALLOWED_TABLES` に含まれるもののみ処理
                df = pd.read_csv(os.path.join(CSV_DIR, file))
                df.to_sql(table_name, con=engine, if_exists="replace", index=False)
                print(f"✅ {table_name} に {len(df)} 件のデータを挿入しました")


if __name__ == "__main__":
    create_tables()
    import_csv_to_db()
