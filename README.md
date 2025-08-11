# Monitor App 🚀

Monitor App は、CSV データから SQLite データベースを自動生成し、Web UI と REST API の両方でデータを管理できる高機能なデータ監視・管理アプリケーションです。

**🎯 主なユースケース:**
- **CSV データの迅速な可視化**: CSVファイルを配置するだけで、即座にWebアプリとAPIが利用可能
- **既存DBシステムへの迅速なフロントエンド提供**: MySQL・PostgreSQLで稼働中の既存システムに、設定変更だけで Web UI と REST API を追加
- **プロトタイプ開発の加速**: データ構造を定義するだけで、フル機能のCRUDアプリケーションが完成

**🔧 主要な機能:**
- **包括的なREST API**: フル CRUD 操作（作成・読み取り・更新・削除）をサポート
- **自動API ドキュメント**: Swagger UI による FastAPI ライクなドキュメント自動生成（`/docs`）
- **分離型設計**: CRUD操作用テーブルと表示用ビューの責任分離
- **柔軟なビュー表示**: カスタムクエリによるデータ表示（JOIN、集計、フィルタリング等）、リアルタイム更新（2秒間隔）
- **高度なスタイリング機能**: セル値に応じた条件付きスタイリング（色分け、フォントサイズ、配置）
- **CORS対応**: フロントエンドアプリケーションとの連携が容易
- **包括的なテスト**: REST API、Web UI、設定の全てに対する自動テスト

このプロジェクトはFlask, Djangoにインスパイアされ、特にFlaskをベースとして開発されています。  
Python初学者にも扱いやすく、製造業やデータ分析業務での迅速なWebアプリ開発を目的としています。

## 📌 特徴
- `monitor-app startproject` で新しいプロジェクトを作成
- CSV データをSQLiteに簡単に変換
- Web UI でデータを表示・編集
- `Flask-SQLAlchemy` を使用し、SQLite / MySQL / PostgreSQL に対応
- Bootstrap を使用したスタイリッシュな UI
- `config.py` でカスタマイズ可能
- フル機能のREST APIを自動生成
- リアルタイムデータ更新機能

---

## 🎯 ユーザーメリット

### **⚡ 圧倒的な開発速度**
- **CSVから即座にWebアプリ化**: CSVファイルを配置するだけで、フル機能のWebアプリケーションが完成
- **ゼロコーディングでREST API**: プログラミング不要で、CRUD操作対応のREST APIが自動生成
- **既存システムとの即時連携**: MySQL・PostgreSQLの既存データベースに設定変更だけでWebインターフェース追加

### **🔧 高い柔軟性と拡張性**
- **マルチデータベース対応**: SQLite（開発・プロトタイプ）、MySQL・PostgreSQL（本格運用）を設定で切り替え
- **カスタマイズ可能なUI**: 条件付きスタイリング機能で、データの重要度や状態を視覚的に表現
- **JOIN機能**: 複数テーブルの関連データを統合表示、複雑なデータ構造も直感的に把握

### **👥 チームコラボレーション強化**
- **非エンジニアにも優しい**: Web UIで誰でもデータ操作が可能
- **API連携**: フロントエンドチームは REST API を使用して自由にUIを構築
- **リアルタイム更新**: 複数ユーザーが同時作業しても、常に最新データを表示

### **🛡️ 安全・確実な運用**
- **包括的なテストカバレッジ**: API、UI、設定の全機能に対する自動テスト
- **エラーハンドリング**: 不正なデータや操作に対する適切なエラー処理
- **CORS対応**: セキュアなクロスオリジン通信をサポート

### **💰 コスト削減効果**
- **開発工数削減**: 従来数日〜数週間の開発作業を数分に短縮
- **保守コスト軽減**: Flaskベースのシンプルな構造で長期運用も安心
- **学習コスト最小化**: Python初学者でも扱える設計

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

## 🔧 `config/config.py` の設定

プロジェクトの全設定は `config/config.py` で変更できます。以下は設定可能な全項目の詳細です。

### **📌 データベース設定**

#### **データベースタイプの選択**
```python
# 使用するデータベースの種類を指定
DB_TYPE = "sqlite"  # "sqlite" | "mysql" | "postgresql"
```

#### **SQLite設定**
```python
# SQLite のカスタムパス設定
CUSTOM_SQLITE_DB_PATH = None  # None の場合は instances/database.db を使用
# CUSTOM_SQLITE_DB_PATH = "/path/to/custom/database.db"  # カスタムパス指定
```

#### **MySQL設定**
```python
# DB_TYPE = "mysql" の場合に使用
MYSQL_USER = "root"
MYSQL_PASSWORD = "password"
MYSQL_HOST = "localhost"
MYSQL_PORT = "3306"
MYSQL_DB = "monitor_app"
```

#### **PostgreSQL設定**
```python
# DB_TYPE = "postgresql" の場合に使用
POSTGRES_USER = "postgres"
POSTGRES_PASSWORD = "password"
POSTGRES_HOST = "localhost"
POSTGRES_PORT = "5432"
POSTGRES_DB = "monitor_app"
```

### **📌 設定の分離設計**

Monitor App は、**CRUD操作用**と**表示用**の設定を明確に分離しています：

- **`ALLOWED_TABLES`**: CRUD API操作用のテーブル定義
- **`VIEW_TABLES`**: 画面表示用のビュー定義  
- **`TABLE_CELL_STYLES`**: ビュー表示用のスタイリング設定

この分離により、データ操作のセキュリティを保ちつつ、表示の柔軟性を実現しています。

### **📌 CRUD操作設定（ALLOWED_TABLES）**

`ALLOWED_TABLES`は、REST API でCRUD操作可能なテーブルを定義します。セキュリティ上、ここに定義されていないテーブルは一切アクセスできません。

#### **ALLOWED_TABLESの役割**
- **セキュリティ**: 不正なテーブルアクセスを防止
- **REST API自動生成**: 定義されたテーブルのみCRUD APIが自動作成
- **入力検証**: POST/PUTリクエストでのカラム検証
- **データ構造定義**: プライマリーキー、外部キー情報の管理

#### **基本的なCRUD設定**
```python
ALLOWED_TABLES = {
    "users": {
        "columns": ["id", "name", "email"],  # CRUD操作対象カラム
        "primary_key": "id"                  # プライマリーキー
    },
    "products": {
        "columns": ["id", "name", "price"], 
        "primary_key": "id"
    },
    "orders": {
        "columns": ["id", "user_id", "product_id", "amount"],
        "primary_key": "id",
        "foreign_keys": {"user_id": "users.id", "product_id": "products.id"}
    }
}
```


### **📌 ビュー表示設定（VIEW_TABLES）**

`VIEW_TABLES`は、Web画面での表示専用のビューを定義します。複雑なJOIN、集計、フィルタリングなどが可能です。

#### **VIEW_TABLESの特徴**
- **表示専用**: CRUD操作は行わず、表示のみ
- **柔軟なクエリ**: JOIN、集計、サブクエリなど自由に記述可能
- **メタデータ対応**: タイトル、説明文の設定
- **カラム名制御**: AS句による表示名の制御

#### **基本的なビュー設定**
```python
VIEW_TABLES = {
    "users_view": {
        "query": "SELECT id, name, email FROM users",
        "title": "ユーザー一覧",
        "description": "システムに登録されているユーザーの一覧"
    },
    "products_view": {
        "query": "SELECT id, name, price FROM products", 
        "title": "商品一覧",
        "description": "システムに登録されている商品の一覧"
    },
    "orders_summary": {
        "query": """
            SELECT 
                orders.id, 
                users.name AS user_name, 
                products.name AS product_name, 
                orders.amount
            FROM orders
            JOIN users ON orders.user_id = users.id
            JOIN products ON orders.product_id = products.id
        """,
        "title": "注文サマリー",
        "description": "ユーザー名と商品名を含む注文の詳細一覧"
    }
}
```

#### **高度なビュー例（集計・サブクエリ）**
```python
VIEW_TABLES = {
    "sales_summary": {
        "query": """
            SELECT 
                users.name as user_name,
                COUNT(orders.id) as order_count,
                SUM(orders.amount * products.price) as total_sales,
                AVG(orders.amount * products.price) as avg_order_value
            FROM users
            LEFT JOIN orders ON users.id = orders.user_id
            LEFT JOIN products ON orders.product_id = products.id
            GROUP BY users.id, users.name
            HAVING order_count > 0
            ORDER BY total_sales DESC
        """,
        "title": "売上サマリー",
        "description": "ユーザー別の注文統計と売上集計"
    }
}
```

### **📌 ビュースタイリング設定（TABLE_CELL_STYLES）**

`TABLE_CELL_STYLES`は、ビュー表示時のセル外観を値に応じて動的に変更する高度な機能です。**VIEW_TABLESのビューのみ**に適用され、**実際のカラム名**（AS句で指定された名前）をキーとします。

#### **重要な設計原則**
- **ビュー専用**: `VIEW_TABLES`で定義されたビューにのみ適用
- **カラム名ベース**: クエリの実際の出力カラム名を使用
- **安定性**: データベースのカラム名変更に影響されにくい設計

#### **基本構造**
```python
TABLE_CELL_STYLES = {
    "ビュー名": {
        "実際のカラム名": {  # AS句で指定された名前または元のカラム名
            # 値による条件分岐
            "greater_than": {"value": 数値, "class": "CSSクラス"},
            "less_than": {"value": 数値, "class": "CSSクラス"}, 
            "equal_to": {"value": 値, "class": "CSSクラス"},
            
            # 表示スタイル設定
            "width": "幅%",
            "font_size": "サイズpx",
            "align": "配置",
            "bold": True/False
        }
    }
}
```

#### **実装例: カラム名ベースのスタイリング**
```python
TABLE_CELL_STYLES = {
    "products_view": {
        "price": {  # 実際のカラム名を使用
            # 1000以上は青背景（高価格商品）
            "greater_than": {"value": 1000, "class": "bg-primary text-white"},
            # 500未満は水色背景（低価格商品）
            "less_than": {"value": 500, "class": "bg-info text-dark"},
            # 750ちょうどは灰色背景（標準価格）
            "equal_to": {"value": 750, "class": "bg-secondary text-white"},
            "width": "20%",
            "align": "right",
            "bold": False
        }
    },
    "orders_summary": {
        "amount": {  # AS句なしの元カラム名
            "greater_than": {"value": 10, "class": "bg-danger text-white"},
            "less_than": {"value": 5, "class": "bg-warning text-dark"},
            "equal_to": {"value": 7, "class": "bg-success text-white"},
            "width": "15%",
            "font_size": "32px",
            "align": "center",
            "bold": True
        }
    }
}
```

#### **文字列による完全一致条件**
```python
TABLE_CELL_STYLES = {
    "orders": {
        "status": {
            "equal_to": [
                {"value": "完了", "class": "bg-success text-white"},
                {"value": "進行中", "class": "bg-warning text-dark"},
                {"value": "キャンセル", "class": "bg-danger text-white"},
                {"value": "保留", "class": "bg-secondary text-white"}
            ]
        }
    }
}
```

#### **表示スタイル詳細設定**
```python
TABLE_CELL_STYLES = {
    "sales": {
        "amount": {
            # 条件による色分け
            "greater_than": {"value": 1000000, "class": "bg-success text-white"},
            "less_than": {"value": 100000, "class": "bg-danger text-white"},
            
            # 表示スタイル
            "width": "20%",           # カラム幅（CSS width）
            "font_size": "24px",      # フォントサイズ
            "align": "right",         # テキスト配置
            "bold": True              # 太字にする
        }
    }
}
```

#### **使用可能なBootstrap CSSクラス**
```python
# 背景色クラス
"bg-primary"      # 青（重要）
"bg-secondary"    # グレー（普通）
"bg-success"      # 緑（成功・完了）
"bg-danger"       # 赤（危険・エラー）
"bg-warning"      # 黄（警告・注意）
"bg-info"         # 水色（情報）
"bg-light"        # 薄グレー
"bg-dark"         # 黒

# テキスト色クラス  
"text-white"      # 白文字（濃い背景用）
"text-dark"       # 黒文字（薄い背景用）
"text-primary"    # 青文字
"text-success"    # 緑文字
"text-danger"     # 赤文字
"text-warning"    # 黄文字
"text-info"       # 水色文字

# 組み合わせ例
"bg-success text-white"     # 緑背景に白文字
"bg-warning text-dark"      # 黄背景に黒文字
"bg-danger text-white"      # 赤背景に白文字
```

#### **align設定の選択肢**
```python
"align": "left"     # 左寄せ（デフォルト）
"align": "center"   # 中央揃え
"align": "right"    # 右寄せ（数値に推奨）
```

### **📌 アプリケーション設定**
```python
# 基本情報
APP_TITLE = "Monitor App"                                    # ブラウザタイトル
HEADER_TEXT = "📊 Monitor Dashboard"                         # ページヘッダー
FOOTER_TEXT = "© 2025 Monitor App - Powered by Flask & Bootstrap"  # フッター
FAVICON_PATH = "favicon.ico"                                 # ファビコン

# 動作設定
TABLE_REFRESH_INTERVAL = 2000           # テーブル自動更新間隔（ミリ秒）
SQLALCHEMY_TRACK_MODIFICATIONS = False  # SQLAlchemy変更追跡（通常はFalse）
```

### **📌 CSV設定**
```python
# CSV ファイルディレクトリ
CUSTOM_CSV_DIR = None  # None の場合は プロジェクト/csv/ を使用
# CUSTOM_CSV_DIR = "/data/csv_files"  # カスタムディレクトリ指定
```

## 🌐 REST API エンドポイント

Monitor App では、データベースの CRUD 操作とビュー表示の両方に対応した REST API が利用できます。

### **📌 API ドキュメント（Swagger UI）**

FastAPI ライクな自動生成ドキュメントが利用可能です：

- **`GET /docs`** - Swagger UI による対話的なAPI ドキュメント
- **`GET /apispec_1.json`** - OpenAPI 仕様書（JSON形式）

### **📌 利用可能なエンドポイント**

#### **🔹 テーブル情報**
- `GET /api/tables` - すべてのテーブルのスキーマ情報を取得

#### **🔹 テーブル操作（CRUD API）**
- `GET /api/<table_name>` - テーブルの全レコードを取得
- `GET /api/<table_name>/<id>` - 指定 ID のレコードを取得
- `POST /api/<table_name>` - 新しいレコードを作成
- `PUT /api/<table_name>/<id>` - 指定 ID のレコードを更新
- `DELETE /api/<table_name>/<id>` - 指定 ID のレコードを削除

#### **🔹 ビュー表示（Display API）**
- `GET /api/table/<table_name>` - テーブルの生データを取得（スタイル情報なし）
- `GET /api/view/<view_name>` - ビューデータを取得（スタイル情報付き）

### **📌 API 使用例**

#### **🔹 基本操作**

##### **1. API ドキュメントの確認**
```bash
# Swagger UI でAPI仕様を確認
curl -X GET http://localhost:9990/docs

# JSON形式のAPI仕様を取得
curl -X GET http://localhost:9990/apispec_1.json
```

##### **2. テーブル一覧の取得**
```bash
curl -X GET http://localhost:9990/api/tables
```

#### **🔹 CRUD操作**

##### **3. ユーザー一覧の取得（CRUD）**
```bash
curl -X GET http://localhost:9990/api/users
```

##### **4. 新しいユーザーの作成**
```bash
curl -X POST http://localhost:9990/api/users \
  -H "Content-Type: application/json" \
  -d '{"name": "田中太郎", "email": "tanaka@example.com"}'
```

##### **5. ユーザー情報の更新**
```bash
curl -X PUT http://localhost:9990/api/users/1 \
  -H "Content-Type: application/json" \
  -d '{"name": "田中次郎", "email": "tanaka.updated@example.com"}'
```

##### **6. ユーザーの削除**
```bash
curl -X DELETE http://localhost:9990/api/users/1
```

#### **🔹 表示専用API**

##### **7. テーブル生データの取得**
```bash
# スタイル情報なしの生データ
curl -X GET http://localhost:9990/api/table/users
```

##### **8. ビューデータの取得**
```bash
# スタイル情報付きのビューデータ
curl -X GET http://localhost:9990/api/view/users_view

# 注文サマリービューの取得
curl -X GET http://localhost:9990/api/view/orders_summary
```

### **📌 レスポンス形式**

#### **🔹 CRUD API レスポンス例**

##### **成功時（作成・更新）**
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

##### **エラー時**
```json
{
  "error": "Record not found"
}
```

#### **🔹 ビューAPI レスポンス例**

##### **ビューデータ取得**
```json
{
  "view_name": "orders_summary",
  "title": "注文サマリー",
  "description": "ユーザー名と商品名を含む注文の詳細一覧",
  "columns": ["id", "user_name", "product_name", "amount"],
  "data": [
    {
      "id": 1,
      "user_name": "田中太郎",
      "product_name": "Apple",
      "amount": 5
    }
  ],
  "cell_styles": {
    "amount": {
      "greater_than": {"value": 10, "class": "bg-danger text-white"},
      "less_than": {"value": 5, "class": "bg-warning text-dark"},
      "width": "15%",
      "font_size": "32px",
      "align": "center",
      "bold": true
    }
  }
}
```

### **📌 注意事項**

#### **🔹 CRUD API**
- `ALLOWED_TABLES` で定義されたテーブルのみ操作可能
- プライマリーキー（通常は `id`）は自動設定のため、POST リクエストでは送信不要
- 外部キー制約のあるテーブルでは、関連するレコードの存在を確認してから操作してください

#### **🔹 ビューAPI**
- `VIEW_TABLES` で定義されたビューのみアクセス可能
- ビューデータは表示専用（CRUD操作は不可）
- スタイル設定は `TABLE_CELL_STYLES` で定義されたビューのみ適用

#### **🔹 設計の分離**
- **CRUD操作**: `/api/<table_name>` → `ALLOWED_TABLES`
- **表示データ**: `/api/view/<view_name>` → `VIEW_TABLES` + `TABLE_CELL_STYLES`
- **生データ**: `/api/table/<table_name>` → `ALLOWED_TABLES`（スタイルなし）

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
