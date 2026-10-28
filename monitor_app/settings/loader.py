"""ユーザーの ``config.py`` を読み込み、:class:`MonitorConfig` を得る。

対応する書き方は 2 通り:

1. **v2 形式** … モジュール内に ``config = MonitorConfig(...)`` がある。
2. **v1 形式(後方互換)** … ``ALLOWED_TABLES`` / ``VIEW_TABLES`` /
   ``TABLE_CELL_STYLES`` などのモジュール変数がある。

``ValidationError`` は初心者にも分かるよう日本語に整形して送出する(どのモデルの
設定か・config.py の何行目か・何が悪いか)。未知のキー(綴りの誤り)は読み込みを
止めない警告(:class:`~monitor_app.settings.declarative.ConfigWarning`)で、
:func:`load_config_with_warnings` で集められる。
"""

from __future__ import annotations

import builtins
import difflib
import importlib.util
import os
import re
import traceback
import types
from collections.abc import Iterable
from pathlib import Path
from types import ModuleType, TracebackType
from typing import Any, Union, get_args, get_origin

from pydantic import ValidationError

from .declarative import (
    CellStyle,
    ConfigWarning,
    MonitorConfig,
    TableDef,
    ViewDef,
    _ConfigModel,
    _key_lineno,
    collect_config_warnings,
    config_warnings_displayed,
    show_config_warning,
)


class ConfigError(Exception):
    """設定ファイルの読み込み・検証に失敗したことを表す。"""


#: エラーの「指定値」として表示する長さの上限。
_INPUT_REPR_MAX = 60
#: ValidationError の表示に添える未知キーのヒントの最大数。
_MAX_HINTS = 5
#: config.py の読み込みで表示済みの警告(ファイル, 行, 文言)。
_SHOWN_WARNINGS: set[tuple[str, int | None, str]] = set()


def _same_file(a: str | os.PathLike, b: str | os.PathLike) -> bool:
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:  # pragma: no cover - 解決できないパス(権限など)
        return os.path.abspath(a) == os.path.abspath(b)


def _config_lineno(tb: TracebackType | None, path: Path) -> int | None:
    """トレースバックのうち config.py の中で最も深い行番号(無ければ None)。"""
    lineno = None
    for frame in traceback.extract_tb(tb):
        if _same_file(frame.filename, path):
            lineno = frame.lineno
    return lineno


def _import_module_from_path(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location("monitor_user_config", path)
    if spec is None or spec.loader is None:
        raise ConfigError(f"設定ファイルを読み込めません: {path}")
    module = importlib.util.module_from_spec(spec)
    # 未知キーの警告は実行が成功してから表示する。失敗したときはエラーのヒントに
    # 含めるため、同じ内容を 2 回見せない(warnings のフィルタには触れない。
    # config.py が自分で設定したフィルタを元に戻してしまわないように)。
    with collect_config_warnings(display=False) as hints:
        try:
            spec.loader.exec_module(module)
        except ValidationError as exc:
            lineno = _config_lineno(exc.__traceback__, path)
            raise ConfigError(
                _format_validation_error(exc, path=path, lineno=lineno, hints=hints)
            ) from exc
        except SyntaxError as exc:
            raise ConfigError(_format_syntax_error(exc, path)) from exc
        except Exception as exc:  # ユーザーコードの任意例外を包む
            raise ConfigError(_format_exec_error(exc, path, module)) from exc
    _show_hints(hints, path)
    return module


def _show_hints(hints: Iterable[ConfigWarning], path: Path) -> None:
    """集めた警告を Python の警告として表示する(``-W error`` なら ConfigError)。"""
    if not config_warnings_displayed():
        return  # 呼び出し側(load_config_with_warnings)が表示する
    for hint in hints:
        # runserver は CLI の検証と app_factory で同じ config.py を 2 回読むため、
        # 同じ警告(ファイル・行・文言)はプロセスで 1 回だけ表示する。
        shown = (os.path.abspath(hint.filename or ""), hint.lineno, str(hint))
        if shown in _SHOWN_WARNINGS:
            continue
        try:
            show_config_warning(hint)
        except ConfigWarning as exc:  # -W error で警告をエラーにしている
            raise ConfigError(format_config_warning(exc, path)) from exc
        _SHOWN_WARNINGS.add(shown)


def _load_v1(module: ModuleType, path: Path) -> MonitorConfig:
    """v1 形式を変換する。未知のキーの警告は config.py の行を指すようにする。

    変換は monitor-app の中(このモジュール)でモデルを組み立てるため、そのままでは
    警告の場所が loader.py になる。config.py の中でそのキーを書いた行に付け替える。
    """
    with collect_config_warnings(display=False) as hints:
        try:
            config = _from_v1_module(module)
        except ValidationError as exc:
            for hint in hints:
                _locate_in(hint, path)
            raise ConfigError(
                _format_validation_error(exc, path=path, hints=hints)
            ) from exc
    for hint in hints:
        _locate_in(hint, path)
    _show_hints(hints, path)
    return config


def _locate_in(warning: ConfigWarning, path: Path) -> None:
    """警告の場所を config.py の中でキーを書いた行にする(見つからなければ 1 行目)。"""
    warning.filename = str(path)
    warning.lineno = _key_lineno(str(path), 1, warning.key, limit=None)


def _from_v1_module(module: ModuleType) -> MonitorConfig:
    """v1 形式のモジュール変数から :class:`MonitorConfig` を組み立てる。"""

    allowed: dict[str, Any] = getattr(module, "ALLOWED_TABLES", {})
    view_tables: dict[str, Any] = getattr(module, "VIEW_TABLES", {})
    cell_styles: dict[str, Any] = getattr(module, "TABLE_CELL_STYLES", {})

    tables = {
        name: TableDef(
            columns=info["columns"],
            primary_key=info.get("primary_key", "id"),
            foreign_keys=info.get("foreign_keys", {}),
        )
        for name, info in allowed.items()
    }

    views = {}
    for name, info in view_tables.items():
        raw_styles = cell_styles.get(name, {})
        styles = {col: CellStyle(**rule) for col, rule in raw_styles.items()}
        views[name] = ViewDef(
            query=info["query"],
            title=info.get("title", name),
            description=info.get("description", ""),
            styles=styles,
        )

    extras: dict[str, Any] = {}
    for src, dst in (
        ("APP_TITLE", "app_title"),
        ("HEADER_TEXT", "header_text"),
        ("FOOTER_TEXT", "footer_text"),
        ("FAVICON_PATH", "favicon_path"),
        ("TABLE_REFRESH_INTERVAL", "refresh_interval_ms"),
    ):
        if hasattr(module, src):
            extras[dst] = getattr(module, src)

    return MonitorConfig(tables=tables, views=views, **extras)


# ---------------------------------------------------------------------------
# エラーの整形
# ---------------------------------------------------------------------------
#: 型の不一致を表すエラー種別 → 期待した型の日本語名。
_TYPE_NAMES = {
    "string_type": "文字列",
    "int_type": "整数",
    "int_parsing": "整数",
    "int_from_float": "整数",
    "float_type": "数値",
    "float_parsing": "数値",
    "bool_type": "True / False",
    "bool_parsing": "True / False",
    "list_type": "リスト [...]",
    "dict_type": "辞書 {...}",
}
#: 比較の制約(ctx のキー → 日本語)。
_BOUNDS = (
    ("gt", "より大きい値"),
    ("ge", "以上の値"),
    ("lt", "より小さい値"),
    ("le", "以下の値"),
)


def _short_repr(value: Any) -> str:
    text = repr(value)
    if len(text) > _INPUT_REPR_MAX:
        text = text[: _INPUT_REPR_MAX - 1] + "…"
    return text


def _expected_choices(expected: str) -> str:
    """pydantic の ``'a', 'b' or 'c'`` を ``'a' / 'b' / 'c'`` にする。"""
    parts = expected.replace(" or ", ", ").split(", ")
    return " / ".join(p.strip() for p in parts if p.strip())


#: pydantic が Union の候補ごとのエラーの位置(loc)に入れる、候補の型の名前の形。
_TYPE_TAG_RE = re.compile(
    r"(?:list|dict|tuple|set|frozenset|literal|function-[a-z]+)\[.*\]"
    r"|str|int|float|bool|none",
    re.DOTALL,
)


def _union_fields() -> frozenset[str]:
    """宣言モデルのうち Union 型(None 以外の候補が 2 つ以上)の項目名(別名を含む)。"""
    names: set[str] = set()
    pending: list[type] = [_ConfigModel]
    while pending:
        model = pending.pop()
        pending.extend(model.__subclasses__())
        for name, field in getattr(model, "model_fields", {}).items():
            members = [a for a in get_args(field.annotation) if a is not type(None)]
            if get_origin(field.annotation) in (Union, types.UnionType) and (
                len(members) > 1
            ):
                names.update(n for n in (name, field.alias) if n)
    return frozenset(names)


def _union_cut(loc: tuple) -> int | None:
    """loc のうち Union の候補(``list[str]`` など)を表す要素の位置(無ければ None)。

    候補の名前は Union 型の項目の直後にだけ現れる。``温度[℃]`` のような列名・
    ``str`` という名前の列を候補の名前と取り違えないよう、位置と形の両方で判定する。
    """
    fields = _union_fields()
    for i in range(1, len(loc)):
        part = loc[i]
        if (
            isinstance(part, str)
            and loc[i - 1] in fields
            and _TYPE_TAG_RE.fullmatch(part)
        ):
            return i
    return None


def _error_message(err: dict) -> str:
    """pydantic のエラー 1 件を日本語の説明にする(未対応の種別は原文のまま)。"""
    kind = err.get("type", "")
    ctx = err.get("ctx") or {}
    shown = f"(指定値: {_short_repr(err.get('input'))})"
    if kind == "missing":
        return "必須の項目が指定されていません"
    if kind == "literal_error":
        return (
            f"{_expected_choices(str(ctx.get('expected', '')))} のいずれかを"
            f"指定してください{shown}"
        )
    if kind in _TYPE_NAMES:
        return f"{_TYPE_NAMES[kind]}を指定してください{shown}"
    if kind in ("model_type", "model_attributes_type"):
        name = ctx.get("class_name") or "モデル"
        return f"{name}(...)か辞書を指定してください{shown}"
    for key, label in _BOUNDS:
        if key in ctx and kind.startswith(("greater_than", "less_than")):
            return f"{ctx[key]} {label}を指定してください{shown}"
    msg = str(err.get("msg", ""))
    for prefix in ("Value error, ", "Assertion failed, "):
        if msg.startswith(prefix):
            return msg[len(prefix) :]
    return msg


def _format_errors(errors: list[dict]) -> list[str]:
    """エラーを ``位置: 説明`` の行にする。Union の候補ごとのエラーはまとめる。

    例えば ``TableDef.columns``(リスト または 辞書)に誤った辞書を渡すと、pydantic は
    「リストではない」と「辞書の中身が誤り」の 2 件を返す。中身まで進んだ候補が
    あればそれだけを示し、どの候補の型にも合わないときは 1 行にまとめる。
    """
    groups: dict[tuple, list[tuple[tuple, dict]]] = {}
    order: list[tuple] = []
    for err in errors:
        loc = tuple(err.get("loc", ()))
        cut = _union_cut(loc)
        if cut is None:
            base, rest = loc, ()
        else:
            base, rest = loc[:cut], loc[cut + 1 :]
        key = (base, cut is not None)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append((rest, err))

    lines: list[str] = []
    for key in order:
        base, is_union = key
        items = groups[key]
        if is_union:
            deep = [(rest, err) for rest, err in items if rest]
            if deep:
                items = deep
            else:
                names = [_TYPE_NAMES.get(err.get("type", "")) for _rest, err in items]
                if all(names):
                    first = items[0][1]
                    where = " → ".join(str(p) for p in base) or "(トップレベル)"
                    unique = list(dict.fromkeys(names))
                    lines.append(
                        f"{where}: {' または '.join(unique)}を指定してください"
                        f"(指定値: {_short_repr(first.get('input'))})"
                    )
                    continue
        for rest, err in items:
            where = " → ".join(str(p) for p in (*base, *rest))
            # モデル全体の検証(参照先の存在確認など)は文言に場所を含む
            line = f"{where}: {_error_message(err)}" if where else _error_message(err)
            if line not in lines:
                lines.append(line)
    return lines


def _format_validation_error(
    exc: ValidationError,
    *,
    path: Path | None = None,
    lineno: int | None = None,
    hints: Iterable[ConfigWarning] = (),
) -> str:
    """ValidationError を日本語で整形する(モデル名・config.py の行・未知キーのヒント)。"""
    where = []
    if path is not None and lineno is not None:
        where.append(f"{path.name} {lineno} 行目")
    if exc.title:
        where.append(exc.title)
    head = "config.py の設定に問題があります"
    lines = [f"{head}({'、'.join(where)}):" if where else f"{head}:"]
    lines.extend(f"  - {line}" for line in _format_errors(exc.errors()))
    # 必須項目の欠落は、綴りを誤ったキー(colums → columns)が原因のことが多い。
    for hint in list(hints)[:_MAX_HINTS]:
        text = format_config_warning(hint, path) if path is not None else str(hint)
        lines.append(f"  ヒント: {text}")
    return "\n".join(lines)


def validation_messages(exc: ValidationError) -> list[str]:
    """ValidationError を ``位置: 説明`` の日本語の行にする(画面での設定の編集に使う)。"""
    return _format_errors(exc.errors())


def _format_syntax_error(exc: SyntaxError, path: Path) -> str:
    # config.py が import した別のファイルの誤りなら、そのファイル名を示す
    name = Path(exc.filename).name if exc.filename else path.name
    where = f"{name} {exc.lineno} 行目" if exc.lineno else name
    lines = [f"config.py の書き方(Python の文法)に誤りがあります({where}): {exc.msg}"]
    if exc.text and exc.text.strip():
        lines.append(f"  {exc.text.strip()}")
    return "\n".join(lines)


def _public_names() -> list[str]:
    import monitor_app

    return list(getattr(monitor_app, "__all__", []))


def _name_error_hint(exc: NameError, module: ModuleType) -> str | None:
    """未定義の名前について、import 漏れか綴りの誤りかを推測して案内する。"""
    name = getattr(exc, "name", None)
    if not name:
        return None
    public = _public_names()
    if name in public:
        return f"config.py の先頭に from monitor_app import {name} を追加してください"
    candidates = [*vars(module), *dir(builtins), *public]
    matches = difflib.get_close_matches(name, candidates, n=1, cutoff=0.7)
    if matches:
        return f"'{matches[0]}' の誤りではありませんか?"
    return None


def _format_exec_error(exc: Exception, path: Path, module: ModuleType) -> str:
    lineno = _config_lineno(exc.__traceback__, path)
    where = f"({path.name} {lineno} 行目)" if lineno is not None else ""
    detail = "".join(
        traceback.TracebackException(
            type(exc), exc, exc.__traceback__
        ).format_exception_only()
    ).strip()
    message = f"config.py の実行中にエラーが発生しました{where}: {detail}"
    if isinstance(exc, NameError) and "Did you mean" not in detail:
        hint = _name_error_hint(exc, module)
        if hint:
            message += f"\n  ヒント: {hint}"
    return message


# ---------------------------------------------------------------------------
# 読み込み
# ---------------------------------------------------------------------------
def resolve_config_path(path: str | os.PathLike) -> Path:
    """設定ファイルの指定を、読むファイルのパスにする(ファイルがあるかは確かめない)。

    ``~`` をホームフォルダに展開し、フォルダを指定したらその中の ``config.py`` にする。
    CLI の ``--config``・``MONITOR_CONFIG_PATH``(:func:`monitor_app.main.app_factory`)・
    :func:`load_config` が同じ規則で解釈する。
    """
    resolved = Path(path).expanduser()
    if resolved.is_dir():
        resolved = resolved / "config.py"
    return resolved


def load_config(path: str | Path) -> MonitorConfig:
    """``config.py`` のパスを受け取り、検証済みの :class:`MonitorConfig` を返す。

    プロジェクトのフォルダや ``~`` で始まるパスも受け付ける(:func:`resolve_config_path`)。
    """

    path = resolve_config_path(path)
    if not path.exists():
        raise ConfigError(f"config.py が見つかりません: {path}")

    module = _import_module_from_path(path)

    try:
        config = getattr(module, "config", None)
        if isinstance(config, MonitorConfig):
            return config
        if hasattr(module, "ALLOWED_TABLES"):
            return _load_v1(module, path)
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(exc)) from exc

    raise ConfigError(
        "config.py に MonitorConfig インスタンス(変数名 config)も "
        "v1 形式の ALLOWED_TABLES も見つかりませんでした"
    )


def format_config_warning(warning: ConfigWarning, path: str | Path) -> str:
    """警告を ``config.py 12 行目: ...`` の形にする(config.py 以外の場所なら本文だけ)。"""
    if warning.filename and warning.lineno and _same_file(warning.filename, path):
        return f"{Path(path).name} {warning.lineno} 行目: {warning}"
    return str(warning)


def load_config_with_warnings(path: str | Path) -> tuple[MonitorConfig, list[str]]:
    """:func:`load_config` と同じく読み込み、未知のキーなどの警告も返す。

    戻り値は ``(config, warnings)``。警告は ``config.py 12 行目: TableDef の未知の
    キー ...`` の形の文字列で、``monitor-app check`` がそのまま表示する。ここで
    集めた警告は Python の警告としては表示しない(呼び出し側が表示するため)。
    """
    with collect_config_warnings(display=False) as found:
        config = load_config(path)
    messages = [format_config_warning(w, path) for w in found]
    return config, list(dict.fromkeys(messages))
