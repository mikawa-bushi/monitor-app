"""monitor-app の公開契約(既存利用者との約束)を機械可読な形へ抽出する。

ベースライン生成(``_make_baseline.py``)と互換性テスト(``tests/test_contract.py``)の
両方がこのモジュールを使う。抽出と正規化をここ 1 か所に置き、生成時と検証時で必ず
同じ表現同士を比べる(比較ルールは ``compat.py``)。

型ノード(OpenAPI スキーマと実レスポンス JSON で共通の表現)は「取り得る型の候補」の
リストで、各候補は次のいずれか:

- ``"string"`` / ``"integer"`` / ``"number"`` / ``"boolean"`` / ``"null"``
- ``"object"`` … 中身を規定しない自由形式のオブジェクト(``Dict[str, Any]`` など)
- ``"any"`` … 型の規定なし
- ``{"array": ノード}`` … 配列(ノードは要素の型。空配列しか観測していなければ ``[]``)
- ``{"ref": "名前"}`` … OpenAPI の ``components.schemas`` にある名前付きスキーマ
- ``{"object": {"properties": {名前: ノード}, "required": [名前, ...]}}`` … 構造を
  持つオブジェクト(実測では「全要素に現れたキー」を required とみなす)
"""

from __future__ import annotations

import csv
import importlib
import inspect
import json
import logging
import os
import pkgutil
import re
import types
import typing
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from enum import Enum
from importlib import metadata
from pathlib import Path, PurePath
from typing import Any

import anyio
from pydantic import BaseModel
from pydantic_core import PydanticUndefined

CONTRACT_DIR = Path(__file__).resolve().parent
BASELINE_DIR = CONTRACT_DIR / "baseline"
FIXTURES_DIR = CONTRACT_DIR / "fixtures"
#: v2.3.1 の利用者プロジェクトを凍結したゴールデンフィクスチャ。
GOLDEN_PROJECT = FIXTURES_DIR / "v2_3_1_project"
#: v1(0.1.x)形式の config.py。
LEGACY_V1_CONFIG = FIXTURES_DIR / "legacy_v1_config.py"

#: ベースライン(``baseline/<名前>.json``)の一覧。
BASELINE_NAMES = ("openapi", "cli", "settings", "models", "public_api", "responses")


# ---------------------------------------------------------------------------
# JSON への正規化
# ---------------------------------------------------------------------------
def to_jsonable(value: Any) -> Any:
    """既定値などを JSON で比較できる形へ正規化する。"""
    if isinstance(value, BaseModel):
        # 入れ子モデルの既定値はクラス名だけを記録する(中身は各モデル側で検証する)。
        return {"__model__": type(value).__name__}
    if isinstance(value, PurePath):
        return value.as_posix()
    if isinstance(value, Enum):
        return to_jsonable(value.value)
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    if hasattr(value, "get_secret_value"):  # pydantic の SecretStr など
        return value.get_secret_value()
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return repr(value)


def dump_json(data: Any) -> str:
    """ベースライン JSON の書式。

    差分をレビューしやすいようキー順を固定し、スカラーだけのリスト(``["string"]`` や
    required の列挙)は 1 行に収める。出力は通常の JSON として読める。
    """
    return _format_json(data, 0) + "\n"


def _format_json(value: Any, level: int) -> str:
    pad = "  " * level
    if isinstance(value, dict) and value:
        items = [
            f"{pad}  {json.dumps(key, ensure_ascii=False)}: "
            + _format_json(value[key], level + 1)
            for key in sorted(value)
        ]
        return "{\n" + ",\n".join(items) + f"\n{pad}}}"
    if isinstance(value, list) and value:
        one_line = json.dumps(value, ensure_ascii=False)
        if not any(isinstance(v, (dict, list)) for v in value) and len(one_line) <= 72:
            return one_line
        items = [f"{pad}  " + _format_json(v, level + 1) for v in value]
        return "[\n" + ",\n".join(items) + f"\n{pad}]"
    return json.dumps(value, ensure_ascii=False)


def load_baseline(name: str) -> dict:
    return json.loads((BASELINE_DIR / f"{name}.json").read_text(encoding="utf-8"))


def mime_type(content_type: str | None) -> str:
    """``text/html; charset=utf-8`` → ``text/html``。"""
    return (content_type or "").split(";")[0].strip().lower()


# ---------------------------------------------------------------------------
# 型ノード(OpenAPI スキーマ / 実レスポンス JSON)
# ---------------------------------------------------------------------------
_REF_PREFIX = "#/components/schemas/"
_HTTP_METHODS = ("get", "put", "post", "delete", "patch", "options", "head", "trace")


def _union(nodes: Iterable[list]) -> list:
    """複数ノードの候補を重複なく 1 つのノードへまとめる(並びは決定的)。"""
    seen: dict[str, Any] = {}
    for node in nodes:
        for alt in node:
            seen.setdefault(json.dumps(alt, sort_keys=True, ensure_ascii=False), alt)
    return [seen[key] for key in sorted(seen)]


def schema_node(schema: dict | None) -> list:
    """OpenAPI(JSON Schema)のスキーマ 1 つを型ノードへ正規化する。

    説明文・タイトル・数値の下限などの制約は契約に含めない(型と構造だけを見る)。
    """
    if not schema:
        return ["any"]
    if "$ref" in schema:
        return [{"ref": schema["$ref"].removeprefix(_REF_PREFIX)}]
    for key in ("anyOf", "oneOf"):
        if key in schema:
            return _union(schema_node(sub) for sub in schema[key])
    if "allOf" in schema:
        parts = schema["allOf"]
        return schema_node(parts[0]) if len(parts) == 1 else ["any"]
    declared = schema.get("type")
    if declared is None:
        if "properties" in schema:
            declared = "object"
        elif "enum" in schema or "const" in schema:
            values = schema.get("enum", [schema.get("const")])
            return _union(json_shape(v) for v in values)
        else:
            return ["any"]
    alts: list = []
    for name in [declared] if isinstance(declared, str) else declared:
        if name == "array":
            alts.append({"array": schema_node(schema.get("items"))})
        elif name == "object" and "properties" in schema:
            alts.append({"object": _object_body(schema)})
        else:
            alts.append(name)
    return _union([alts])


def _object_body(schema: dict) -> dict:
    return {
        "properties": {
            name: schema_node(sub) for name, sub in schema.get("properties", {}).items()
        },
        "required": sorted(schema.get("required", [])),
    }


def json_shape(value: Any) -> list:
    """実際の JSON 値から型ノードを作る(配列は全要素の形を合成する)。"""
    if value is None:
        return ["null"]
    if isinstance(value, bool):
        return ["boolean"]
    if isinstance(value, int):
        return ["integer"]
    if isinstance(value, float):
        return ["number"]
    if isinstance(value, str):
        return ["string"]
    if isinstance(value, list):
        items: list = []
        for item in value:
            items = merge_shapes(items, json_shape(item))
        return [{"array": items}]
    if isinstance(value, dict):
        return [
            {
                "object": {
                    "properties": {k: json_shape(v) for k, v in value.items()},
                    "required": sorted(value),
                }
            }
        ]
    raise TypeError(f"JSON の値ではありません: {value!r}")


def merge_shapes(a: list, b: list) -> list:
    """実測ノード 2 つを合成する(オブジェクトはキーの和、required は共通部分)。"""
    scalars: set[str] = set()
    arrays: list[list] = []
    objects: list[dict] = []
    for alt in (*a, *b):
        if isinstance(alt, str):
            scalars.add(alt)
        elif "array" in alt:
            arrays.append(alt["array"])
        else:
            objects.append(alt["object"])
    merged: list = sorted(scalars)
    if arrays:
        items: list = []
        for node in arrays:
            items = merge_shapes(items, node)
        merged.append({"array": items})
    if objects:
        properties: dict[str, list] = {}
        for body in objects:
            for name, node in body["properties"].items():
                properties[name] = merge_shapes(properties.get(name, []), node)
        required = set(objects[0]["required"])
        for body in objects[1:]:
            required &= set(body["required"])
        merged.append(
            {"object": {"properties": properties, "required": sorted(required)}}
        )
    return merged


# ---------------------------------------------------------------------------
# OpenAPI
# ---------------------------------------------------------------------------
def openapi_contract(doc: dict) -> dict:
    """``app.openapi()`` の文書から、互換性に関わる部分(操作・型・認証)を抜き出す。"""
    operations = {}
    for path, item in doc.get("paths", {}).items():
        for method in _HTTP_METHODS:
            if method in item:
                operations[f"{method.upper()} {path}"] = _operation(item[method])
    components = doc.get("components", {})
    return {
        "operations": operations,
        "components": {
            name: schema_node(schema)
            for name, schema in components.get("schemas", {}).items()
        },
        "security_schemes": {
            name: {
                k: scheme[k] for k in ("type", "in", "name", "scheme") if k in scheme
            }
            for name, scheme in components.get("securitySchemes", {}).items()
        },
    }


def _operation(op: dict) -> dict:
    parameters = {}
    for param in op.get("parameters", []):
        schema = param.get("schema", {})
        entry = {
            "required": bool(param.get("required", False)),
            "type": schema_node(schema),
        }
        if "default" in schema:
            entry["default"] = schema["default"]
        parameters[f"{param['in']}:{param['name']}"] = entry
    request_body = None
    if "requestBody" in op:
        body = op["requestBody"]
        request_body = {
            "required": bool(body.get("required", False)),
            "content": {
                ctype: schema_node(media.get("schema"))
                for ctype, media in body.get("content", {}).items()
            },
        }
    responses = {
        code: {
            ctype: schema_node(media.get("schema"))
            for ctype, media in (response.get("content") or {}).items()
        }
        for code, response in op.get("responses", {}).items()
    }
    security = sorted({name for req in op.get("security", []) for name in req})
    return {
        "parameters": parameters,
        "request_body": request_body,
        "responses": responses,
        "security": security,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
#: click の型名の表記ゆれ(版によって "text"/"str" など)を吸収する。
_CLI_TYPE_ALIASES = {
    "text": "str",
    "string": "str",
    "integer": "int",
    "boolean": "bool",
}


def cli_contract() -> dict:
    """Typer アプリ(``monitor_app.cli:app``)のコマンドと引数・オプション。"""
    import typer.main

    from monitor_app.cli import app

    group = typer.main.get_command(app)
    return {
        "group_params": [_cli_param(p) for p in group.params],
        "commands": {
            name: {"params": [_cli_param(p) for p in command.params]}
            for name, command in sorted(group.commands.items())
        },
    }


def _cli_param(param: Any) -> dict:
    type_name = str(getattr(param.type, "name", type(param.type).__name__)).lower()
    default = param.default
    return {
        "name": param.name,
        "kind": param.param_type_name,  # "option" / "argument"
        "opts": list(param.opts),
        "secondary_opts": list(param.secondary_opts),
        "is_flag": bool(getattr(param, "is_flag", False)),
        "default": "<callable>" if callable(default) else to_jsonable(default),
        "required": bool(param.required),
        "multiple": bool(param.multiple),
        "nargs": param.nargs,
        "type": _CLI_TYPE_ALIASES.get(type_name, type_name),
    }


# ---------------------------------------------------------------------------
# 実行時設定(AppSettings / MONITOR_*)
# ---------------------------------------------------------------------------
#: 環境変数が効くことを確かめるための代表値。既定値と異なり、かつ実運用で妥当な値。
SETTINGS_PROBES = {
    "database_url": "sqlite:///probe/database.db",
    "host": "0.0.0.0",
    "port": "8123",
    "api_key": "probe-key",
    "protect_reads": "true",
    "cors_origins": '["http://probe.example"]',
    "log_level": "DEBUG",
    "csv_dir": "probe_csv",
    "csv_encoding": "cp932",
    "view_cache_ttl_ms": "250",
    "ingest_watch": "true",
    "ingest_interval": "2.5",
    "ingest_mode": "append",
    "audit_enabled": "true",
    "webhook_url": "http://probe.example/hook",
    "smtp_host": "smtp.probe.example",
    "smtp_port": "2525",
    "smtp_user": "probe-user",
    "smtp_password": "probe-pass",
    "alert_email_to": "ops@probe.example",
}


@contextmanager
def monitor_environ(updates: dict[str, str] | None = None) -> Iterator[None]:
    """``MONITOR_*`` を全部外し ``updates`` だけを設定する。終了時に元へ戻す。

    開発者のシェルに残った ``MONITOR_API_KEY`` などが契約の観測を汚さないようにする。
    """
    saved = {k: v for k, v in os.environ.items() if k.upper().startswith("MONITOR_")}
    try:
        for key in saved:
            del os.environ[key]
        os.environ.update(updates or {})
        yield
    finally:
        for key in [k for k in os.environ if k.upper().startswith("MONITOR_")]:
            del os.environ[key]
        os.environ.update(saved)


def read_settings(env: dict[str, str]) -> dict[str, Any]:
    """``env`` だけを与えて AppSettings を作り、全フィールドの値を返す(.env は読まない)。"""
    from monitor_app.settings.runtime import AppSettings

    with monitor_environ(env):
        settings = AppSettings(_env_file=None)
    return {
        name: to_jsonable(getattr(settings, name))
        for name in type(settings).model_fields
    }


def field_default(field: Any) -> Any:
    """pydantic のフィールド定義から既定値を取り出して正規化する。"""
    if field.default_factory is not None:
        try:
            value = field.default_factory()
        except TypeError:  # 検証済みデータを引数に取るファクトリ
            return "<factory>"
        return to_jsonable(value)
    if field.default is PydanticUndefined:
        return None
    return to_jsonable(field.default)


def settings_fields() -> dict:
    """AppSettings の環境変数プレフィックス・.env ファイル名と、各フィールドの
    環境変数名・必須か・既定値(静的な定義から読む。環境には依存しない)。"""
    from monitor_app.settings.runtime import AppSettings

    config = AppSettings.model_config
    prefix = config.get("env_prefix", "")
    fields = {}
    for name, field in AppSettings.model_fields.items():
        entry: dict[str, Any] = {
            "env": f"{prefix}{name}".upper(),
            "required": field.is_required(),
        }
        if not field.is_required():
            entry["default"] = field_default(field)
        fields[name] = entry
    return {
        "env_prefix": prefix,
        "env_file": to_jsonable(config.get("env_file")),
        "fields": fields,
    }


def settings_contract() -> dict:
    """``settings_fields`` に、代表値の環境変数が実際に効くことの実測を加える。"""
    contract = settings_fields()
    fields = contract["fields"]
    probes = {name: raw for name, raw in SETTINGS_PROBES.items() if name in fields}
    observed = read_settings({fields[name]["env"]: raw for name, raw in probes.items()})
    for name, raw in probes.items():
        if observed[name] == fields[name].get("default"):
            raise RuntimeError(f"代表値が既定値と同じで検証になりません: {name}")
        fields[name]["probe"] = {"raw": raw, "expected": observed[name]}
    return contract


# ---------------------------------------------------------------------------
# 宣言モデル(config.py で書く Pydantic モデル)
# ---------------------------------------------------------------------------
MODEL_NAMES = (
    "MonitorConfig",
    "TableDef",
    "ViewDef",
    "AlertRule",
    "KpiCard",
    "KioskConfig",
    "ChartDef",
    "CellStyle",
    "Threshold",
    "FormDef",
    "SourceDef",
    "DashboardConfig",
)


def literal_choices(annotation: Any) -> list[str] | None:
    """``Literal``(``Optional[Literal]`` を含む)で受け付ける値の一覧。"""
    origin = typing.get_origin(annotation)
    if origin is typing.Literal:
        return sorted(str(v) for v in typing.get_args(annotation))
    if origin in (typing.Union, types.UnionType):
        found: set[str] = set()
        for arg in typing.get_args(annotation):
            found.update(literal_choices(arg) or [])
        return sorted(found) or None
    return None


def public_members(cls: type) -> list[str]:
    """モデルに定義された公開メソッド・プロパティ名(pydantic 由来のものは除く)。"""
    names: set[str] = set()
    for klass in cls.__mro__:
        if klass is object or klass.__module__.startswith("pydantic"):
            continue
        for name, value in vars(klass).items():
            if name.startswith("_") or name.startswith("model_"):
                continue
            if callable(value) or isinstance(value, (property, classmethod)):
                names.add(name)
    return sorted(names)


def populate_by_name(cls: type) -> bool:
    config = cls.model_config
    return bool(config.get("populate_by_name") or config.get("validate_by_name"))


def models_contract() -> dict:
    from monitor_app.settings import declarative

    models = {}
    for name in MODEL_NAMES:
        cls = getattr(declarative, name)
        fields = {}
        for field_name, field in cls.model_fields.items():
            entry: dict[str, Any] = {
                "required": field.is_required(),
                "alias": field.alias,
            }
            if not field.is_required():
                entry["default"] = field_default(field)
            choices = literal_choices(field.annotation)
            if choices:
                entry["choices"] = choices
            fields[field_name] = entry
        models[name] = {
            "fields": fields,
            "populate_by_name": populate_by_name(cls),
            "members": public_members(cls),
        }
    return {
        "models": models,
        "column_types": sorted(typing.get_args(declarative.ColumnType)),
    }


# ---------------------------------------------------------------------------
# 公開 API(import パス・関数シグネチャ・ロガー名・コマンド名・プリセット)
# ---------------------------------------------------------------------------
#: 利用者の config.py・スクリプト・ドキュメントが使う import パス。
PUBLIC_IMPORTS = {
    "monitor_app.settings.declarative": [*MODEL_NAMES, "ColumnType"],
    "monitor_app.settings.loader": ["ConfigError", "load_config"],
    "monitor_app.settings.runtime": ["AppSettings"],
    "monitor_app.main": ["app_factory", "create_app"],
    "monitor_app.cli": ["app"],
    "monitor_app.integrations": ["merge"],
    "monitor_app.integrations.netdiag": ["attach"],
    "monitor_app.integrations.network_checker": ["attach"],
    "monitor_app.integrations.procdiag": ["attach"],
}

#: シグネチャを固定する公開関数(``モジュール:名前``)。
PUBLIC_FUNCTIONS = (
    "monitor_app.main:create_app",
    "monitor_app.main:app_factory",
    "monitor_app.settings.loader:load_config",
    "monitor_app.integrations:merge",
    "monitor_app.integrations.procdiag:attach",
    "monitor_app.integrations.netdiag:attach",
    "monitor_app.integrations.network_checker:attach",
)

#: プリセットを既定の(またはドキュメント記載の)引数で合成するケース。
INTEGRATION_CASES = {
    "procdiag": ("procdiag", {"db_path": "procdiag.db"}),
    "procdiag+findings": (
        "procdiag",
        {"db_path": "procdiag.db", "findings_json": "findings.json"},
    ),
    "netdiag": ("netdiag", {}),
    "network_checker": ("network_checker", {}),
    "network_checker+hosts": ("network_checker", {"hosts": ["plc-01", "gateway"]}),
}


def resolve(target: str) -> Any:
    module_name, _, attr = target.partition(":")
    return getattr(importlib.import_module(module_name), attr)


def signature_contract(func: Any) -> list[dict]:
    params = []
    for param in inspect.signature(func).parameters.values():
        entry: dict[str, Any] = {"name": param.name, "kind": param.kind.name}
        if param.default is not inspect.Parameter.empty:
            entry["default"] = to_jsonable(param.default)
        params.append(entry)
    return params


_LITERAL_LOGGER_RE = re.compile(r"""getLogger\(\s*["'](monitor_app(?:\.[\w.]+)?)["']""")


def monitor_logger_names() -> list[str]:
    """``monitor_app.*`` のロガー名(利用者のログ設定のフィルタ・レベル指定が参照する)。

    全モジュールを import して生成されたロガーに、ソース中の ``getLogger("...")`` の
    文字列リテラルを加える(関数内で遅延生成するように書き換えても検出できるように)。
    """
    import monitor_app

    names: set[str] = set()
    for info in pkgutil.walk_packages(monitor_app.__path__, "monitor_app."):
        if not info.name.endswith(".__main__"):
            importlib.import_module(info.name)
    for package_dir in monitor_app.__path__:
        for source in Path(package_dir).rglob("*.py"):
            names.update(_LITERAL_LOGGER_RE.findall(source.read_text(encoding="utf-8")))
    names.update(
        name
        for name, logger in logging.root.manager.loggerDict.items()
        if isinstance(logger, logging.Logger)
        and (name == "monitor_app" or name.startswith("monitor_app."))
    )
    return sorted(names)


def console_scripts() -> dict[str, str]:
    """配布メタデータに登録されたコマンド名 → エントリポイント。"""
    try:
        dist = metadata.distribution("monitor-app")
    except metadata.PackageNotFoundError:  # ソースツリーを直接 import している
        return {}
    return {
        ep.name: ep.value for ep in dist.entry_points if ep.group == "console_scripts"
    }


def compose_integration(module_name: str, kwargs: dict, base: Any = None) -> Any:
    from monitor_app import MonitorConfig

    module = importlib.import_module(f"monitor_app.integrations.{module_name}")
    return module.attach(base if base is not None else MonitorConfig(), **kwargs)


def integration_names(config: Any) -> dict:
    """合成済み設定から、利用者が名前で参照し得る要素の一覧を取り出す。"""
    return {
        "tables": sorted(config.tables),
        "views": sorted(config.views),
        "kpis": sorted(config.kpis),
        "sources": {name: s.kind for name, s in sorted(config.sources.items())},
        "alert_views": sorted({rule.view for rule in config.alerts}),
    }


def public_api_contract() -> dict:
    import monitor_app
    import monitor_app.settings

    for module_name, names in PUBLIC_IMPORTS.items():
        module = importlib.import_module(module_name)
        missing = [n for n in names if not hasattr(module, n)]
        if missing:
            raise RuntimeError(f"{module_name} に {missing} がありません")
    integrations = {}
    for case, (module_name, kwargs) in INTEGRATION_CASES.items():
        config = compose_integration(module_name, kwargs)
        integrations[case] = {
            "module": module_name,
            "kwargs": kwargs,
            **integration_names(config),
        }
    return {
        "all": {
            "monitor_app": sorted(monitor_app.__all__),
            "monitor_app.settings": sorted(monitor_app.settings.__all__),
        },
        "imports": {name: sorted(names) for name, names in PUBLIC_IMPORTS.items()},
        "signatures": {t: signature_contract(resolve(t)) for t in PUBLIC_FUNCTIONS},
        "loggers": monitor_logger_names(),
        "console_scripts": console_scripts(),
        "integrations": integrations,
    }


# ---------------------------------------------------------------------------
# REST / HTML / SSE の実レスポンス
# ---------------------------------------------------------------------------
def golden_rows(table: str) -> list[dict]:
    """ゴールデンフィクスチャの CSV を行(文字列の dict)として読む。"""
    path = GOLDEN_PROJECT / "csv" / f"{table}.csv"
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def golden_line_events() -> list[dict]:
    path = GOLDEN_PROJECT / "sources" / "line_events.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def create_contract_app(settings: dict | None = None) -> Any:
    """ゴールデン config(v2.3.1 の scaffold 相当)とインメモリ SQLite でアプリを組む。"""
    from monitor_app.main import create_app
    from monitor_app.settings.loader import load_config
    from monitor_app.settings.runtime import AppSettings

    config = load_config(GOLDEN_PROJECT / "config.py")
    with monitor_environ():
        app_settings = AppSettings(
            _env_file=None, database_url="sqlite://", **(settings or {})
        )
    return create_app(config, app_settings)


def seeded_client(app: Any, api_key: str | None = None) -> Any:
    """ゴールデンの CSV/JSONL を公開 API(``POST /api/ingest``)で投入した TestClient。

    内部 API(CsvImporter など)に依存しないため、リファクタでサービス層の構成が
    変わってもシナリオはそのまま成り立つ。lifespan は起動しない(AlertScheduler が
    裏でアラート状態を変えないようにし、観測を決定的にする)。
    """
    from fastapi.testclient import TestClient

    client = TestClient(app, raise_server_exceptions=False)
    headers = {"X-API-Key": api_key} if api_key else {}
    batches = [(t, golden_rows(t)) for t in ("users", "products", "orders")]
    batches.append(("line_events", golden_line_events()))
    for table, rows in batches:
        res = client.post(f"/api/ingest/{table}", json=rows, headers=headers)
        if res.status_code != 200:
            raise RuntimeError(
                f"投入に失敗しました: {table} {res.status_code} {res.text}"
            )
    return client


def _get(request_id: str, path: str, **extra: Any) -> dict:
    return {"id": request_id, "method": "GET", "path": path, **extra}


_SECURITY_HEADERS = ["referrer-policy", "x-content-type-options", "x-frame-options"]

#: 実レスポンスの契約シナリオ。順番に実行する(作成 → 取得 → 更新 → 削除 のように
#: 前のリクエストの結果に依存する)。記録する観測値:
#:   status / content_type(常に)、shape(JSON 本文の形。``shape: False`` で省略)、
#:   pin_json(値を固定する JSON パス)、pin_headers(値を固定するヘッダ)、
#:   header_contains(ヘッダに含まれるべき文字列)、pin_first_line(本文 1 行目)、
#:   kind="sse" は最初のイベントの型と data の形。
RESPONSE_SCENARIOS = [
    {
        "name": "default",
        "settings": {"audit_enabled": True},
        "requests": [
            _get(
                "health",
                "/api/health",
                pin_json=["status"],
                pin_headers=_SECURITY_HEADERS,
            ),
            _get("schema", "/api/schema"),
            _get("tables.list", "/api/tables/users"),
            _get("tables.list.paged", "/api/tables/users?limit=2&offset=1"),
            _get("tables.get", "/api/tables/users/1"),
            {
                "id": "tables.create",
                "method": "POST",
                "path": "/api/tables/orders",
                "json": {"user_id": 1, "product_id": 2, "amount": 3},
                "pin_json": ["success"],
            },
            _get("tables.get.created", "/api/tables/orders/6"),
            {
                "id": "tables.update",
                "method": "PUT",
                "path": "/api/tables/orders/6",
                "json": {"amount": 7},
                "pin_json": ["success"],
            },
            {
                "id": "tables.delete",
                "method": "DELETE",
                "path": "/api/tables/orders/6",
                "pin_json": ["success"],
            },
            {
                "id": "ingest.single",
                "method": "POST",
                "path": "/api/ingest/products",
                "json": {"id": 10, "name": "Durian", "price": "1200"},
                "pin_json": ["success", "inserted"],
            },
            {
                "id": "ingest.list",
                "method": "POST",
                "path": "/api/ingest/products",
                "json": [
                    {"id": 11, "name": "Elderberry", "price": 300},
                    {"id": 12, "name": "Fig", "price": 450.5},
                ],
                "pin_json": ["success", "inserted"],
            },
            _get("views.get.paged", "/api/views/orders_summary?limit=2&offset=1"),
            _get("views.get", "/api/views/orders_summary"),
            _get("views.get.labels", "/api/views/line_events_view"),
            _get(
                "views.batch", "/api/views/batch?names=users_view,products_view,ghost"
            ),
            _get(
                "views.export.csv",
                "/api/views/products_view/export",
                pin_first_line=True,
            ),
            {
                "id": "views.stream",
                "kind": "sse",
                "method": "GET",
                "path": "/api/views/orders_summary/stream",
            },
            _get("kpis", "/api/kpis"),
            _get("alerts", "/api/alerts"),
            _get("audit", "/api/audit"),
            _get("audit.filtered", "/api/audit?table=orders&limit=2"),
            # --- エラー(エンベロープ {"detail": {"code": ..., "message": ...}})---
            _get("error.view_not_found", "/api/views/ghost", pin_json=["detail.code"]),
            _get(
                "error.export_view_not_found",
                "/api/views/ghost/export",
                pin_json=["detail.code"],
            ),
            _get(
                "error.table_not_found", "/api/tables/ghost", pin_json=["detail.code"]
            ),
            _get(
                "error.record_not_found",
                "/api/tables/users/999",
                pin_json=["detail.code"],
            ),
            _get(
                "error.record_id_type",
                "/api/tables/users/abc",
                pin_json=["detail.code"],
            ),
            {
                "id": "error.update_not_found",
                "method": "PUT",
                "path": "/api/tables/users/999",
                "json": {"name": "x"},
                "pin_json": ["detail.code"],
            },
            {
                "id": "error.delete_not_found",
                "method": "DELETE",
                "path": "/api/tables/users/999",
                "pin_json": ["detail.code"],
            },
            {
                "id": "error.no_valid_columns",
                "method": "POST",
                "path": "/api/tables/users",
                "json": {"junk": 1},
                "pin_json": ["detail.code"],
            },
            {
                "id": "error.coercion",
                "method": "POST",
                "path": "/api/tables/products",
                "json": {"name": "x", "price": "abc"},
                "pin_json": ["detail.code"],
            },
            {
                "id": "error.ingest_table_not_found",
                "method": "POST",
                "path": "/api/ingest/ghost",
                "json": [{"id": 1}],
                "pin_json": ["detail.code"],
            },
            # クエリパラメータの検証エラーは FastAPI 標準の形({"detail": [...]})。
            # 要素のキーは FastAPI/pydantic の版で増減するため形は記録しない
            # (中核のキーは test_contract の個別テストで確認する)。
            _get(
                "error.query_validation", "/api/views/users_view?limit=0", shape=False
            ),
            # --- HTML ページ・静的ファイル・API ドキュメント(OpenAPI 外のルート)---
            _get("page.index", "/", pin_headers=_SECURITY_HEADERS),
            _get("page.table", "/table/users_view"),
            _get("page.table.chart", "/table/products_view"),
            _get("page.form", "/form/orders"),
            _get("page.kiosk", "/kiosk"),
            _get("page.login", "/login"),
            _get("page.table.not_found", "/table/ghost", pin_json=["detail.code"]),
            _get("page.form.not_found", "/form/ghost", pin_json=["detail.code"]),
            _get("static.css", "/static/css/app.css"),
            _get("docs.swagger", "/docs"),
            _get("docs.redoc", "/redoc"),
            _get("docs.openapi", "/openapi.json", shape=False),
        ],
    },
    {
        "name": "auth",
        "settings": {"api_key": "test-key", "protect_reads": True},
        "requests": [
            _get("auth.health_is_open", "/api/health", pin_json=["status"]),
            _get(
                "auth.read_without_key",
                "/api/views/users_view",
                pin_json=["detail.code"],
            ),
            _get(
                "auth.read_with_key",
                "/api/views/users_view",
                send_headers={"X-API-Key": "test-key"},
            ),
            {
                "id": "auth.write_without_key",
                "method": "POST",
                "path": "/api/tables/users",
                "json": {"name": "x"},
                "pin_json": ["detail.code"],
            },
            _get("auth.page_redirects_to_login", "/", pin_headers=["location"]),
            _get("auth.login_page_is_open", "/login"),
            {
                "id": "auth.login_wrong_key",
                "method": "POST",
                "path": "/api/login",
                "form": {"key": "wrong"},
                "pin_headers": ["location"],
            },
            {
                "id": "auth.login",
                "method": "POST",
                "path": "/api/login",
                "form": {"key": "test-key"},
                "pin_headers": ["location"],
                "header_contains": {"set-cookie": ["monitor_session=", "httponly"]},
            },
            _get("auth.read_with_session", "/api/views/users_view"),
            _get("auth.page_with_session", "/"),
            {
                "id": "auth.logout",
                "method": "POST",
                "path": "/api/logout",
                "pin_headers": ["location"],
            },
            _get(
                "auth.read_after_logout",
                "/api/views/users_view",
                pin_json=["detail.code"],
            ),
        ],
    },
]


def json_path(body: Any, path: str) -> Any:
    """``"detail.code"`` のようなドット区切りで JSON をたどる。無ければ ``"<missing>"``。"""
    current = body
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return "<missing>"
        current = current[part]
    return current


def perform(app: Any, client: Any, spec: dict) -> dict:
    """シナリオの 1 リクエストを実行し、契約として比べる観測値を返す。"""
    if spec.get("kind") == "sse":
        return _perform_sse(app, spec)
    res = client.request(
        spec["method"],
        spec["path"],
        json=spec.get("json"),
        data=spec.get("form"),
        headers=spec.get("send_headers"),
        follow_redirects=False,
    )
    observed: dict[str, Any] = {
        "status": res.status_code,
        "content_type": mime_type(res.headers.get("content-type")),
    }
    body = res.json() if observed["content_type"] == "application/json" else None
    if body is not None and spec.get("shape", True):
        observed["shape"] = json_shape(body)
    if spec.get("pin_json"):
        observed["pin_json"] = {p: json_path(body, p) for p in spec["pin_json"]}
    if spec.get("pin_headers"):
        observed["pin_headers"] = {h: res.headers.get(h) for h in spec["pin_headers"]}
    if spec.get("header_contains"):
        observed["header_contains"] = {
            header: [
                n for n in needles if n.lower() in res.headers.get(header, "").lower()
            ]
            for header, needles in spec["header_contains"].items()
        }
    if spec.get("pin_first_line"):
        observed["first_line"] = res.text.splitlines()[0] if res.text else ""
    return observed


def _perform_sse(app: Any, spec: dict) -> dict:
    status, headers, raw = anyio.run(
        read_first_sse_event, app, spec["path"], spec.get("send_headers") or {}
    )
    observed: dict[str, Any] = {
        "status": status,
        "content_type": mime_type(headers.get("content-type")),
    }
    events = parse_sse(raw)
    if events:
        observed["event"] = events[0]["event"]
        observed["shape"] = json_shape(json.loads(events[0]["data"]))
    return observed


async def read_first_sse_event(
    app: Any, path: str, headers: dict[str, str] | None = None, timeout: float = 10.0
) -> tuple[int, dict[str, str], bytes]:
    """SSE エンドポイントを ASGI レベルで呼び、最初のイベントを受け取ったら切断する。

    TestClient はストリームを最後まで読もうとするため無限の SSE には使えない。ここでは
    ミドルウェア・ルーティング・EventSourceResponse を含む実際のアプリをそのまま通し、
    最初のイベントが確定した時点(コメントや retry だけの塊は読み飛ばす)でクライアント
    切断を通知する。
    """
    start: dict[str, Any] = {}
    body = bytearray()
    first_event = anyio.Event()
    request_sent = False

    async def receive() -> dict:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await first_event.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        if message["type"] == "http.response.start":
            start.update(message)
        elif message["type"] == "http.response.body":
            body.extend(message.get("body", b""))
            # 最初のイベントが確定したか、ストリームが終わった(エラー応答など)。
            # 確定したイベントの手前のバイト列は受信済みなので、末尾の未完の多バイト
            # 文字だけが置換され得る(未完のイベントは解析結果に含まれない)。
            ended = not message.get("more_body")
            if ended or parse_sse(bytes(body).decode("utf-8", "replace")):
                first_event.set()

    raw_path, _, query = path.partition("?")
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": raw_path,
        "raw_path": raw_path.encode("utf-8"),
        "query_string": query.encode("utf-8"),
        "root_path": "",
        "headers": [(b"host", b"testserver"), (b"accept", b"text/event-stream")]
        + [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }
    with anyio.fail_after(timeout):
        await app(scope, receive, send)
    response_headers = {
        k.decode("latin-1").lower(): v.decode("latin-1")
        for k, v in start.get("headers", [])
    }
    return start.get("status", 0), response_headers, bytes(body)


def parse_sse(raw: bytes | str) -> list[dict]:
    """SSE のワイヤ形式(text/event-stream)をイベントのリストへ解析する。

    仕様どおり ``event`` 省略時の型は ``message``、``:`` で始まる行はコメント(ping)、
    空行で 1 イベントが確定する(空行の来ていない末尾は未完のイベントとして捨てる)。
    """
    text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    events: list[dict] = []
    data: list[str] = []
    event_type: str | None = None
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line == "":
            if data:
                events.append(
                    {"event": event_type or "message", "data": "\n".join(data)}
                )
            data, event_type = [], None
            continue
        if line.startswith(":"):
            continue
        name, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if name == "event":
            event_type = value
        elif name == "data":
            data.append(value)
    return events


def responses_contract() -> dict:
    scenarios = []
    for scenario in RESPONSE_SCENARIOS:
        settings = scenario["settings"]
        app = create_contract_app(settings)
        client = seeded_client(app, api_key=settings.get("api_key"))
        entries = [
            {"request": spec, "expected": perform(app, client, spec)}
            for spec in scenario["requests"]
        ]
        client.close()
        scenarios.append(
            {"name": scenario["name"], "settings": settings, "requests": entries}
        )
    return {"scenarios": scenarios}


# ---------------------------------------------------------------------------
# まとめ
# ---------------------------------------------------------------------------
def build_baseline(name: str) -> dict:
    """ベースライン 1 つ分を現行コードから組み立てる。"""
    if name == "openapi":
        return openapi_contract(create_contract_app().openapi())
    builders = {
        "cli": cli_contract,
        "settings": settings_contract,
        "models": models_contract,
        "public_api": public_api_contract,
        "responses": responses_contract,
    }
    return builders[name]()
