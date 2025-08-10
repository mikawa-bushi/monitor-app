# Monitor App 🚀

Monitor App は、CSV データをデータベースに取り込み、Webブラウザで閲覧できるシンプルなアプリケーションです。  
SQLite / MySQL / PostgreSQL に対応し、`startproject` コマンドでプロジェクトを作成できます。  
このプロジェクトはFlask, Djangoに非常にインスパイアされており、特にFlaskをベースとしたライブラリです。  
Python初学者の多い製造業にて、より短期間でWebアプリを作成することを目的に開発しています。

## 📌 特徴
- `monitor-app startproject` で新しいプロジェクトを作成
- CSV データをSQLiteに簡単に変換
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
monitor-app startproject <プロジェクト名>
```
📌 **例: my_projectという名称でテンプレートを作成**
```sh
monitor-app startproject my_project
```
➡ `my_project` フォルダにMonitor-appアプリのテンプレートが作成されます。


### **2️⃣ CSV をデータベースに登録**
```sh
cd my_project
python <プロジェクト名>/app.py import-csv
```
➡ `csv/` フォルダのCSVをSQLiteデータベースに変換します。

### **3️⃣ Web アプリを起動**
```sh
python <プロジェクト名>/app.py runserver
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
python <プロジェクト名>/app.py runserver --csv
```

📌 **例: デバッグモードでポート `8000` で起動**
```sh
python <プロジェクト名>/app.py runserver --debug --port 8000
```

---

## 📂 フォルダ構成
```sh
my_project/
│── monitor_app/
│   ├── app.py           # Flask アプリのメインファイル
│   ├── cli.py           # テンプレート作成用のコマンドファイル
│   ├── csv_to_db.py     # CSV をデータベースにインポートするスクリプト
│   ├── config/
│   │    ├── config.py   # 設定ファイル
│   ├── templates/       # HTML テンプレート
│   ├── static/          # CSS / JavaScript / 画像
│   ├── csv/             # CSV データを保存するフォルダ
│   ├── instances/       # SQLiteデータベースの保存先
│── pyproject.toml       # Poetry の設定ファイル
│── README.md            # このファイル
```

---

## 🔧 `config.py` の設定
プロジェクトの設定は `config/config.py` で変更できます。

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

## 🌐 REST API エンドポイント

Monitor App では、データベースの CRUD 操作を行う REST API が利用できます。

### **📌 利用可能なエンドポイント**

#### **テーブル情報**
- `GET /api/tables` - すべてのテーブルのスキーマ情報を取得

#### **データ操作**
- `GET /api/<table_name>` - テーブルの全レコードを取得
- `GET /api/<table_name>/<id>` - 指定 ID のレコードを取得
- `POST /api/<table_name>` - 新しいレコードを作成
- `PUT /api/<table_name>/<id>` - 指定 ID のレコードを更新
- `DELETE /api/<table_name>/<id>` - 指定 ID のレコードを削除

### **📌 API 使用例**

#### **1. テーブル一覧の取得**
```bash
curl -X GET http://localhost:9990/api/tables
```

#### **2. ユーザー一覧の取得**
```bash
curl -X GET http://localhost:9990/api/users
```

#### **3. 新しいユーザーの作成**
```bash
curl -X POST http://localhost:9990/api/users \
  -H "Content-Type: application/json" \
  -d '{"name": "田中太郎", "email": "tanaka@example.com"}'
```

#### **4. ユーザー情報の更新**
```bash
curl -X PUT http://localhost:9990/api/users/1 \
  -H "Content-Type: application/json" \
  -d '{"name": "田中次郎", "email": "tanaka.updated@example.com"}'
```

#### **5. ユーザーの削除**
```bash
curl -X DELETE http://localhost:9990/api/users/1
```

### **📌 レスポンス形式**

#### **成功時のレスポンス例**
```json
{
  "success": true,
  "message": "Record created successfully",
  "data": {
    "id": 1,
    "name": "田中太郎",
    "email": "tanaka@example.com"
  }
}
```

#### **エラー時のレスポンス例**
```json
{
  "error": "Record not found"
}
```

### **📌 注意事項**
- API は `config.py` の `ALLOWED_TABLES` で定義されたテーブルのみ操作可能
- プライマリーキー（通常は `id`）は自動設定のため、POST リクエストでは送信不要
- 外部キー制約のあるテーブルでは、関連するレコードの存在を確認してから操作してください

## 🧪 テストの実行

Monitor App には、REST API の動作を検証する包括的なテストスイートが含まれています。

### **📌 テスト環境のセットアップ**

テストの実行には `pytest` が必要です（Poetry 環境では既にインストール済み）。

```bash
# Poetry を使用している場合
poetry install

# pip を使用している場合
pip install pytest
```

### **📌 テストの実行方法**

#### **全テストを実行**
```bash
python -m pytest tests/test_api.py -v
```

#### **特定のテストクラスのみ実行**
```bash
# ユーザー API のテストのみ
python -m pytest tests/test_api.py::TestUsersAPI -v

# 商品 API のテストのみ
python -m pytest tests/test_api.py::TestProductsAPI -v

# エラーハンドリングのテストのみ
python -m pytest tests/test_api.py::TestErrorHandling -v
```

#### **特定のテストメソッドのみ実行**
```bash
python -m pytest tests/test_api.py::TestUsersAPI::test_create_user -v
```

### **📌 テスト内容**

#### **🔹 テーブル情報API (`TestTableAPI`)**
- `GET /api/tables` - テーブルスキーマの取得

#### **🔹 ユーザーAPI (`TestUsersAPI`)**
- `GET /api/users` - 全ユーザー取得
- `GET /api/users/<id>` - 特定ユーザー取得
- `POST /api/users` - ユーザー作成
- `PUT /api/users/<id>` - ユーザー更新
- `DELETE /api/users/<id>` - ユーザー削除
- エラーケース（存在しないユーザー、無効なデータ）

#### **🔹 商品API (`TestProductsAPI`)**
- `GET /api/products` - 全商品取得
- `POST /api/products` - 商品作成
- `PUT /api/products/<id>` - 商品更新

#### **🔹 注文API (`TestOrdersAPI`)**
- `GET /api/orders` - 全注文取得
- `POST /api/orders` - 注文作成（外部キー制約付き）

#### **🔹 エラーハンドリング (`TestErrorHandling`)**
- 存在しないテーブルへのアクセス
- 不正なJSON形式のリクエスト
- Content-Type ヘッダーなしのリクエスト

#### **🔹 データ検証 (`TestDataValidation`)**
- 余分なフィールドの自動除去
- 外部キー制約の検証

### **📌 テスト実行例**

```bash
$ python -m pytest tests/test_api.py -v

============================= test session starts ==============================
platform darwin -- Python 3.13.1, pytest-8.3.5, pluggy-1.5.0
collecting ... collected 20 items

tests/test_api.py::TestTableAPI::test_get_tables PASSED                  [  5%]
tests/test_api.py::TestUsersAPI::test_get_all_users PASSED               [ 10%]
tests/test_api.py::TestUsersAPI::test_get_user_by_id PASSED              [ 15%]
tests/test_api.py::TestUsersAPI::test_get_user_not_found PASSED          [ 20%]
tests/test_api.py::TestUsersAPI::test_create_user PASSED                 [ 25%]
...
============================== 20 passed in 0.95s ===========================
```

### **📌 継続的インテグレーション**

プロジェクトに CI/CD を設定する場合は、以下のコマンドをビルドスクリプトに追加してください：

```bash
# テストの実行
python -m pytest tests/test_api.py

# カバレッジレポート付きでテスト実行（オプション）
pip install pytest-cov
python -m pytest tests/test_api.py --cov=monitor_app --cov-report=html
```

### **📌 テストファイルの場所**
- `tests/test_api.py` - REST API のテスト
- `tests/test_app.py` - Web アプリケーションのテスト
- `tests/test_config.py` - 設定のテスト

### **📌 各テストファイルの詳細**

#### **🔹 `tests/test_api.py`**
REST APIの包括的なテストを行います。主な内容：

- **TestTableAPI**: データベーステーブルのスキーマ情報取得API
  - `GET /api/tables` のテスト（テーブル一覧、カラム情報、主キーの確認）
  
- **TestUsersAPI**: ユーザー管理API
  - 全ユーザー取得、特定ユーザー取得、ユーザー作成、更新、削除
  - エラーハンドリング（存在しないユーザー、無効なデータ）
  
- **TestProductsAPI**: 商品管理API
  - 商品の取得、作成、更新機能のテスト
  
- **TestOrdersAPI**: 注文管理API
  - 注文の取得、作成機能（外部キー制約の検証含む）
  
- **TestErrorHandling**: エラーハンドリング
  - 存在しないテーブルへのアクセス、不正なJSON、Content-Typeなしリクエスト
  
- **TestDataValidation**: データ検証
  - 余分なフィールドの除去、外部キー制約のテスト

#### **🔹 `tests/test_app.py`**
Webアプリケーションの基本機能をテストします：

- **test_index_page**: ルートページ（`/`）の表示確認
  - ステータスコード200、"Monitor Dashboard"の表示確認
  
- **test_table_page**: テーブル表示ページ（`/table/users`）の動作確認
  - 許可されたテーブルの表示確認

#### **🔹 `tests/test_config.py`**
設定ファイルの検証を行います：

- **test_database_uri**: データベース接続設定の確認
  - SQLite/MySQL/PostgreSQLのURI形式チェック
  
- **test_allowed_tables**: 許可されたテーブル設定の確認
  - users、orders、productsテーブルの設定確認
  
- **test_app_metadata**: アプリケーション設定の確認
  - アプリタイトル、ヘッダー、フッターテキストの設定確認

---

## 📌 `monitor-app` の CLI コマンド一覧
| コマンド | 説明 |
|------------|----------------------------------|
| `monitor-app startproject <name>` | 新しいプロジェクトを作成 |
| `monitor-app import-csv` | CSV をデータベースに登録 |
| `python <プロジェクト名>/app.py` | Web アプリを起動 |
| `python <プロジェクト名>/app.py --csv` | CSV 登録後に起動 |
| `python <プロジェクト名>/app.py --port <PORT>` | 指定ポートで起動 |

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
