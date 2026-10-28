"""Web UI の配信部品: テンプレートの URL 生成・静的ファイルのキャッシュ・サブパス・圧縮。

:func:`~monitor_app.main.create_app` が組み込む HTTP 層の部品で、サービス層には依存しない。

- **サブパス配信**(``MONITOR_ROOT_PATH``): リバースプロキシの ``/monitor/`` 配下などで
  公開できるようにする。プロキシがプレフィックスを外して転送しても、外さずに転送しても
  同じ設定で動く(:class:`RootPathMiddleware`)。
- **ルート相対 URL**: テンプレートの ``url_for`` はスキームとホストを含まない
  ``/monitor/static/...`` 形式を返す。TLS 終端プロキシの裏でも ``http://`` の
  アセット URL(混在コンテンツ)にならない。
- **静的ファイルのキャッシュ**: テンプレートは ``?v=<バージョン>-<静的ファイルのハッシュ>``
  付きの URL を出し、その要求には 1 年の不変キャッシュを指示する
  (:class:`VersionedStaticFiles`)。アップグレードや静的ファイルの変更で URL が変わる
  ため、古い JS/CSS が使われ続けることはない。別の版の ``?v=`` (古いページ・入れ替え中の
  別インスタンス)には不変を指示しない。
- **圧縮**: 大きなレスポンス(ビューの JSON・静的ファイル)を gzip で返す。SSE は
  イベントごとに即時送る必要があるため圧縮しない(:class:`CompressionMiddleware`)。
- **Host ヘッダの確認**: この PC だけで待ち受けるサーバーへの DNS リバインディングを
  断る(:class:`HostGuardMiddleware`。判定は :mod:`monitor_app.security`)。
"""

from __future__ import annotations

import hashlib
import inspect
import logging
import os
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote

import jinja2
from fastapi.templating import Jinja2Templates
from starlette.datastructures import Headers
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import HTTPConnection
from starlette.responses import PlainTextResponse, RedirectResponse, Response
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from .. import __version__
from ..security import HostPolicy, host_header_name

logger = logging.getLogger("monitor_app.web")

try:  # Starlette 1.3 以降のどの版にもあるが、念のため名前の変更に備える
    from starlette.middleware.gzip import DEFAULT_EXCLUDED_CONTENT_TYPES
except ImportError:  # pragma: no cover
    DEFAULT_EXCLUDED_CONTENT_TYPES = ("text/event-stream",)


def static_digest(static_dir: str | os.PathLike[str]) -> str:
    """静的ファイル一式(パスと中身)から作る短いハッシュ。読めなければ ``""``。"""
    root = Path(static_dir)
    if not root.is_dir():
        return ""
    digest = hashlib.sha256()
    try:
        for path in sorted(p for p in root.rglob("*") if p.is_file()):
            digest.update(path.relative_to(root).as_posix().encode("utf-8") + b"\0")
            digest.update(path.read_bytes())
    except OSError:
        return ""
    return digest.hexdigest()[:10]


_STATIC_DIGEST = static_digest(Path(__file__).with_name("static"))
#: 静的ファイルの URL に付けるバージョン(``?v=``)。配布物のバージョンに、静的ファイルの
#: 中身のハッシュを足したもの(``2.4.0-1a2b3c4d5e``)。バージョンを上げずに中身が変わる
#: 場合(git のチェックアウトからのビルド・開発中)も URL が変わり、ブラウザが 1 年間
#: キャッシュした古い JS/CSS を使い続けることがない。
ASSET_VERSION = f"{__version__}-{_STATIC_DIGEST}" if _STATIC_DIGEST else __version__
#: ``?v=`` 付きの要求への Cache-Control。版ごとに URL が変わるので再検証も不要。
IMMUTABLE_CACHE_CONTROL = "public, max-age=31536000, immutable"
#: ``?v=`` なしの要求への Cache-Control。保存はするが、使う前に毎回 ETag で再検証させる。
REVALIDATE_CACHE_CONTROL = "no-cache"
#: gzip 圧縮の既定。2 万行(約 2 MB)のビュー JSON で level 9(Starlette の既定)は
#: 約 170 ms(8.9 倍に圧縮)、level 5 は約 12 ms(7.6 倍)だった(開発機での実測)。
#: Raspberry Pi などの非力なサーバーでも応答を重くしない値にする。
GZIP_MINIMUM_SIZE = 1024
GZIP_COMPRESSLEVEL = 5
XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

_VERSION_QUERY = "v=" + quote(ASSET_VERSION, safe="")


# ---------------------------------------------------------------------------
# サブパス(root_path)
# ---------------------------------------------------------------------------
def app_root_path(source: HTTPConnection | Scope) -> str:
    """アプリの公開サブパス(``/monitor`` 形式。ルートで公開しているなら ``""``)。

    リクエストでも ASGI の scope でもよい。``Mount`` の内側(静的ファイル)では
    scope の ``root_path`` がマウント先まで伸びるため、Starlette が保持する
    アプリ全体の値(``app_root_path``)を優先する。
    """
    scope = source.scope if isinstance(source, HTTPConnection) else source
    if "app_root_path" in scope:
        root = scope["app_root_path"]
    else:
        root = scope.get("root_path", "")
    return (root or "").rstrip("/")


def cookie_path(source: HTTPConnection | Scope) -> str:
    """Cookie の ``Path``(サブパス。ルートで公開しているなら ``/``)。

    ブラウザは要求の(パーセントエンコードされた)パスと前方一致で比べるうえ、
    ヘッダは latin-1 でしか送れないため、日本語などのサブパスはエンコードして渡す。
    """
    return quote(app_root_path(source)) or "/"


class RootPathMiddleware:
    """サブパス配信を、プロキシの転送方式に依らず成立させる ASGI ミドルウェア。

    ASGI 仕様では ``scope["path"]`` は ``root_path`` を含む。Starlette はその前提で
    ``root_path`` を取り除いてからルーティングするが、プレフィックスを外して転送する
    プロキシ(nginx の ``proxy_pass http://app:9990/;``)の後ろでは ``path`` に含まれず、
    ルートは一致しても ``/static`` のマウント内で 404 になる。

    ここで ``root_path`` を設定し、``path`` が ``root_path`` で始まっていなければ
    前置して仕様どおりの形に揃える。プレフィックスを外さないプロキシ
    (``proxy_pass http://app:9990;``)や ``uvicorn --root-path`` では ``path`` が既に
    ``root_path`` で始まっているので変更しない。``root_path`` が空(既定)なら
    サーバーが渡した値をそのまま使う(今までと同じ挙動)。

    末尾のスラッシュなしでサブパスそのもの(``/monitor``)へ来た要求は、ここで
    ``/monitor/`` へルート相対でリダイレクトする。Starlette の redirect_slashes に
    任せると Host ヘッダとスキームから絶対 URL を作るため、TLS 終端プロキシの裏で
    ``http://`` になったり、プロキシが Host を書き換えると内部のアドレスになったりする。
    """

    def __init__(self, app: ASGIApp, root_path: str = "") -> None:
        self.app = app
        self.root_path = (root_path or "").rstrip("/")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):
            root = self.root_path or (scope.get("root_path") or "").rstrip("/")
            if root:
                if scope["type"] == "http" and scope.get("path") == root:
                    await _slash_redirect(scope, root)(scope, receive, send)
                    return
                scope = _with_root_path(scope, root)
        await self.app(scope, receive, send)


def _slash_redirect(scope: Scope, root: str) -> Response:
    """``/monitor`` → ``/monitor/`` (クエリ文字列は保つ)。"""
    location = quote(root) + "/"
    query = scope.get("query_string", b"")
    if query:
        location += "?" + query.decode("latin-1")
    return RedirectResponse(location, status_code=307)


def _with_root_path(scope: Scope, root: str) -> Scope:
    scope = dict(scope)
    scope["root_path"] = root
    path = scope.get("path", "")
    if path != root and not path.startswith(root + "/"):
        scope["path"] = root + path
        raw_path = scope.get("raw_path")
        if raw_path is not None:
            scope["raw_path"] = quote(root).encode("ascii") + raw_path
    return scope


# ---------------------------------------------------------------------------
# テンプレート
# ---------------------------------------------------------------------------
@jinja2.pass_context
def _url_for(context: dict, name: str, /, **path_params: Any) -> str:
    """ルート相対の URL(スキーム・ホストなし。サブパスは含む)。

    パスの値はパーセントエンコードする(Starlette はそのまま埋め込むため、ビュー名に
    ``#`` ``?`` ``%`` などがあるとリンクが壊れる)。``/`` は静的ファイルのパスの区切り。
    """
    request = context["request"]
    params = {key: quote(str(value), safe="/") for key, value in path_params.items()}
    return request.url_for(name, **params).path


@jinja2.pass_context
def _static_url(context: dict, path: str) -> str:
    """静的ファイルのバージョン付き URL(``/static/<path>?v=<版>``)。"""
    return f"{_url_for(context, 'static', path=path)}?{_VERSION_QUERY}"


@jinja2.pass_context
def _root_path(context: dict) -> str:
    """JS に渡すサブパス(``<meta name="monitor-root">``)。"""
    return app_root_path(context["request"])


@jinja2.pass_context
def _config_revision(context: dict) -> str:
    """使う設定の版(``<meta name="monitor-config">``)。変わったら画面が読み直す。"""
    return context["request"].app.state.services.config_revision


def create_templates(directory: str | os.PathLike[str]) -> Jinja2Templates:
    """UI 用の Jinja2Templates。

    テンプレートで使える関数:
      ``url_for(name, **params)`` … ルート相対の URL(Starlette 既定の絶対 URL を置き換え)
      ``static_url(path)``        … バージョン付きの静的ファイル URL
      ``root_path()``             … サブパス(``/monitor`` または空文字)
      ``config_revision()``       … 使う設定の版(画面で設定を変えたら読み直すため)
    """
    templates = Jinja2Templates(directory=str(directory))
    templates.env.globals.update(
        url_for=_url_for,
        static_url=_static_url,
        root_path=_root_path,
        config_revision=_config_revision,
    )
    return templates


# ---------------------------------------------------------------------------
# 静的ファイル
# ---------------------------------------------------------------------------
def _requests_current_version(scope: Scope) -> bool:
    query = scope.get("query_string", b"").decode("latin-1")
    return any(key == "v" and value == ASSET_VERSION for key, value in parse_qsl(query))


class VersionedStaticFiles(StaticFiles):
    """``?v=<現在の版>`` の要求には長期キャッシュ、それ以外には再検証を指示する StaticFiles。

    テンプレートは常に ``?v=<版>`` 付きで参照する(``static_url``)ので、ページを
    移動してもアセットの再取得・再検証は起きない。``?v=`` なしの直接参照は
    ``no-cache``(ETag による 304 で軽く再検証)になる。

    ``?v=`` が別の版のときも ``no-cache`` にする。アップグレード中に新旧のインスタンスが
    並ぶと、新しいページの ``?v=<新版>`` を古いインスタンスが受けることがあり、
    そこで不変を指示すると古い中身が 1 年間その URL に居座ってしまう。
    """

    def file_response(
        self,
        full_path: str | os.PathLike[str],
        stat_result: os.stat_result,
        scope: Scope,
        status_code: int = 200,
    ) -> Response:
        response = super().file_response(full_path, stat_result, scope, status_code)
        response.headers["Cache-Control"] = (
            IMMUTABLE_CACHE_CONTROL
            if _requests_current_version(scope)
            else REVALIDATE_CACHE_CONTROL
        )
        return response


# ---------------------------------------------------------------------------
# 圧縮
# ---------------------------------------------------------------------------
def _gzip_options(minimum_size: int, compresslevel: int) -> dict[str, Any]:
    options: dict[str, Any] = {
        "minimum_size": minimum_size,
        "compresslevel": compresslevel,
    }
    # 除外する Content-Type を指定できる版(Starlette 1.9 など)では SSE を明示的に
    # 除外する。指定できない版(1.3 系)も既定で text/event-stream を除外している。
    # xlsx(ビューの Excel エクスポート)は中身が zip なので圧縮し直さない。
    if "exclude_content_types" in inspect.signature(GZipMiddleware).parameters:
        excluded = tuple(DEFAULT_EXCLUDED_CONTENT_TYPES)
        for content_type in ("text/event-stream", XLSX_CONTENT_TYPE):
            if content_type not in excluded:
                excluded += (content_type,)
        options["exclude_content_types"] = excluded
    return options


def _bypasses_compression(scope: Scope) -> bool:
    """圧縮器を通さない要求か(SSE の購読、Range 付きの部分取得)。"""
    for key, value in scope.get("headers", ()):
        if key == b"accept" and b"text/event-stream" in value:
            return True  # EventSource は必ずこの Accept を送る
        if key == b"range":
            return True  # 部分応答(206)を圧縮すると壊れる版がある
    return False


def accepts_gzip(scope: Scope) -> bool:
    """圧縮ミドルウェアが本文を gzip にする要求か(Accept-Encoding に gzip があり、SSE・Range でない)。

    圧縮済みの本文をキャッシュして返すエンドポイント(ビューの GET)が、ミドルウェアと
    同じ条件で判定するのに使う。``Content-Encoding`` の付いた応答はミドルウェアが
    そのまま通すので、二重に圧縮されることはない。
    """
    if _bypasses_compression(scope):
        return False
    return "gzip" in Headers(scope=scope).get("accept-encoding", "")


class CompressionMiddleware:
    """レスポンスを gzip で圧縮する(``MONITOR_COMPRESS_RESPONSES``)。

    Starlette の GZipMiddleware を、SSE を確実に素通しする形で包む。SSE は
    Content-Type で除外されるうえ、EventSource の要求(``Accept: text/event-stream``)は
    圧縮器自体を経由させないので、イベントがバッファされずに 1 件ずつ届く。
    304(本文なし)や小さな応答(``minimum_size`` 未満)は圧縮しない。
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        minimum_size: int = GZIP_MINIMUM_SIZE,
        compresslevel: int = GZIP_COMPRESSLEVEL,
    ) -> None:
        self.app = app
        self.gzip = GZipMiddleware(app, **_gzip_options(minimum_size, compresslevel))

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and not _bypasses_compression(scope):
            await self.gzip(scope, receive, send)
        else:
            await self.app(scope, receive, send)


# ---------------------------------------------------------------------------
# Host ヘッダ(DNS リバインディング対策)
# ---------------------------------------------------------------------------
#: 許可していない Host ヘッダへの応答(要求の Host の値は含めない)。
INVALID_HOST_MESSAGE = (
    "Invalid host header\n"
    "このサーバーは、この名前(Host ヘッダ)での接続を受け付けません。"
    "サーバーの管理者は MONITOR_ALLOWED_HOSTS を確認してください。\n"
)
#: 断った Host を警告に記録する名前の数の上限(ログを溢れさせない)。
_MAX_WARNED_HOSTS = 32


class HostGuardMiddleware:
    """許可していない Host ヘッダの要求を 400 で断る ASGI ミドルウェア(#D6-03)。

    悪意のある Web ページが自分のドメイン名を 127.0.0.1 に向け直す(DNS リバインディング)
    と、ブラウザはこの PC だけで待ち受けるサーバーにも「そのページと同じオリジン」として
    書き込み・削除の要求を送れてしまう。そうした要求の Host ヘッダは攻撃者のドメイン名に
    なるため、:class:`~monitor_app.security.HostPolicy` で許可した名前以外を断る。
    断った名前は 1 回だけ警告に記録し、設定の直し方(``MONITOR_ALLOWED_HOSTS``)を示す。
    """

    def __init__(self, app: ASGIApp, policy: HostPolicy) -> None:
        self.app = app
        self.policy = policy
        self._warned: set[str] = set()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket") or not self.policy.applies_to(
            scope.get("server")
        ):
            await self.app(scope, receive, send)
            return
        host = Headers(scope=scope).get("host")
        if self.policy.allows(host):
            await self.app(scope, receive, send)
            return
        self._warn(host or "")
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        await PlainTextResponse(INVALID_HOST_MESSAGE, status_code=400)(
            scope, receive, send
        )

    def _warn(self, host: str) -> None:
        # 制御文字はログに出さない(ログの行を偽装させない)。
        name = "".join(
            c if c.isprintable() else "?" for c in host_header_name(host)[:100]
        )
        if name in self._warned or len(self._warned) >= _MAX_WARNED_HOSTS:
            return
        self._warned.add(name)
        if self.policy.loopback_only:
            reason = (
                "この PC だけで待ち受けているため、DNS リバインディング対策として "
                "localhost と IP アドレス以外の名前を受け付けません"
            )
        else:
            reason = "MONITOR_ALLOWED_HOSTS にない名前です"
        logger.warning(
            "Host ヘッダ %r の要求を断りました(%s)。リバースプロキシなどでこの名前を"
            "使うなら .env に MONITOR_ALLOWED_HOSTS=%s を設定してください",
            name,
            reason,
            name,
        )
