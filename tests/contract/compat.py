"""公開契約の互換性判定ルール(ベースライン = 過去の約束 vs 現行コード)。

既存の利用者を壊す変更だけを「違反」として日本語のメッセージで列挙する。追加
(新しいパス・パラメータ・プロパティ・コマンド・オプション)は常に許す。

型ノード(表現は ``snapshot`` モジュールの docstring を参照)の比較は部分型の判定で、
2 つのモードがある(``integer`` は ``number`` の、構造を持つオブジェクトは自由形式
``object`` の部分型、すべての型は ``any`` の部分型とみなす)。

- ``request`` … クライアントが送る値。ベースラインで受け付けた値を現行もすべて受け付ける
  (ベースライン ≤ 現行)なら互換。``integer`` → ``number`` の拡張や ``any`` への緩和は可。
  新たな必須プロパティ、任意だったプロパティの必須化は不可。
- ``response`` … サーバが返す値。現行が返し得る値がすべてベースラインの型に収まる
  (現行 ≤ ベースライン)なら互換。プロパティの追加は可。プロパティの削除、常に返って
  いたものが省略され得るようになること、新しい型(``null`` を含む)を返し得るように
  なること、構造の規定が ``any`` / 自由形式 ``object`` に失われることは不可。

どちらのモードでも、ベースラインのプロパティは現行にも残っていなければならない。
"""

from __future__ import annotations

from typing import Any

_MISSING = "<missing>"
_MAX_DEPTH = 32  # 自己参照スキーマへの保険(現状のスキーマに再帰は無い)


# ---------------------------------------------------------------------------
# 型ノード
# ---------------------------------------------------------------------------
def node_violations(
    base: list,
    cur: list,
    *,
    mode: str,
    where: str,
    base_defs: dict | None = None,
    cur_defs: dict | None = None,
) -> list[str]:
    """ベースラインの型ノード ``base`` と現行の ``cur`` を比べ、互換でない理由を返す。

    ``base_defs`` / ``cur_defs`` は ``{"ref": 名前}`` を解決する定義表(OpenAPI の
    components)。名前が変わっても構造が互換なら違反にしない(ワイヤ上の互換を見る)。
    """
    if mode not in ("request", "response"):
        raise ValueError(mode)
    ctx = (mode, base_defs or {}, cur_defs or {})
    return _node(base, cur, ctx, where, 0)


def _expand(node: list, defs: dict, seen: tuple = ()) -> list:
    """``{"ref": 名前}`` を定義の中身の候補へ展開する。"""
    out: list = []
    for alt in node:
        if isinstance(alt, dict) and "ref" in alt:
            name = alt["ref"]
            if name in seen or name not in defs:
                out.append("any")
            else:
                out.extend(_expand(defs[name], defs, (*seen, name)))
        else:
            out.append(alt)
    return out


def _describe(alt: Any) -> str:
    if isinstance(alt, str):
        return alt
    if "array" in alt:
        return "array"
    if "object" in alt:
        return "object{" + ", ".join(alt["object"]["properties"]) + "}"
    return str(alt)


def _node(base: list, cur: list, ctx: tuple, where: str, depth: int) -> list[str]:
    if depth > _MAX_DEPTH:
        return []
    mode, base_defs, cur_defs = ctx
    base_alts, cur_alts = _expand(base, base_defs), _expand(cur, cur_defs)
    if mode == "response":
        # 現行が返し得る型は、どれもベースラインが返していた型に収まらなければならない
        subs, sups = cur_alts, base_alts
    else:
        # ベースラインで受け付けた型は、どれも現行で受け付けられなければならない
        subs, sups = base_alts, cur_alts
    errors: list[str] = []
    for sub in subs:
        best: list[str] | None = None
        for sup in sups:
            found = _alt(sub, sup, ctx, where, depth)
            if not found:
                best = []
                break
            if best is None or len(found) < len(best):
                best = found  # もっとも近い候補の理由を報告する
        if best is None:
            best = [_mismatch(sub, sups, mode, where)]
        errors.extend(best)
    return errors


def _mismatch(sub: Any, sups: list, mode: str, where: str) -> str:
    allowed = " | ".join(_describe(s) for s in sups) or "(なし)"
    if mode == "response":
        return f"{where}: {_describe(sub)} を返し得るようになりました(以前は {allowed})"
    return f"{where}: {_describe(sub)} を受け付けなくなりました(現行は {allowed})"


def _alt(sub: Any, sup: Any, ctx: tuple, where: str, depth: int) -> list[str]:
    """``sub`` が ``sup`` の部分型かを調べる(空リストなら部分型)。"""
    mode = ctx[0]
    if sup == "any" or sub == sup:
        return []
    if isinstance(sub, str):
        if sub == "integer" and sup == "number":
            return []
        return [_mismatch(sub, [sup], mode, where)]
    if "array" in sub:
        if not (isinstance(sup, dict) and "array" in sup):
            return [_mismatch(sub, [sup], mode, where)]
        base_items, cur_items = (
            (sup["array"], sub["array"])
            if mode == "response"
            else (sub["array"], sup["array"])
        )
        if not base_items:
            return []  # ベースラインで要素を観測していない(空配列)
        if not cur_items and mode == "response":
            return [f"{where}: 配列に要素が返らなくなりました"]
        return _node(base_items, cur_items, ctx, f"{where}[]", depth + 1)
    if "object" in sub:
        if sup == "object":
            return []  # 構造を持つオブジェクトは自由形式オブジェクトの部分型
        if not (isinstance(sup, dict) and "object" in sup):
            return [_mismatch(sub, [sup], mode, where)]
        base_body, cur_body = (
            (sup["object"], sub["object"])
            if mode == "response"
            else (sub["object"], sup["object"])
        )
        return _object(base_body, cur_body, ctx, where, depth)
    return [f"{where}: 未知の型ノード {sub!r}"]


def _object(base: dict, cur: dict, ctx: tuple, where: str, depth: int) -> list[str]:
    errors: list[str] = []
    for name, node in base["properties"].items():
        if name not in cur["properties"]:
            errors.append(f"{where}: プロパティ '{name}' がなくなりました")
            continue
        errors += _node(
            node, cur["properties"][name], ctx, f"{where}.{name}", depth + 1
        )
    base_required, cur_required = set(base["required"]), set(cur["required"])
    if ctx[0] == "response":
        for name in sorted((base_required - cur_required) & set(cur["properties"])):
            errors.append(f"{where}: '{name}' が常には返らなくなりました")
    else:
        for name in sorted(cur_required - base_required):
            errors.append(f"{where}: '{name}' が新たに必須になりました")
    return errors


# ---------------------------------------------------------------------------
# OpenAPI
# ---------------------------------------------------------------------------
def openapi_violations(base: dict, cur: dict) -> list[str]:
    """操作(メソッド+パス)・パラメータ・リクエストボディ・応答・認証の互換性。"""
    errors: list[str] = []
    defs = {"base_defs": base["components"], "cur_defs": cur["components"]}
    scheme_alias = _security_alias(
        base["security_schemes"], cur["security_schemes"], errors
    )
    for key, bop in base["operations"].items():
        cop = cur["operations"].get(key)
        if cop is None:
            errors.append(f"{key}: 操作がなくなりました")
            continue
        errors += _parameter_violations(key, bop["parameters"], cop["parameters"], defs)
        errors += _body_violations(key, bop["request_body"], cop["request_body"], defs)
        for code, bcontent in bop["responses"].items():
            ccontent = cop["responses"].get(code)
            if ccontent is None:
                errors.append(f"{key}: 応答ステータス {code} がなくなりました")
                continue
            for ctype, bnode in bcontent.items():
                if ctype not in ccontent:
                    errors.append(f"{key} {code}: {ctype} を返さなくなりました")
                    continue
                errors += node_violations(
                    bnode,
                    ccontent[ctype],
                    mode="response",
                    where=f"{key} {code} $",
                    **defs,
                )
        for scheme in bop["security"]:
            if scheme in scheme_alias and scheme_alias[scheme] not in cop["security"]:
                errors.append(f"{key}: 認証方式 {scheme} が外れました")
    return errors


def _security_alias(base: dict, cur: dict, errors: list[str]) -> dict[str, str]:
    """ベースラインの認証方式名 → 同じ定義(type/in/name)を持つ現行の方式名。"""
    alias = {}
    for name, definition in base.items():
        match = next((n for n, d in cur.items() if d == definition), None)
        if match is None:
            errors.append(f"securitySchemes: {name} {definition} がなくなりました")
        else:
            alias[name] = match
    return alias


def _parameter_violations(key: str, base: dict, cur: dict, defs: dict) -> list[str]:
    errors = []
    for pkey, bparam in base.items():
        cparam = cur.get(pkey)
        if cparam is None:
            errors.append(f"{key}: パラメータ {pkey} がなくなりました")
            continue
        if cparam["required"] != bparam["required"]:
            errors.append(
                f"{key}: パラメータ {pkey} の required が"
                f" {bparam['required']} → {cparam['required']} に変わりました"
            )
        errors += node_violations(
            bparam["type"],
            cparam["type"],
            mode="request",
            where=f"{key} パラメータ {pkey}",
            **defs,
        )
        if "default" in bparam and cparam.get("default", _MISSING) != bparam["default"]:
            errors.append(
                f"{key}: パラメータ {pkey} の既定値が {bparam['default']!r}"
                f" → {cparam.get('default', _MISSING)!r} に変わりました"
            )
    for pkey, cparam in cur.items():
        if pkey not in base and cparam["required"]:
            errors.append(f"{key}: 必須パラメータ {pkey} が追加されました")
    return errors


def _body_violations(key: str, base: dict | None, cur: dict | None, defs: dict) -> list:
    if base is None:
        if cur is not None and cur["required"]:
            return [f"{key}: リクエストボディが新たに必須になりました"]
        return []
    if cur is None:
        return [f"{key}: リクエストボディを受け付けなくなりました"]
    errors = []
    if cur["required"] != base["required"]:
        errors.append(
            f"{key}: リクエストボディの required が"
            f" {base['required']} → {cur['required']} に変わりました"
        )
    for ctype, bnode in base["content"].items():
        if ctype not in cur["content"]:
            errors.append(f"{key}: リクエストボディ {ctype} を受け付けなくなりました")
            continue
        errors += node_violations(
            bnode,
            cur["content"][ctype],
            mode="request",
            where=f"{key} リクエストボディ $",
            **defs,
        )
    return errors


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _cli_type_ok(base_type: str, cur_type: str) -> bool:
    if cur_type == base_type:
        return True
    if base_type == "int" and cur_type == "float":
        return True
    return cur_type == "str" and base_type != "bool"  # 文字列は何でも受け付ける


def cli_violations(base: dict, cur: dict) -> list[str]:
    """コマンド・オプション(フラグ文字列・is_flag・既定値)・位置引数の互換性。"""
    errors = _param_violations(
        "(グローバル)", base["group_params"], cur["group_params"]
    )
    for name, bcommand in base["commands"].items():
        ccommand = cur["commands"].get(name)
        if ccommand is None:
            errors.append(f"コマンド '{name}' がなくなりました")
            continue
        errors += _param_violations(name, bcommand["params"], ccommand["params"])
    return errors


def _param_violations(where: str, base: list, cur: list) -> list[str]:
    errors: list[str] = []
    matched: set[int] = set()
    by_flag = {
        flag: index
        for index, param in enumerate(cur)
        if param["kind"] == "option"
        for flag in (*param["opts"], *param["secondary_opts"])
    }
    for bparam in (p for p in base if p["kind"] == "option"):
        for flag in (*bparam["opts"], *bparam["secondary_opts"]):
            if flag not in by_flag:
                errors.append(f"{where}: オプション {flag} がなくなりました")
                continue
            matched.add(by_flag[flag])
            cparam = cur[by_flag[flag]]
            for attr in ("is_flag", "multiple", "default"):
                if cparam[attr] != bparam[attr]:
                    errors.append(
                        f"{where}: {flag} の {attr} が {bparam[attr]!r}"
                        f" → {cparam[attr]!r} に変わりました"
                    )
            if cparam["required"] and not bparam["required"]:
                errors.append(f"{where}: {flag} が必須になりました")
            if not _cli_type_ok(bparam["type"], cparam["type"]):
                errors.append(
                    f"{where}: {flag} の型が {bparam['type']} → {cparam['type']}"
                    " に変わりました"
                )
    cur_args = [i for i, p in enumerate(cur) if p["kind"] == "argument"]
    for position, bparam in enumerate(p for p in base if p["kind"] == "argument"):
        if position >= len(cur_args):
            errors.append(
                f"{where}: {position + 1} 番目の位置引数 {bparam['name']} がなくなりました"
            )
            continue
        matched.add(cur_args[position])
        cparam = cur[cur_args[position]]
        if cparam["nargs"] != bparam["nargs"]:
            errors.append(f"{where}: 位置引数 {bparam['name']} の個数が変わりました")
        if cparam["required"] and not bparam["required"]:
            errors.append(f"{where}: 位置引数 {bparam['name']} が必須になりました")
        if not _cli_type_ok(bparam["type"], cparam["type"]):
            errors.append(f"{where}: 位置引数 {bparam['name']} の型が変わりました")
    for index, cparam in enumerate(cur):
        if index not in matched and cparam["required"]:
            errors.append(
                f"{where}: 必須の {cparam['kind']} '{cparam['name']}' が追加されました"
            )
    return errors


# ---------------------------------------------------------------------------
# 関数シグネチャ
# ---------------------------------------------------------------------------
_POSITIONAL = ("POSITIONAL_ONLY", "POSITIONAL_OR_KEYWORD")
_VARIADIC = ("VAR_POSITIONAL", "VAR_KEYWORD")


def signature_violations(where: str, base: list, cur: list) -> list[str]:
    """既存の呼び出し(位置渡し・キーワード渡し・既定値頼み)が壊れないか。"""
    errors = []
    cur_names = [p["name"] for p in cur]
    for position, bparam in enumerate(base):
        name = bparam["name"]
        if name not in cur_names:
            errors.append(f"{where}: 引数 '{name}' がなくなりました")
            continue
        cparam = cur[cur_names.index(name)]
        kind_ok = cparam["kind"] == bparam["kind"] or (
            # キーワード専用 → 位置/キーワード両用への緩和は可
            bparam["kind"] == "KEYWORD_ONLY"
            and cparam["kind"] == "POSITIONAL_OR_KEYWORD"
        )
        if not kind_ok:
            errors.append(
                f"{where}: 引数 '{name}' の種類が {bparam['kind']} → {cparam['kind']}"
                " に変わりました"
            )
        if bparam["kind"] in _POSITIONAL and cur_names.index(name) != position:
            errors.append(f"{where}: 引数 '{name}' の位置が変わりました")
        if "default" in bparam and cparam.get("default", _MISSING) != bparam["default"]:
            errors.append(
                f"{where}: 引数 '{name}' の既定値が {bparam['default']!r}"
                f" → {cparam.get('default', _MISSING)!r} に変わりました"
            )
    base_names = {p["name"] for p in base}
    for cparam in cur:
        if (
            cparam["name"] not in base_names
            and "default" not in cparam
            and cparam["kind"] not in _VARIADIC
        ):
            errors.append(
                f"{where}: 既定値のない引数 '{cparam['name']}' が追加されました"
            )
    return errors


# ---------------------------------------------------------------------------
# 実レスポンス
# ---------------------------------------------------------------------------
def response_violations(where: str, expected: dict, observed: dict) -> list[str]:
    """``snapshot.perform`` の観測値同士を比べる。"""
    errors = []
    for key in ("status", "content_type", "event", "first_line"):
        if key in expected and observed.get(key, _MISSING) != expected[key]:
            errors.append(
                f"{where}: {key} が {expected[key]!r}"
                f" → {observed.get(key, _MISSING)!r} に変わりました"
            )
    if "shape" in expected:
        if "shape" not in observed:
            errors.append(f"{where}: JSON の本文が返らなくなりました")
        else:
            errors += node_violations(
                expected["shape"],
                observed["shape"],
                mode="response",
                where=f"{where} $",
            )
    for group in ("pin_json", "pin_headers", "header_contains"):
        for name, value in expected.get(group, {}).items():
            actual = observed.get(group, {}).get(name, _MISSING)
            if actual != value:
                errors.append(
                    f"{where}: {group}[{name}] が {value!r} → {actual!r} に変わりました"
                )
    return errors
