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
    CSV_DIR,  # ✅ `CSV_DIR` を `config.py` から取得
)

# データベースエンジンの作成
engine = create_engine(SQLALCHEMY_DATABASE_URI)
metadata = MetaData()


def create_tables():
    """ALLOWED_TABLES に基づいてテーブルを作成"""
    for table_name, table_info in ALLOWED_TABLES.items():
        columns = []

        # プライマリーキーの追加
        primary_key = table_info.get("primary_key")

        # カラムの型を判定
        for col in table_info["columns"]:
            col_type = String(255)  # デフォルトは String

            if col == primary_key:  # プライマリーキー
                col_type = Integer
            elif col.endswith("_id"):  # ID系のカラムは Integer 型
                col_type = Integer
            elif "price" in col or "amount" in col:  # 金額や数量系のカラムは Float 型
                col_type = Float

            col_def = Column(col, col_type, primary_key=(col == primary_key))
            columns.append(col_def)

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
                file_path = os.path.join(CSV_DIR, file)
                df = pd.read_csv(file_path)

                # データ型を SQLAlchemy のカラム定義と合わせる
                for col in df.columns:
                    if col in ALLOWED_TABLES[table_name]["columns"]:
                        if col.endswith("_id") or col == ALLOWED_TABLES[table_name].get(
                            "primary_key"
                        ):
                            df[col] = df[col].astype("Int64")  # Nullを考慮した整数型
                        elif "price" in col or "amount" in col:
                            df[col] = df[col].astype(float)

                df.to_sql(table_name, con=engine, if_exists="replace", index=False)
                print(f"✅ {table_name} に {len(df)} 件のデータを挿入しました")


if __name__ == "__main__":
    create_tables()
    import_csv_to_db()
