"""API キーと公開範囲(Host ヘッダ)の判定。HTTP 層・CLI・点検で共有する。

- **API キー** … デプロイ用ファイルの生成時に作る推測されにくいキー(#D6-17)と、
  そのまま使うと危ないキー(説明用の値・短すぎる・ASCII 以外)の判定(#D6-07)。
- **表計算ソフトの数式** … CSV・Excel のエクスポートで、``=`` などで始まる文字列が
  数式として実行されないようにする(#D6-10。CSV インジェクション対策)。
- **Host ヘッダ** … このパソコンだけで待ち受けている(127.0.0.1 など)サーバーへの
  DNS リバインディング対策(#D6-03)。悪意のある Web ページが自分のドメイン名を
  127.0.0.1 に向け直すと、ブラウザはそのページと同じオリジンとして API を呼べて
  しまう。そうした要求は Host ヘッダが攻撃者のドメイン名になるため、localhost・
  IP アドレス・待ち受けのホスト名・``MONITOR_ALLOWED_HOSTS`` 以外の名前を断る。
  LAN・Docker(0.0.0.0)やリバースプロキシ配下(``MONITOR_ROOT_PATH`` /
  ``MONITOR_FORWARDED_ALLOW_IPS`` を設定)では、既定では確かめない。

FastAPI・Starlette には依存しない(CLI の起動を重くしない)。
"""

from __future__ import annotations

import ipaddress
import secrets
import socket
from collections.abc import Iterable
from dataclasses import dataclass
from typing import NamedTuple

# ---------------------------------------------------------------------------
# API キー
# ---------------------------------------------------------------------------
#: 生成する API キーの乱数のバイト数(URL で使える Base64 で 32 文字)。
API_KEY_BYTES = 24
#: これより短いキーは推測されやすいとして警告する。
MIN_API_KEY_LENGTH = 16
#: キーの作り方の例(案内に表示する)。
KEYGEN_COMMAND = 'python -c "import secrets; print(secrets.token_urlsafe(24))"'
#: 説明や例に使われがちな値(そのまま使うと誰でも推測できる)。小文字で比べる。
PLACEHOLDER_API_KEYS = frozenset(
    {
        "change-me",
        "changeme",
        "change_me",
        "secret",
        "password",
        "apikey",
        "api-key",
        "api_key",
        "your-api-key",
        "xxxxx",
    }
)


class KeyProblem(NamedTuple):
    """API キーの問題 1 件(``check`` の警告・起動時の警告に使う)。"""

    #: 機械可読な種別(``api-key-placeholder`` など)。
    code: str
    message: str
    hint: str


def generate_api_key() -> str:
    """推測されにくい API キー(英数字と ``-`` ``_`` だけ。.env・Compose で引用符が不要)。"""
    return secrets.token_urlsafe(API_KEY_BYTES)


def is_placeholder_api_key(key: str) -> bool:
    """説明用の値(``change-me`` や ``<長いランダムな文字列>`` など)か。"""
    normalized = key.strip().lower()
    if normalized in PLACEHOLDER_API_KEYS:
        return True
    # ドキュメントの「<...>」をそのまま貼り付けたもの
    return normalized.startswith("<") and normalized.endswith(">")


def api_key_problems(key: str | None) -> list[KeyProblem]:
    """設定された API キーの問題(未設定なら空)。キーの値はメッセージに含めない。"""
    if not key:
        return []
    problems: list[KeyProblem] = []
    if is_placeholder_api_key(key):
        problems.append(
            KeyProblem(
                "api-key-placeholder",
                "MONITOR_API_KEY が説明用の例の値のままです。誰でも推測できます",
                f"推測されにくい長い文字列に変えてください(作り方: {KEYGEN_COMMAND})",
            )
        )
    elif len(key) < MIN_API_KEY_LENGTH:
        problems.append(
            KeyProblem(
                "api-key-short",
                f"MONITOR_API_KEY が短すぎます({len(key)} 文字)。推測されやすいため"
                f" {MIN_API_KEY_LENGTH} 文字以上にしてください",
                f"作り方: {KEYGEN_COMMAND}",
            )
        )
    if not key.isascii():
        problems.append(
            KeyProblem(
                "api-key-non-ascii",
                "MONITOR_API_KEY に ASCII 以外の文字(日本語など)が含まれています。"
                "画面のログインでは使えますが、X-API-Key ヘッダでは送れないクライアントが"
                "あります",
                "API から使うなら、英数字と記号だけのキーにしてください",
            )
        )
    return problems


# ---------------------------------------------------------------------------
# 表計算ソフトの数式(CSV・Excel のエクスポート)
# ---------------------------------------------------------------------------
#: 表計算ソフトが数式として解釈し得る先頭の文字(OWASP の CSV インジェクション対策)。
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def neutralize_formula(value: object) -> object:
    """数式として解釈され得る文字列の先頭に ``'`` を付ける(数値などの値はそのまま)。

    Excel などは ``'`` で始まるセルを文字列として扱う(``'`` は表示されない)。
    """
    if isinstance(value, str) and value.startswith(FORMULA_PREFIXES):
        return "'" + value
    return value


# ---------------------------------------------------------------------------
# Host ヘッダ(DNS リバインディング対策)
# ---------------------------------------------------------------------------
#: ``MONITOR_ALLOWED_HOSTS`` で「確かめない」を表す値。
ALLOW_ANY_HOST = "*"


def _strip_zone(name: str) -> str:
    return name.split("%", 1)[0]


def _is_ip_literal(name: str) -> bool:
    try:
        ipaddress.ip_address(_strip_zone(name))
    except ValueError:
        return False
    return True


def is_loopback_host(host: str | None) -> bool:
    """このマシン自身を指す名前・アドレスか(localhost・*.localhost・127.0.0.0/8・::1)。

    ブラウザは ``*.localhost`` の名前も自分自身に解決する(DNS には問い合わせない)。
    """
    if not host:
        return False
    name = host.strip().strip("[]").lower().rstrip(".")
    if name == "localhost" or name.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(_strip_zone(name)).is_loopback
    except ValueError:
        return False


def host_header_name(value: str) -> str:
    """Host ヘッダの値からホスト名を取り出す(小文字。ポート・末尾のドット・IPv6 の角括弧を除く)。"""
    value = value.strip().lower()
    if value.startswith("["):
        end = value.find("]")
        return value[1:end] if end != -1 else value[1:]
    if value.count(":") == 1:
        value = value.split(":", 1)[0]
    return value.rstrip(".")


def parse_allowed_hosts(value: str | None) -> list[str]:
    """``MONITOR_ALLOWED_HOSTS`` の値(カンマ・空白区切り)を、比べる形の名前のリストにする。"""
    if not value:
        return []
    names: list[str] = []
    for raw in value.replace(",", " ").split():
        if raw == ALLOW_ANY_HOST:
            names.append(ALLOW_ANY_HOST)
        elif raw.startswith("*."):
            names.append("*." + host_header_name(raw[2:]))
        else:
            name = host_header_name(raw)
            if name:
                names.append(name)
    return names


def _machine_names() -> set[str]:
    try:
        name = socket.gethostname().strip().lower().rstrip(".")
    except OSError:  # pragma: no cover - 通常は失敗しない
        return set()
    return {name} if name else set()


@dataclass(frozen=True)
class HostPolicy:
    """受け付ける Host ヘッダの名前。

    localhost・``*.localhost``・IP アドレス(DNS を使わないためリバインディングに
    ならない)と、Host ヘッダの無い要求(ブラウザは必ず送る)は常に受け付ける。
    """

    #: 受け付ける名前(小文字)。
    names: frozenset[str]
    #: 受け付けるサブドメインの接尾辞(``*.example.com`` → ``.example.com``)。
    suffixes: tuple[str, ...] = ()
    #: True なら、ループバックのアドレス(127.0.0.1 など)に届いた接続だけを確かめる
    #: (既定の自動判定。リバインディングで狙われるのはこの経路だけ)。
    loopback_only: bool = False

    def applies_to(self, server: Iterable[object] | None) -> bool:
        """ASGI の ``scope["server"]``(接続を受けたアドレス)の要求を確かめるか。"""
        if not self.loopback_only:
            return True
        if not server:
            # Unix ソケットなど。ブラウザが直接つなげる経路ではない。
            return False
        return is_loopback_host(str(next(iter(server), "")))

    def allows(self, host_header: str | None) -> bool:
        if not host_header:
            return True
        name = host_header_name(host_header)
        if not name or _is_ip_literal(name) or is_loopback_host(name):
            return True
        if name in self.names:
            return True
        return any(name.endswith(suffix) for suffix in self.suffixes)


def host_policy(
    *,
    bound_host: str,
    allowed_hosts: str | None = None,
    root_path: str = "",
    forwarded_allow_ips: str | None = None,
) -> HostPolicy | None:
    """Host ヘッダの確かめ方。確かめないなら None。

    - ``allowed_hosts`` が ``*`` … 確かめない。
    - ``allowed_hosts`` に名前がある … 待ち受けのアドレスに関わらず、その名前と既定の
      名前(localhost・IP アドレス・待ち受けのホスト名・このマシンのホスト名)だけを
      受け付ける(``*.example.com`` でサブドメインも)。
    - 未設定(既定)… ループバック(127.0.0.1 など)で待ち受けていて、リバースプロキシの
      設定(``root_path`` / ``forwarded_allow_ips``)も無いときだけ、ループバックに届いた
      接続の Host を既定の名前に限る。LAN・Docker(0.0.0.0)では確かめない。
    """
    entries = parse_allowed_hosts(allowed_hosts)
    if ALLOW_ANY_HOST in entries:
        return None
    base = _machine_names()
    bound = host_header_name(bound_host or "")
    if bound and not _is_ip_literal(bound):
        base.add(bound)
    if entries:
        names = base | {e for e in entries if not e.startswith("*.")}
        suffixes = tuple(e[1:] for e in entries if e.startswith("*."))
        return HostPolicy(frozenset(names), suffixes)
    if not is_loopback_host(bound_host) or root_path or forwarded_allow_ips:
        return None
    return HostPolicy(frozenset(base), loopback_only=True)
