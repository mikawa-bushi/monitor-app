# Monitor App 🚀

Monitor App は、CSV データをデータベースに取り込み、Web ブラウザで閲覧できるシンプルなアプリケーションです。  
SQLite / MySQL / PostgreSQL に対応し、Django のように `startproject` コマンドでプロジェクトを作成できます。

## 📌 特徴
- `monitor-app startproject` で新しいプロジェクトを作成
- CSV データをデータベースに簡単にインポート
- Web UI でデータを表示
- `Flask-SQLAlchemy` を使用し、SQLite / MySQL / PostgreSQL に対応
- Bootstrap を使用したスタイリッシュな UI
- `config.py` でカスタマイズ可能

---

## 🚀 インストール方法
`pip install` で簡単にインストールできます。

```sh
pip install monitor-app
```

---

## 🔧 使い方

### **1️⃣ 新しいプロジェクトを作成**
```sh
monitor-app startproject my_project
```
➡ `my_project` フォルダに Flask アプリのテンプレートが作成されます。

### **2️⃣ CSV をデータベースに登録**
```sh
cd my_project
monitor-app import-csv
```
➡ `csv/` フォルダの CSV をデータベースに登録します。

### **3️⃣ Web アプリを起動**
```sh
monitor-app runserver
```
➡ `http://127.0.0.1:9990` にアクセス！

### **📌 `runserver` のオプション**
| オプション | 説明 |
|------------|--------------------------------|
| `--csv`   | CSV を登録してから起動する  |
| `--debug` | デバッグモードで起動する    |
| `--port <PORT>` | ポートを指定する（デフォルト: 9990） |

📌 **例: CSV を登録後に起動**
```sh
monitor-app runserver --csv
```

📌 **例: デバッグモードでポート `8000` で起動**
```sh
monitor-app runserver --debug --port 8000
```

---

## 📂 フォルダ構成
```sh
my_project/
│── monitor_app/
│   ├── app.py        # Flask アプリのメインファイル
│   ├── config.py     # 設定ファイル
│   ├── csv_to_db.py  # CSV をデータベースにインポートするスクリプト
│   ├── templates/    # HTML テンプレート
│   ├── static/       # CSS / JavaScript / 画像
│   ├── csv/          # CSV データを保存するフォルダ
│   ├── instances/    # データベースの保存先
│── pyproject.toml    # Poetry の設定ファイル
│── README.md         # このファイル
```

---

## 🔧 `config.py` の設定
プロジェクトの設定は `monitor_app/config.py` で変更できます。

📌 **データベースの設定**
```python
# SQLite（デフォルト）
SQLALCHEMY_DATABASE_URI = "sqlite:///instances/database.db"

# MySQL を使用する場合
# SQLALCHEMY_DATABASE_URI = "mysql+pymysql://user:password@localhost/dbname"

# PostgreSQL を使用する場合
# SQLALCHEMY_DATABASE_URI = "postgresql://user:password@localhost/dbname"
```

📌 **カスタムテーブルと JOIN の設定**
```python
ALLOWED_TABLES = {
    "users": {"columns": ["id", "name", "email"], "primary_key": "id"},
    "products": {"columns": ["id", "name", "price"], "primary_key": "id"},
    "orders": {
        "columns": ["id", "user_id", "product_id", "amount"],
        "primary_key": "id",
        "foreign_keys": {"user_id": "users.id", "product_id": "products.id"},
        "join": '''
            SELECT orders.id, users.name AS user_name, products.name AS product_name, orders.amount
            FROM orders
            JOIN users ON orders.user_id = users.id
            JOIN products ON orders.product_id = products.id
        ''',
    },
}
```

---

## 📌 `monitor-app` の CLI コマンド一覧
| コマンド | 説明 |
|------------|----------------------------------|
| `monitor-app startproject <name>` | 新しいプロジェクトを作成 |
| `monitor-app import-csv` | CSV をデータベースに登録 |
| `monitor-app runserver` | Web アプリを起動 |
| `monitor-app runserver --csv` | CSV 登録後に起動 |
| `monitor-app runserver --port <PORT>` | 指定ポートで起動 |

---

## 📌 必要な環境
- Python 3.10+
- `Flask`, `Flask-SQLAlchemy`, `pandas`, `click`
- `Poetry` (開発環境)

---

## 📌 ライセンス
MIT ライセンスのもとで提供されています。

---

## 📌 貢献
Pull Request 大歓迎！🚀  
バグ報告や改善提案もお待ちしています！

🔗 **GitHub:** [Monitor App Repository](https://github.com/hardwork9047/monitor-app)

---

✅ **これで `monitor-app` を簡単にインストール＆利用できるようになります！** 🚀
