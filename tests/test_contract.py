"""公開契約(後方互換)のテスト。

``tests/contract/baseline/*.json`` を v2.3.1 時点の現行コードから生成した「既存の
利用者との約束」とみなし、現行コードがそれを満たし続けることを検証する。追加(新しい
パス・パラメータ・プロパティ・コマンド・オプション・設定項目)は常に許し、削除と
互換性のない変更だけを失敗にする。比較ルールは ``tests/contract/compat.py``、
抽出は ``tests/contract/snapshot.py`` にある。

契約を意図的に変えるときは、合意のうえで ``uv run python tests/contract/_make_baseline.py``
を実行してベースラインを再生成し、差分をレビューすること(テストを通すためだけに
再生成しない)。
"""

from __future__ import annotations

import csv
import importlib
import importlib.resources
import json
import textwrap
import warnings
from pathlib import Path

import anyio
import pytest
from contract import compat, helpers, snapshot
from fastapi.testclient import TestClient

from monitor_app import MonitorConfig, TableDef, Threshold
from monitor_app.integrations import netdiag, network_checker, procdiag
from monitor_app.main import create_app
from monitor_app.settings.loader import load_config
from monitor_app.settings.runtime import AppSettings

#: 既定値を意図的に変える AppSettings のフィールド: フィールド名 → (旧既定値, 新既定値)。
#: ここに載ったフィールドは、旧・新どちらの既定値でも契約違反にしない(移行期間中は
#: 両方を許す)。項目を足すときは CHANGELOG と移行手順への記載を必ずセットにすること。
INTENTIONAL_DEFAULT_CHANGES = {
    # CSV の文字コード自動判別(utf-8-sig を試し、読めなければ cp932)を既定にする(DX-04)。
    "csv_encoding": ("utf-8-sig", "auto"),
}

#: OpenAPI に載らない(include_in_schema=False)ルート → (実際に叩く URL, Content-Type)。
NON_OPENAPI_ROUTES = {
    "/": ("/", "text/html"),
    "/table/{view_name}": ("/table/users_view", "text/html"),
    "/form/{table_name}": ("/form/orders", "text/html"),
    "/kiosk": ("/kiosk", "text/html"),
    "/login": ("/login", "text/html"),
    "/static": ("/static/css/app.css", "text/css"),
    "/docs": ("/docs", "text/html"),
    "/redoc": ("/redoc", "text/html"),
    "/openapi.json": ("/openapi.json", "application/json"),
}

_RESPONSE_SCENARIOS = [
    s["name"] for s in snapshot.load_baseline("responses")["scenarios"]
]


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """開発者の MONITOR_* や作業ディレクトリの .env を観測に持ち込まない。"""
    helpers.isolate_from_host(monkeypatch, tmp_path)


@pytest.fixture(scope="module")
def contract_client():
    """読み取り専用の確認に使う、ゴールデンデータ投入済みのクライアント。"""
    app = snapshot.create_contract_app()
    client = snapshot.seeded_client(app)
    yield app, client
    client.close()
    # インメモリ DB(StaticPool の単一の接続)は Services.close() が残すため、ここで閉じる
    app.state.services.db.engine.dispose()


def _report(errors: list[str]) -> str:
    return "公開契約の違反:\n  - " + "\n  - ".join(errors)


def _settings(**overrides) -> AppSettings:
    return AppSettings(_env_file=None, database_url="sqlite://", **overrides)


# ---------------------------------------------------------------------------
# (a) OpenAPI / (b) OpenAPI に載らないルート
# ---------------------------------------------------------------------------
def test_openapi_is_backward_compatible():
    """操作・パラメータ・リクエストボディ・応答の型・認証方式が後方互換である。"""
    base = snapshot.load_baseline("openapi")
    cur = snapshot.openapi_contract(snapshot.create_contract_app().openapi())
    errors = compat.openapi_violations(base, cur)
    assert not errors, _report(errors)


@pytest.mark.parametrize("route", list(NON_OPENAPI_ROUTES))
def test_non_openapi_routes_are_still_served(route, contract_client):
    _app, client = contract_client
    url, content_type = NON_OPENAPI_ROUTES[route]
    res = client.get(url)
    assert res.status_code == 200, f"{route}({url}): {res.status_code} {res.text[:200]}"
    assert snapshot.mime_type(res.headers.get("content-type")) == content_type


# ---------------------------------------------------------------------------
# (c) CLI
# ---------------------------------------------------------------------------
def test_cli_is_backward_compatible():
    """コマンド・オプションのフラグ文字列・is_flag・既定値・位置引数が残っている。"""
    errors = compat.cli_violations(
        snapshot.load_baseline("cli"), snapshot.cli_contract()
    )
    assert not errors, _report(errors)


# ---------------------------------------------------------------------------
# (d) 実行時設定(AppSettings / MONITOR_*)
# ---------------------------------------------------------------------------
def test_settings_keep_env_names_and_defaults():
    base = snapshot.load_baseline("settings")
    cur = snapshot.settings_fields()
    errors = []
    if cur["env_prefix"] != base["env_prefix"]:
        errors.append(
            f"環境変数のプレフィックス {base['env_prefix']} → {cur['env_prefix']}"
        )
    for name, bfield in base["fields"].items():
        cfield = cur["fields"].get(name)
        if cfield is None:
            errors.append(f"設定 {name}({bfield['env']})がなくなりました")
            continue
        if cfield["env"] != bfield["env"]:
            errors.append(f"設定 {name} の環境変数名 {bfield['env']} → {cfield['env']}")
        if cfield["required"]:
            errors.append(
                f"設定 {name} が必須になりました(未設定の環境で起動できなくなる)"
            )
        allowed = [bfield.get("default")]
        if name in INTENTIONAL_DEFAULT_CHANGES:
            old, new = INTENTIONAL_DEFAULT_CHANGES[name]
            assert old == bfield.get("default"), (
                f"INTENTIONAL_DEFAULT_CHANGES[{name!r}] の旧既定値 {old!r} が"
                f"ベースライン {bfield.get('default')!r} と一致しません"
            )
            allowed = [old, new]
        if cfield.get("default") not in allowed:
            errors.append(
                f"設定 {name} の既定値 {bfield.get('default')!r}"
                f" → {cfield.get('default')!r}"
                "(意図的なら INTENTIONAL_DEFAULT_CHANGES に記載する)"
            )
    for name, cfield in cur["fields"].items():
        if name not in base["fields"] and cfield["required"]:
            errors.append(f"新しい設定 {name} が必須です(既定値が必要)")
    assert not errors, _report(errors)


def _same_setting(observed, expected) -> bool:
    if isinstance(observed, str) and isinstance(expected, str):
        return observed.lower() == expected.lower()  # 大文字小文字の正規化は許す
    return observed == expected


def test_monitor_env_vars_are_honoured():
    """各 ``MONITOR_<FIELD>`` 環境変数が対応するフィールドに反映される。"""
    fields = snapshot.load_baseline("settings")["fields"]
    probes = {f["env"]: f["probe"]["raw"] for f in fields.values() if "probe" in f}
    observed = snapshot.read_settings(probes)
    errors = [
        f"{field['env']}={field['probe']['raw']!r} が {name} に反映されません"
        f"(値: {observed.get(name)!r})"
        for name, field in fields.items()
        if "probe" in field
        and not _same_setting(observed.get(name), field["probe"]["expected"])
    ]
    assert not errors, _report(errors)


def test_dotenv_file_in_working_directory_is_honoured(tmp_path):
    (tmp_path / ".env").write_text(
        "MONITOR_PORT=9876\nMONITOR_AUDIT_ENABLED=true\n", encoding="utf-8"
    )
    settings = AppSettings()
    assert settings.port == 9876
    assert settings.audit_enabled is True


def test_unknown_settings_do_not_stop_startup(tmp_path, monkeypatch):
    """.env に他ツールの変数や未知の ``MONITOR_*`` があっても起動を止めない(警告は可)。"""
    (tmp_path / ".env").write_text(
        "OTHER_TOOL_SETTING=1\nMONITOR_NOT_A_SETTING=1\n", encoding="utf-8"
    )
    monkeypatch.setenv("MONITOR_ANOTHER_UNKNOWN", "x")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        AppSettings()  # 例外(ValidationError)にならなければよい


# ---------------------------------------------------------------------------
# (e) 宣言モデル(config.py で書くもの)
# ---------------------------------------------------------------------------
def test_declarative_models_are_backward_compatible():
    """既存フィールドの既定値・別名・選択肢が保たれ、新フィールドは任意である。"""
    base = snapshot.load_baseline("models")
    cur = snapshot.models_contract()
    errors = []
    for missing in sorted(set(base["column_types"]) - set(cur["column_types"])):
        errors.append(f"列の論理型 '{missing}' を受け付けなくなりました")
    for model, bmodel in base["models"].items():
        cmodel = cur["models"].get(model)
        if cmodel is None:
            errors.append(f"{model} がなくなりました")
            continue
        for name, bfield in bmodel["fields"].items():
            cfield = cmodel["fields"].get(name)
            where = f"{model}.{name}"
            if cfield is None:
                errors.append(f"{where} がなくなりました")
                continue
            if cfield["required"] and not bfield["required"]:
                errors.append(f"{where} が必須になりました")
            if not bfield["required"] and cfield.get("default") != bfield["default"]:
                errors.append(
                    f"{where} の既定値 {bfield['default']!r}"
                    f" → {cfield.get('default')!r}"
                )
            renamed = cfield["alias"] != bfield["alias"]
            if renamed and not (bfield["alias"] is None and cmodel["populate_by_name"]):
                errors.append(
                    f"{where} の config 上の名前(alias) {bfield['alias']!r}"
                    f" → {cfield['alias']!r}"
                )
            if "choices" in bfield and "choices" in cfield:
                for value in sorted(set(bfield["choices"]) - set(cfield["choices"])):
                    errors.append(f"{where} が値 {value!r} を受け付けなくなりました")
        for name, cfield in cmodel["fields"].items():
            if name not in bmodel["fields"] and cfield["required"]:
                errors.append(
                    f"新しいフィールド {model}.{name} が必須です"
                    "(既存の config.py が読めなくなる)"
                )
        if bmodel["populate_by_name"] and not cmodel["populate_by_name"]:
            errors.append(f"{model} がフィールド名での指定を受け付けなくなりました")
        for member in sorted(set(bmodel["members"]) - set(cmodel["members"])):
            errors.append(f"{model}.{member}() がなくなりました")
    assert not errors, _report(errors)


def test_threshold_accepts_both_class_alias_and_field_name():
    by_alias = Threshold(value=1, **{"class": "bg-danger"})
    by_name = Threshold(value=1, css_class="bg-danger")
    assert by_alias == by_name
    assert by_alias.model_dump(by_alias=True) == {"value": 1.0, "class": "bg-danger"}


def test_unknown_config_keys_still_load():
    """config.py の未知キー(typo を含む)は 2.x では読み込みを止めない。

    警告を出すのは可。``extra="forbid"`` への切り替えは既存の config.py を壊すため、
    メジャーバージョンまで行わない(DX の監査でも警告からの段階導入が推奨)。
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        config = MonitorConfig(
            refresh_intrval_ms=500,
            tables={"t": TableDef(columns=["id", "name"], primay_key="id")},
        )
    # typo のキーは無視され、正しいフィールドは既定値のまま
    assert config.refresh_interval_ms == MonitorConfig().refresh_interval_ms
    assert config.tables["t"].primary_key == "id"


def test_default_favicon_path_points_to_a_packaged_static_file(contract_client):
    """``MonitorConfig.favicon_path`` の既定値が同梱の静的ファイルを指している。"""
    _app, client = contract_client
    assert client.get(f"/static/{MonitorConfig().favicon_path}").status_code == 200


# ---------------------------------------------------------------------------
# (f) 公開 API
# ---------------------------------------------------------------------------
def test_public_names_are_still_exported():
    base = snapshot.load_baseline("public_api")
    errors = []
    for module_name, names in base["all"].items():
        module = importlib.import_module(module_name)
        for name in sorted(set(names) - set(getattr(module, "__all__", []))):
            errors.append(f"{module_name}.__all__ から {name} が消えました")
    for module_name, names in base["imports"].items():
        try:
            module = importlib.import_module(module_name)
        except ImportError as exc:
            errors.append(f"{module_name} を import できません: {exc}")
            continue
        for name in names:
            if not hasattr(module, name):
                errors.append(f"from {module_name} import {name} ができなくなりました")
    assert not errors, _report(errors)


def test_public_function_signatures_are_compatible():
    errors = []
    for target, bsig in snapshot.load_baseline("public_api")["signatures"].items():
        try:
            func = snapshot.resolve(target)
        except (ImportError, AttributeError):
            errors.append(f"{target} がなくなりました")
            continue
        errors += compat.signature_violations(
            target, bsig, snapshot.signature_contract(func)
        )
    assert not errors, _report(errors)


def test_logger_names_are_kept():
    """利用者のログ設定(フィルタ・レベル指定)が参照するロガー名が残っている。"""
    base = snapshot.load_baseline("public_api")["loggers"]
    missing = sorted(set(base) - set(snapshot.monitor_logger_names()))
    assert not missing, f"ロガーがなくなりました: {missing}"


def test_console_script_entry_point_is_kept():
    current = snapshot.console_scripts()
    if not current:
        pytest.skip("monitor-app が配布物としてインストールされていません")
    for name, target in snapshot.load_baseline("public_api")["console_scripts"].items():
        assert current.get(name) == target, f"コマンド {name} → {current.get(name)}"


def _write_config(path: Path, title: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        textwrap.dedent(f"""
            from monitor_app import MonitorConfig, TableDef

            config = MonitorConfig(
                app_title={title!r},
                tables={{"items": TableDef(columns=["id", "name"])}},
            )
            """),
        encoding="utf-8",
    )
    return path


def test_app_factory_honours_monitor_config_path(tmp_path, monkeypatch):
    from monitor_app.main import app_factory

    config_path = _write_config(tmp_path / "elsewhere" / "my_config.py", "Factory")
    monkeypatch.setenv("MONITOR_CONFIG_PATH", str(config_path))
    monkeypatch.setenv(
        "MONITOR_DATABASE_URL", helpers.sqlite_url(tmp_path / "factory.db")
    )
    client = TestClient(app_factory())
    assert client.get("/openapi.json").json()["info"]["title"] == "Factory"
    assert set(client.get("/api/schema").json()["tables"]) == {"items"}


def test_app_factory_defaults_to_config_py_in_working_directory(tmp_path, monkeypatch):
    from monitor_app.main import app_factory

    _write_config(tmp_path / "config.py", "Working Directory")
    monkeypatch.setenv(
        "MONITOR_DATABASE_URL", helpers.sqlite_url(tmp_path / "factory.db")
    )
    client = TestClient(app_factory())
    assert client.get("/openapi.json").json()["info"]["title"] == "Working Directory"


# ---------------------------------------------------------------------------
# (g) 実レスポンスの形 / (h) SSE のフレーム形式
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("scenario", _RESPONSE_SCENARIOS)
def test_response_contract(scenario):
    """ベースライン記録時と同じリクエスト列を再生し、応答が後方互換であることを確かめる。

    JSON はキー集合がベースラインの上位集合で各値の型が互換であればよい(キーの追加は可)。
    """
    data = next(
        s
        for s in snapshot.load_baseline("responses")["scenarios"]
        if s["name"] == scenario
    )
    app = snapshot.create_contract_app(data["settings"])
    client = snapshot.seeded_client(app, api_key=data["settings"].get("api_key"))
    errors = []
    for entry in data["requests"]:
        spec = entry["request"]
        observed = snapshot.perform(app, client, spec)
        where = f"[{spec['id']}] {spec['method']} {spec['path']}"
        errors += compat.response_violations(where, entry["expected"], observed)
    client.close()
    assert not errors, _report(errors)


def test_health_reports_ok(contract_client):
    _app, client = contract_client
    res = client.get("/api/health")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"  # キーの追加(版情報など)は可


@pytest.mark.parametrize(
    "method, path, body, status, code",
    [
        ("GET", "/api/views/ghost", None, 404, "VIEW_NOT_FOUND"),
        ("GET", "/api/tables/ghost", None, 404, "TABLE_NOT_FOUND"),
        ("POST", "/api/tables/users", {"junk": 1}, 422, "INVALID_PAYLOAD"),
        ("POST", "/api/tables/products", {"price": "abc"}, 422, "INVALID_PAYLOAD"),
    ],
)
def test_error_envelope(contract_client, method, path, body, status, code):
    _app, client = contract_client
    res = client.request(method, path, json=body)
    assert res.status_code == status
    detail = res.json()["detail"]
    assert detail["code"] == code
    assert isinstance(detail["message"], str) and detail["message"]


def test_query_validation_error_keeps_fastapi_shape(contract_client):
    _app, client = contract_client
    res = client.get("/api/views/users_view", params={"limit": 0})
    assert res.status_code == 422
    assert {"loc", "msg", "type"} <= set(res.json()["detail"][0])


def test_sse_first_event_carries_the_view_payload(contract_client):
    app, client = contract_client
    payload = client.get("/api/views/orders_summary").json()
    status, headers, raw = anyio.run(
        snapshot.read_first_sse_event, app, "/api/views/orders_summary/stream"
    )
    assert status == 200
    assert snapshot.mime_type(headers.get("content-type")) == "text/event-stream"
    event = snapshot.parse_sse(raw)[0]
    assert event["event"] == "message"
    data = json.loads(event["data"])
    assert set(payload) <= set(data)
    assert "alerts" in data
    assert data["data"] == payload["data"]


def test_export_xlsx_or_csv_fallback(contract_client):
    _app, client = contract_client
    res = client.get("/api/views/products_view/export", params={"format": "xlsx"})
    assert res.status_code == 200
    content_type = snapshot.mime_type(res.headers.get("content-type"))
    try:
        import openpyxl  # noqa: F401
    except ImportError:
        assert content_type == "text/csv"  # openpyxl 未導入なら CSV にフォールバック
    else:
        assert content_type == (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        assert res.content[:2] == b"PK"


# ---------------------------------------------------------------------------
# (i) config の読み込み(v2 scaffold・v1 dict 形式・連携プリセット)
# ---------------------------------------------------------------------------
def test_packaged_scaffold_config_serves_every_view_and_kpi():
    """同梱 scaffold(startproject が配る config.py と CSV)がそのまま動く。"""
    scaffold = Path(str(importlib.resources.files("monitor_app") / "scaffold"))
    config = load_config(scaffold / "config.py")
    assert isinstance(config, MonitorConfig)
    assert config.tables and config.views
    client = TestClient(create_app(config, _settings()), raise_server_exceptions=False)
    for table in config.tables:
        csv_path = scaffold / "csv" / f"{table}.csv"
        if csv_path.exists():
            with csv_path.open(encoding="utf-8-sig", newline="") as fh:
                rows = list(csv.DictReader(fh))
            assert client.post(f"/api/ingest/{table}", json=rows).status_code == 200
    for view in config.views:
        res = client.get(f"/api/views/{view}")
        assert res.status_code == 200, f"{view}: {res.text[:200]}"
    kpis = client.get("/api/kpis").json()["kpis"]
    assert [k["key"] for k in kpis] == list(config.kpis)


def test_golden_v2_3_1_config_loads_into_the_same_structure():
    """v2.3.1 の利用者の config.py(凍結コピー)が同じ意味の MonitorConfig になる。"""
    config = load_config(snapshot.GOLDEN_PROJECT / "config.py")
    assert (config.app_title, config.refresh_interval_ms, config.refresh_mode) == (
        "Monitor App",
        2000,
        "sse",
    )
    assert list(config.tables) == ["users", "products", "orders", "line_events"]
    users, products, orders = (
        config.tables[t] for t in ("users", "products", "orders")
    )
    # 列の論理型は命名規則で推論される(DB の列型を決めるため契約の一部)
    assert [users.column_type(c) for c in users.column_names] == ["int", "str", "str"]
    assert products.column_type("price") == "float"
    assert [orders.column_type(c) for c in orders.column_names] == [
        "int",
        "int",
        "int",
        "float",
    ]
    assert orders.foreign_keys == {"user_id": "users.id", "product_id": "products.id"}
    assert orders.form.submit_label == "注文を登録"
    assert orders.form.labels["amount"] == "数量"
    assert config.tables["line_events"].indexes == [["ts"]]
    assert list(config.views) == [
        "users_view",
        "products_view",
        "orders_summary",
        "line_events_view",
    ]
    products_view = config.views["products_view"]
    assert products_view.group == "販売"
    price_style = products_view.styles["price"]
    assert price_style.greater_than.css_class == "bg-primary text-white"
    assert price_style.less_than.value == 500
    assert (products_view.chart.type, products_view.chart.ucl) == ("bar", 5000)
    assert config.views["line_events_view"].labels["value"] == "計測値 (℃)"
    rule = config.alerts[0]
    assert (rule.view, rule.column, rule.op, rule.value, rule.level, rule.notify) == (
        "orders_summary",
        "amount",
        ">",
        10,
        "critical",
        ["console"],
    )
    assert list(config.kpis) == ["order_count", "total_amount"]
    assert config.kpis["total_amount"].target == 20
    assert (config.kiosk.rotate_seconds, config.kiosk.theme) == (15, "dark")
    assert (config.dashboard.views, config.dashboard.columns) == (["products_view"], 2)
    source = config.sources["line_events"]
    assert (source.kind, source.path, source.table, source.mode) == (
        "jsonl",
        "sources/line_events.jsonl",
        "line_events",
        "append",
    )


def test_legacy_v1_dict_config_loads_and_serves():
    """v1 形式(ALLOWED_TABLES / VIEW_TABLES / TABLE_CELL_STYLES)が読め、アプリが動く。"""
    config = load_config(snapshot.LEGACY_V1_CONFIG)
    assert config.app_title == "第1工場 モニタ"
    assert config.header_text == "📊 第1工場 ダッシュボード"
    assert config.footer_text == "© 2025 Monitor App - Powered by Flask & Bootstrap"
    assert config.favicon_path == "favicon.ico"
    assert config.refresh_interval_ms == 5000
    assert list(config.tables) == ["users", "products", "orders"]
    assert config.tables["orders"].foreign_keys == {
        "user_id": "users.id",
        "product_id": "products.id",
    }
    assert config.views["users_view"].title == "ユーザー一覧"
    assert config.views["users_view"].description.startswith("システムに登録")
    assert config.views["users_view"].styles == {}
    price = config.views["products_view"].styles["price"]
    assert (price.greater_than.value, price.greater_than.css_class) == (
        1000,
        "bg-primary text-white",
    )
    assert price.equal_to.css_class == "bg-secondary text-white"
    assert (price.width, price.font_size, price.align, price.bold) == (
        "20%",
        "16px",
        "right",
        False,
    )

    client = TestClient(create_app(config, _settings()))
    res = client.get("/api/views/orders_summary")
    assert res.status_code == 200
    assert res.json()["cell_styles"]["amount"]["greater_than"] == {
        "value": 10.0,
        "class": "bg-danger text-white",
    }


def test_integration_presets_produce_the_same_names():
    """プリセットが作るテーブル・ビュー・KPI・ソース名(利用者が参照する)が残っている。"""
    errors = []
    for case, expected in snapshot.load_baseline("public_api")["integrations"].items():
        names = snapshot.integration_names(
            snapshot.compose_integration(expected["module"], expected["kwargs"])
        )
        for key in ("tables", "views", "kpis", "alert_views"):
            for missing in sorted(set(expected[key]) - set(names[key])):
                errors.append(f"{case}: {key} の {missing} がなくなりました")
        for source, kind in expected["sources"].items():
            if names["sources"].get(source) != kind:
                errors.append(
                    f"{case}: ソース {source}({kind})→ {names['sources'].get(source)}"
                )
    assert not errors, _report(errors)


def test_all_presets_compose_onto_a_user_config_and_serve():
    config = load_config(snapshot.GOLDEN_PROJECT / "config.py")
    config = procdiag.attach(config, db_path="procdiag.db")
    config = netdiag.attach(config, alerts_jsonl="alerts.jsonl")
    config = network_checker.attach(config, db_path="netcheck.db", hosts=["plc-01"])
    client = TestClient(create_app(config, _settings()), raise_server_exceptions=False)
    for view in config.views:
        res = client.get(f"/api/views/{view}")
        assert res.status_code == 200, f"{view}: {res.text[:200]}"
    assert client.get("/api/kpis").status_code == 200
