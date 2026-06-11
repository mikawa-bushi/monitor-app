# Monitor App v2 再設計ドキュメント — FastAPI + Pydantic によるスクラッチ設計

本ドキュメントは、現行の Flask 製 monitor-app を「もしスクラッチで作り直すなら」という観点で再設計したものである。機能要件(CSV→DB 自動化、宣言的設定、Web UI、CRUD REST API、3DB 対応)はすべて維持しつつ、現行実装の構造的な課題を解消する。

---

## 1. 設計方針

最重要価値は「**Python 初心者が CSV を置いて設定ファイルを 1 つ書くだけで動く**」こと。すべての設計判断は以下の 3 軸で行う。

1. **ユーザーが触るファイルは最小限** — 理想は `config.py` 1 ファイル + `csv/` フォルダのみ
2. **間違いは起動時に分かりやすく検出** — Pydantic バリデーションで typo・矛盾を実行前に報告
3. **フレームワーク本体はパッケージ内に閉じる** — ユーザープロジェクトへのソース複製を廃止し、`pip install -U monitor-app` だけで UI・API が更新される状態にする

### 現行実装の課題(再設計で解消するもの)

| 課題 | 現状 | v2 での解消 |
|---|---|---|
| 巨大な単一ファイル | `app.py` 564 行にルート・Swagger YAML・初期化が混在 | ルーター分割 + アプリファクトリ(§2, §5) |
| SQL インジェクションリスク | f-string による生 SQL 組み立て(カラム名は無保護) | SQLAlchemy Core 式ビルダに全面移行(§4) |
| 設定の型安全性なし | dict 定義の typo が実行時まで不明 | Pydantic モデル + `check` コマンド(§3, §8) |
| 環境変数未対応 | DB パスワードを config.py に直書き | pydantic-settings + `.env`(§3) |
| 認証なし | 全エンドポイント無防備 | オプトイン API キー(§9) |
| スキーマ破壊バグ | pandas `to_sql(if_exists="replace")` が FK 付きテーブル定義を上書き | 標準 csv + 設定駆動スキーマ(§6) |
| アップグレード不能 | `startproject` がソース全コピー(15 ファイル超) | 雛形のみ生成、本体はパッケージ参照(§8) |
| ログ体系なし | `print()` デバッグ | logging dictConfig(§9) |

---

## 2. 全体アーキテクチャ

### レイヤ構成(4 層)

```
CLI (Typer) ──┐
              ├─→ App Factory (create_app) ─→ API 層 (FastAPI Router)
              │                                  │
              │                              Service 層 (ビジネスロジック)
              │                                  │
              └─→ Importer ──────────────→  DB 層 (SQLAlchemy Core / Repository)
                                                 ↑
                                          設定層 (pydantic-settings + 宣言モデル)
```

### パッケージ構成

```
monitor_app/
├── __init__.py              # __version__、公開 API(MonitorConfig 等の再エクスポート)
├── cli.py                   # Typer CLI(startproject / runserver / import-csv / check)
├── main.py                  # create_app() アプリファクトリ(現 app.py の後継、~80 行)
│
├── settings/
│   ├── runtime.py           # AppSettings: 環境変数系設定(pydantic-settings、MONITOR_ プレフィックス)
│   ├── declarative.py       # TableDef / ViewDef / CellStyle / MonitorConfig(純 Pydantic モデル)
│   └── loader.py            # ユーザーの config.py を動的 import + バリデーション + エラー整形
│
├── db/
│   ├── engine.py            # create_engine / セッション管理(FastAPI Depends 用)
│   ├── registry.py          # TableRegistry: 設定 → SQLAlchemy Table オブジェクト構築・保持
│   └── repository.py        # TableRepository: CRUD(Core 式クエリビルダ、生 SQL 禁止)
│
├── services/
│   ├── crud_service.py      # CRUD 操作 + カスタム例外送出
│   ├── view_service.py      # ViewDef の SELECT 実行(読み取り専用ガード付き)
│   └── importer.py          # CSV インポートパイプライン(現 csv_to_db.py の後継)
│
├── api/
│   ├── deps.py              # Depends: get_session / get_config / verify_api_key
│   ├── errors.py            # カスタム例外 → JSON レスポンス変換(exception_handler)
│   ├── schemas.py           # レスポンス用 Pydantic スキーマ(TableDataResponse 等)
│   └── routers/
│       ├── crud.py          # /api/tables/{table} の CRUD
│       ├── views.py         # /api/views/{view} + SSE(/api/views/{view}/stream)
│       ├── meta.py          # /api/schema(スキーマ一覧)、/api/health
│       └── pages.py         # / と /table/{view} の HTML 配信(Jinja2)
│
├── web/
│   ├── templates/           # base.html / index.html / table.html(パッケージ内・コピーしない)
│   └── static/              # bootstrap、vue.global.prod.js、app.js(パッケージ内)
│
├── logging_conf.py          # ロギング設定(dictConfig、--debug で DEBUG レベル)
└── scaffold/                # startproject 用テンプレート(これだけがコピー対象)
    ├── config.py.jinja      # コメント豊富な設定ファイル雛形
    ├── .env.example
    └── csv/                 # サンプル CSV(users / products / orders)
```

### 責務の要点

- `main.py` の `create_app(config: MonitorConfig) -> FastAPI` がアプリファクトリ。現 `app.py` の最大の問題である「import 時にグローバル app が生成され DB 接続される」副作用を排除し、テスト時に設定を差し替え可能にする。
- API 層は「HTTP の関心事」のみを扱う。バリデーション・DB 操作は Service / Repository へ委譲。
- `web/` はパッケージ内リソースとして `importlib.resources` 経由で配信。ユーザープロジェクトに `templates/overrides/` があればそちらを優先する(カスタマイズの口は残す)。

---

## 3. 設定システム

設定を「実行環境」と「宣言的定義」の 2 層に分離する。

### (a) 実行環境設定 — `settings/runtime.py`

`pydantic-settings` の `BaseSettings`。環境変数 `MONITOR_*` および `.env` ファイルでオーバーライド可能。

```python
class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MONITOR_", env_file=".env")

    database_url: str = "sqlite:///instances/database.db"
    host: str = "127.0.0.1"      # 現行の 0.0.0.0 デフォルトは安全側に変更
    port: int = 9990
    api_key: str | None = None   # 設定時のみ認証有効(§9)
    log_level: str = "INFO"
    csv_dir: Path = Path("csv")
```

現行の `DB_TYPE` + 個別ホスト/ユーザー/パスワード変数から URI を組み立てるロジックは廃止し、標準的な `DATABASE_URL` 1 本に統一する。初心者向けには config.py 雛形のコメントで sqlite / mysql / postgresql それぞれの URL の書き方例を提示する。これにより「config.py へのパスワード直書き」と「環境ごとの設定差し替え不能」を同時に解消する。

### (b) 宣言的定義 — `settings/declarative.py`

ユーザーが書く `config.py` は「Pydantic モデルのインスタンスを組み立てる Python ファイル」とする。

```python
# ユーザーの config.py(scaffold が生成する雛形)
from monitor_app import MonitorConfig, TableDef, ViewDef, CellStyle, Threshold

config = MonitorConfig(
    app_title="Monitor App",
    header_text="📊 Monitor Dashboard",
    footer_text="© 2026 Monitor App",
    refresh_interval_ms=2000,
    tables={
        "users": TableDef(columns=["id", "name", "email"], primary_key="id"),
        "orders": TableDef(
            columns=["id", "user_id", "product_id", "amount"],
            primary_key="id",
            foreign_keys={"user_id": "users.id", "product_id": "products.id"},
        ),
    },
    views={
        "orders_summary": ViewDef(
            query="""
                SELECT o.id, u.name, p.name AS product, o.amount
                FROM orders o JOIN users u ON o.user_id = u.id
                              JOIN products p ON o.product_id = p.id
            """,
            title="注文サマリー",
            description="ユーザー・商品を JOIN した注文一覧",
            styles={
                "amount": CellStyle(
                    greater_than=Threshold(value=10, css_class="bg-danger text-white"),
                    align="right",
                    bold=True,
                ),
            },
        ),
    },
)
```

### 設計判断

- **dict → Pydantic モデル直書き**。typo は `ValidationError` として起動時に検出される。`loader.py` に v1 の dict 形式(`ALLOWED_TABLES` / `VIEW_TABLES` / `TABLE_CELL_STYLES`)を `MonitorConfig.model_validate()` で受ける後方互換ローダーを持たせ、既存 config.py をほぼそのまま渡しても動くようにする。
- **YAML / TOML 案は不採用**。理由: (1) 既存ユーザーの学習資産を活かせる、(2) IDE 補完が効く、(3) f-string でクエリを組み立てる等の柔軟性。ターゲットは「Python 初心者」であって「非 Python 利用者」ではない。
- **`TABLE_CELL_STYLES` のトップレベル辞書を廃止**し、`ViewDef.styles` に統合。現状の「ビュー名 → カラム名 → ルール」の 3 階層 dict は typo の温床であり、ViewDef に寄せれば関連設定が 1 か所にまとまる。
- **バリデーション内容**: primary_key が columns に含まれるか、foreign_keys の参照先テーブルの存在、`ViewDef.query` が SELECT/WITH で始まるかの簡易チェック、styles のカラム名チェック(クエリ実行前は静的に確定できないため警告レベル)。
- **エラーメッセージの日本語整形**: `loader.py` で `ValidationError` を捕捉し、「config.py の tables['orders']: primary_key 'idd' は columns に存在しません」のような形式で表示する。初心者向け価値の中核。

---

## 4. DB 層

### SQLAlchemy 2.0 Core 中心(ORM 不採用)

テーブルがユーザー設定で動的に決まるため、ORM クラスの静的定義ができない。動的に `type()` で ORM クラスを生成する手もあるが複雑さに見合わない。Core の `Table` オブジェクトで十分である。

- **`registry.py`**: 起動時に `TableDef` から `Table(name, metadata, Column(...), ...)` を構築(現 `csv_to_db.py` の `create_tables` 相当)。加えて `metadata.reflect()` で実 DB と突合し、設定と DB のズレを警告ログに出す。
- **`repository.py`**: クエリはすべて式ビルダで構築する。

```python
stmt = select(table)
stmt = insert(table).values(**filtered)
stmt = update(table).where(table.c[pk] == record_id).values(**filtered)
stmt = delete(table).where(table.c[pk] == record_id)
```

f-string による SQL 組み立てを全廃する。テーブル名・カラム名は `TableRegistry` に登録済みの `Table` / `Column` オブジェクトしか使えない構造にすることで、**ホワイトリスト検証を「コード規約」ではなく「型構造」で強制**する。これが現行実装に対する最大のセキュリティ改善である。

### ViewDef の生 SQL の扱い

`ViewDef.query` だけは生 SQL を許容する(JOIN・集計の自由度のため)。ただし多重防御を敷く:

1. `text()` によるバインドパラメータのみ許可
2. 実行前に `SELECT` / `WITH` 始まりを検証
3. 実行時は読み取り専用接続(SQLite は `PRAGMA query_only`、MySQL/PostgreSQL は `SET TRANSACTION READ ONLY`)

この SQL はユーザー自身が設定ファイルに書くものなので、目的は「インジェクション対策」ではなく「誤操作防止」である。

### マイグレーション: Alembic 不採用

- 現アプリのデータ源は CSV であり、インポート時にテーブルを再生成する運用が前提。マイグレーション履歴で守るべきデータがない。
- Alembic は初心者にとって最大級のつまずきポイント。導入コストが価値を上回る。
- 代替: `monitor-app import-csv` 時に「既存テーブルを置換します」の確認プロンプトを出し、追記モードの `--keep` オプションを用意する。スキーマ変更は「config.py を直して import し直す」運用で十分。
- 「Web UI / API から手で入れたデータは import-csv で消える」という現行の暗黙挙動を、ドキュメントとプロンプトで明示する。

---

## 5. API 設計

### ルーター分割とパス再編

| ルーター | パス | 現行からの変更 |
|---|---|---|
| crud.py | `GET/POST /api/tables/{table}`、`GET/PUT/DELETE /api/tables/{table}/{record_id}` | 現 `/api/<table>` は `/api/tables`(スキーマ一覧)と衝突リスクがあるため `tables/` 配下に移動 |
| views.py | `GET /api/views/{view}`、`GET /api/views/{view}/stream`(SSE) | 現 `/api/view/` → 複数形に統一 |
| meta.py | `GET /api/schema`、`GET /api/health` | 現 `/api/tables`(スキーマ一覧)は `/api/schema` に改名 |
| pages.py | `GET /`、`GET /table/{view}` | 変更なし(HTML 配信) |

v2.0 としてパスは非互換で割り切る。後方互換が必要になった場合のみ、旧パスに `deprecated=True` のリダイレクトルートを置く。

### Pydantic スキーマ(api/schemas.py)

動的テーブルなのでレコード本体は `dict[str, Any]` だが、エンベロープは型定義する。

```python
class TableDataResponse(BaseModel):
    table_name: str
    columns: list[str]
    data: list[dict[str, Any]]
    count: int

class ViewDataResponse(BaseModel):
    view_name: str
    title: str
    description: str
    columns: list[str]
    data: list[dict[str, Any]]
    cell_styles: dict[str, CellStyle]   # 設定モデルをそのまま再利用 → UI との契約が型で繋がる
```

POST / PUT の入力は `dict` で受け、Service 層で `TableDef.columns` によるフィルタと、レジストリの Column 型に基づく簡易型変換(int / float)を行う。「テーブルごとに `create_model()` で動的 Pydantic モデルを生成し OpenAPI に個別スキーマを出す」案は、実装の複雑化に対しユーザーメリットが薄いため初期版では見送る(将来の拡張ポイントとして余地のみ残す)。

### エラーハンドリング(api/errors.py)

```python
class MonitorAppError(Exception): ...
class TableNotFoundError(MonitorAppError): ...   # → 404
class RecordNotFoundError(MonitorAppError): ...  # → 404
class InvalidPayloadError(MonitorAppError): ...  # → 422
class QueryExecutionError(MonitorAppError): ...  # → 500(詳細はログのみ)
```

`app.add_exception_handler()` で一括変換し、レスポンス形式を `{"detail": {"code": "TABLE_NOT_FOUND", "message": "..."}}` に統一する。現行の `except Exception as e: return str(e), 500`(内部情報の漏えい)を全廃し、500 系はリクエスト ID のみ返してスタックトレースはログに送る。

### OpenAPI / Swagger UI

flasgger の手書き YAML docstring(現 app.py の半分以上を占める)は全削除する。FastAPI の自動生成 + `response_model` + docstring 要約だけで `/docs`(Swagger UI)と `/redoc` が得られる。これだけで約 300 行が消える。

---

## 6. CSV インポートパイプライン

### pandas をやめて標準 csv + 自前型推論

判断理由:

- pandas は依存として重く(インストールサイズ・初心者環境でのビルド失敗リスク)、現状の用途は `read_csv` + `to_sql` のみで pandas の価値をほぼ使っていない。
- `to_sql(if_exists="replace")` は pandas 側の型推論でスキーマを作り直すため、`create_tables()` が作った FK 付きテーブルを破壊する(現行実装のバグ温床)。
- v2 では `csv.DictReader` で読み、`TableRegistry` の Column 型に従って値変換し、`insert().values()` でバッチ投入する。**スキーマの真実は常に config.py 側**となり一貫する。

### 型推論戦略(2 段構え)

1. **明示優先**: `TableDef.columns` を `list[str]`(従来互換: `_id` → int、`price`/`amount` → float の命名規則推論)と `dict[str, Literal["int", "float", "str", "bool", "date"]]`(明示指定)の両対応にする。Pydantic の Union で受けて正規化。
2. **サンプリング推論**: 命名規則に合致しない列は先頭 100 行を見て int → float → date → str の順に試行。命名規則だけに頼る現方式(`"price" in col` のため `priceless` という列名も Float になる)より堅牢。

インポートは `services/importer.py` に置き、結果レポート(テーブル名、投入行数、型変換に失敗しスキップした行)を構造化ログと CLI 出力で返す。失敗行は「csv/orders.csv 3 行目: amount='abc' を float に変換できません」と具体的に出す(初心者向け価値)。

---

## 7. Web UI とリアルタイム更新

### Jinja2 + 同梱 Vue 継続(ビルド工程なし、SPA 化は不採用)

Vite 等のフロントエンドビルドを導入すると Node.js 環境が必要になり、ターゲットユーザーの導入障壁が跳ね上がる。現方式(Jinja2 でシェルを出し、Vue 3 + fetch でデータ取得)はビルド不要で十分機能している。改善点のみ:

- table.html 内のインライン JS(約 90 行)を `web/static/js/app.js` に分離。Jinja2 からは `<div id="app" data-view-name="...">` の data 属性で初期値を渡し、テンプレートと JS を分離する。
- axios 依存を削除し標準 `fetch` に変更(依存削減)。
- CDN 参照(unpkg)をパッケージ同梱の `vue.global.prod.js` に変更。工場のモニタ用途などオフライン環境で動作させるため。
- 現行の未使用ファイル `refresh_table.js` に相当するものは作らない。

### リアルタイム更新: SSE 推奨、ポーリングをフォールバックに

| 方式 | 利点 | 欠点 |
|---|---|---|
| ポーリング(現行) | 単純、プロキシ問題なし | 無駄なリクエスト、更新遅延 = interval |
| **SSE(推奨)** | FastAPI で実装容易(`StreamingResponse` / sse-starlette)、HTTP のまま、クライアントは `EventSource` 数行、自動再接続内蔵 | 単方向のみ(本用途は単方向で十分) |
| WebSocket | 双方向 | 双方向が不要。再接続・プロキシ・スケールの考慮が増え過剰 |

推奨実装: `/api/views/{view}/stream` がサーバー側で `refresh_interval_ms` 間隔に DB を再クエリし、**結果のハッシュが変わったときのみ**イベントを送出する(差分がなければネットワーク・再描画コストゼロ)。クライアントは `EventSource` が使えない/失敗した場合に従来ポーリングへフォールバックする。設定 `refresh_mode: Literal["sse", "polling"] = "sse"` で切替可能にする。

注意: 外部プロセスによる SQLite 書き込みの検知が目的である以上、サーバー側は結局ポーリングになる。SSE の利点は「クライアント↔サーバー間の効率と即時性」に限定されることをドキュメントに明記し、過大評価しない。

---

## 8. CLI(Typer)

```
monitor-app startproject <name>   # 雛形生成(下記)
monitor-app runserver [--host --port --reload --import-csv]
monitor-app import-csv [--keep]
monitor-app check                 # config.py のバリデーションのみ実行(初心者向け lint)
```

- **Typer 採用理由**: 型ヒントベースで click より宣言的、FastAPI と作者・思想が同じで学習が連続する。
- `runserver` は `uvicorn.run("monitor_app.main:create_app", factory=True, ...)` を呼ぶ。現行の `--debug` は `--reload` + `log_level=DEBUG` に分解。
- **`check` コマンドを新設**。設定 typo を起動前に検出できる、初心者向けの最重要コマンド。

### startproject の脱・全コピー

現行実装は app.py / cli.py / csv_to_db.py / テンプレート / 静的ファイルの全部(15 ファイル超)をユーザープロジェクトへコピーしており、パッケージ更新が一切反映されない。v2 で生成するのは以下のみ:

```
myproject/
├── config.py        # scaffold/config.py.jinja から生成(コメント・サンプル定義入り)
├── .env.example     # DATABASE_URL / MONITOR_API_KEY の記入例
├── csv/             # サンプル CSV 3 つ(users / products / orders — 初回起動から動く状態)
├── instances/       # .gitkeep のみ
└── README.md        # 3 ステップの使い方
```

アプリ本体・テンプレート・静的ファイルはパッケージ内から配信する。テンプレートをカスタマイズしたい上級者向けには、`monitor-app eject-templates`(templates/ をプロジェクトへ展開しオーバーライドを有効化する任意コマンド)を将来オプションとして残す。

---

## 9. 認証・ロギング・テスト

### 認証(オプトイン API キー)

- `MONITOR_API_KEY` が**未設定なら認証なし**(現行同等、ローカル用途を壊さない)。設定されたら書き込み系(POST / PUT / DELETE)に `X-API-Key` ヘッダ必須。読み取りも保護する `MONITOR_PROTECT_READS=true` を別途用意。
- 実装は `api/deps.py` の `Security(APIKeyHeader(...))` 依存 1 つ。比較は `secrets.compare_digest`。
- ユーザー管理・JWT・OAuth は明確に**スコープ外**(過剰設計の回避)。それ以上の要件はリバースプロキシ側の仕事とする。
- CORS は現行の全開放(`CORS(app)`)をやめ、デフォルト同一オリジンのみ・設定で開放可能に変更。

### ロギング

- `logging_conf.py` の `dictConfig` で `monitor_app.*` ロガー階層を構成。デフォルト INFO、`print()` 全廃。
- uvicorn のアクセスログをそのまま利用。SQL ログは `--debug` 時のみ `sqlalchemy.engine` を INFO に。
- ログフォーマットは「時刻 LEVEL メッセージ」のシンプル形式(JSON 構造化ログはオプション設定に留める)。

### テスト戦略

- **単体**: `settings/declarative.py` のバリデーション(typo が仕様通り失敗するか)、importer の型推論、repository のクエリ構築。
- **API 統合**: `TestClient` + in-memory SQLite + テスト用 `MonitorConfig` フィクスチャを `create_app()` に注入。アプリファクトリ化の最大の配当はここ。CRUD 全パス・404/422 系・API キー有無を網羅(現 tests/test_api.py の検証観点を引き継ぐ)。
- **E2E 軽量**: `startproject` → `check` → `import-csv` → サーバー起動確認を tmp_path 上で実行する CLI テスト(`typer.testing.CliRunner`)。

---

## 10. 不採用判断の一覧(過剰設計の回避)

| 採用しないもの | 理由 |
|---|---|
| Alembic(マイグレーション) | CSV 置換運用に履歴管理の概念が不要。初心者最大のつまずき要因 |
| フロントエンドビルド(Vite / npm) | Node 環境要求は導入障壁。同梱 Vue + fetch で十分 |
| 非同期 DB(asyncpg / aiosqlite) | 同期 SQLAlchemy + FastAPI のスレッドプールで性能十分。async DB はデバッグ難度が上がる |
| テーブルごとの動的 Pydantic モデル(per-table OpenAPI スキーマ) | 実装複雑度に見合う利用者価値なし(将来拡張の余地のみ確保) |
| WebSocket / メッセージブローカー | 単方向更新には SSE で十分 |
| ユーザー認証・RBAC | API キー 1 本で止める。それ以上はリバースプロキシの仕事 |
| YAML / TOML 設定 | Python config.py 継続(IDE 補完・既存互換・クエリ組み立ての柔軟性) |

**守るもの**: 「3 コマンドで動く」体験。

```sh
pip install monitor-app
monitor-app startproject demo
cd demo && monitor-app runserver --import-csv
```

サンプル CSV 同梱により、初回起動から画面にデータが表示される状態を維持する。

---

## 11. 実装ステップ(依存順)とリスク

1. **settings/**: declarative.py(Pydantic モデル)→ runtime.py → loader.py(v1 dict 後方互換含む)。全レイヤの土台
2. **db/**: engine.py → registry.py(TableDef → Table 構築)→ repository.py(Core 式 CRUD)
3. **services/**: importer.py(標準 csv 版)→ crud_service.py / view_service.py
4. **api/**: errors.py → schemas.py → deps.py → routers(crud → views → meta → pages)
5. **main.py**: create_app() ファクトリ統合、exception_handler・静的ファイル・Jinja2 登録
6. **web/**: テンプレート移植(JS 分離、SSE クライアント + ポーリングフォールバック)
7. **cli.py**: Typer 移植、startproject 簡素化、check コマンド
8. **テスト・ドキュメント**: フィクスチャ整備 → API / CLI / 設定テスト → README・移行ガイド

### 先行検証が必要なリスク

- (a) SSE の `StreamingResponse` と uvicorn のワーカーモデルの相性 — ステップ 6 の前にスパイクで確認
- (b) `importlib.resources` によるテンプレート / 静的ファイル配信のパッケージング(poetry の include 設定)— ステップ 5 で wheel をビルドして確認
- (c) v1 設定の後方互換ローダーの網羅性 — 既存の config.py をそのまま食わせるテストを最初に書く

---

## 12. v1 → v2 移行ガイド(config.py 対応表)

| v1(現行) | v2 | 備考 |
|---|---|---|
| `DB_TYPE` + `CUSTOM_SQLITE_DB_PATH` + ホスト/ユーザー/パスワード変数 | 環境変数 `MONITOR_DATABASE_URL`(または `.env`) | パスワードを config.py から排除 |
| `ALLOWED_TABLES = {"users": {"columns": [...], "primary_key": "id"}}` | `tables={"users": TableDef(columns=[...], primary_key="id")}` | dict のままでも後方互換ローダーで受理 |
| `ALLOWED_TABLES[...]["foreign_keys"]` | `TableDef(foreign_keys={...})` | 同形式 |
| `VIEW_TABLES = {"v": {"query": ..., "title": ..., "description": ...}}` | `views={"v": ViewDef(query=..., title=..., description=...)}` | |
| `TABLE_CELL_STYLES[view][col]` | `ViewDef.styles[col] = CellStyle(...)` | トップレベル辞書を廃止しビュー定義に統合 |
| `TABLE_CELL_STYLES` の `greater_than: {"value": x, "class": y}` | `CellStyle(greater_than=Threshold(value=x, css_class=y))` | `class` は予約語のため `css_class` に改名 |
| `APP_TITLE` / `HEADER_TEXT` / `FOOTER_TEXT` / `FAVICON_PATH` | `MonitorConfig(app_title=..., header_text=..., footer_text=..., favicon_path=...)` | |
| `TABLE_REFRESH_INTERVAL`(ms) | `MonitorConfig(refresh_interval_ms=...)` + `refresh_mode="sse" \| "polling"` | |
| API パス `/api/<table>` | `/api/tables/{table}` | パス衝突解消 |
| API パス `/api/view/<view>` | `/api/views/{view}`(+ `/stream` で SSE) | |
| API パス `/api/tables`(スキーマ一覧) | `/api/schema` | |
| `python app.py runserver` | `monitor-app runserver` | アプリ本体がプロジェクト内に存在しないため CLI 経由に統一 |
