"""Web サーバー(uvicorn)の起動(``runserver`` と :func:`serving_session`)。"""

from __future__ import annotations

import errno
import ipaddress
import logging
import os
import signal
import socket
import threading
import time
import webbrowser
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer

from .. import __version__, logging_conf
from ..security import is_loopback_host as is_loopback  # 点検・Host の確認と同じ判定
from ._common import (
    CONFIG_HELP,
    StopRequested,
    check_relative_paths,
    fail,
    is_address_in_use,
    load_project,
    plan_csv_import,
    same_dir,
    stop_signals,
)
from .data import run_csv_import

if TYPE_CHECKING:
    from fastapi import FastAPI

    from ..settings.declarative import MonitorConfig
    from ..settings.runtime import AppSettings

logger = logging.getLogger("monitor_app.cli")

_LOOPBACK_OF_WILDCARD = {"0.0.0.0": "127.0.0.1", "::": "::1", "": "127.0.0.1"}
#: すべてのインターフェースで待ち受けるアドレス。
_ALL_INTERFACES = tuple(_LOOPBACK_OF_WILDCARD)
#: ``--open`` でサーバーの待ち受け開始を待つ最大秒数。
OPEN_TIMEOUT = 15.0
#: API キーを作るコマンドの例(警告に添える)。
_KEYGEN_HINT = 'python -c "import secrets; print(secrets.token_urlsafe(24))"'


#: 待ち受けに使えるポート番号の範囲(0 は空いているポートを OS が選ぶ)。
PORT_RANGE = range(0, 65536)
#: 「権限がない」を表す errno(Windows の WSAEACCES = 10013 を含む。Hyper-V・WSL が
#: 予約したポートもこれになる)。
_ACCESS_ERRNOS = frozenset({errno.EACCES, errno.EPERM, 10013})
#: 「この PC のアドレスではない」を表す errno(Windows の WSAEADDRNOTAVAIL を含む)。
_NOT_LOCAL_ERRNOS = frozenset(
    {errno.EADDRNOTAVAIL, getattr(errno, "WSAEADDRNOTAVAIL", errno.EADDRNOTAVAIL)}
)
#: 待ち受けのホストの指定の案内。
_HOST_HINT = (
    "--host か MONITOR_HOST には 127.0.0.1(この PC だけ)か 0.0.0.0(ほかの PC からも)、"
    "またはこの PC の IP アドレスを指定してください"
)


def check_port(port: int) -> None:
    """ポート番号が範囲内(0〜65535)かを確かめ、範囲外なら案内して終了する。"""
    if port not in PORT_RANGE:
        fail(
            f"ポート番号 {port} は使えません(0〜65535 の範囲で指定してください)",
            "--port か MONITOR_PORT の値を確認してください",
        )


def ensure_port_available(host: str, port: int) -> None:
    """``host:port`` で待ち受けできるかを事前に確かめ、使用中なら案内して終了する。

    CSV の取り込みより前に確かめることで、二重起動したときに稼働中のサーバーの
    データを置き換えてしまうのを防ぐ。範囲外のポート番号も短く案内して終了する。
    権限のないポート(1024 未満・Windows の予約済みのポート)や、この PC で待ち受け
    できないホスト(名前を引けない・ほかの PC のアドレス)も、取り込みや起動の前に
    短く案内して終了する(終了コード 1)。それ以外の失敗は uvicorn 自身の報告に任せる。
    """
    check_port(port)
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    try:
        # create_server は名前を引けない失敗も errno だけの OSError にするため、先に引く
        socket.getaddrinfo(
            host or None, port, family, socket.SOCK_STREAM, 0, socket.AI_PASSIVE
        )
    except (socket.gaierror, UnicodeError) as exc:
        fail(f"ホスト {host} で待ち受けできません(名前を引けません: {exc})", _HOST_HINT)
    try:
        sock = socket.create_server((host, port), family=family)
    except OSError as exc:
        if is_address_in_use(exc):
            fail(
                f"ポート {port} は使用中です"
                "(既に起動中ではありませんか? --port で変更できます)"
            )
        if exc.errno in _ACCESS_ERRNOS or getattr(exc, "winerror", None) == 10013:
            fail(
                f"ポート {port} で待ち受けする権限がありません(1024 未満のポートは"
                "管理者の権限が必要です。Windows では Hyper-V などが予約したポートも"
                "使えません)",
                "--port 9990 など別のポートを指定してください(Windows で予約済みの"
                "ポートの確認: netsh int ipv4 show excludedportrange protocol=tcp)",
            )
        if exc.errno in _NOT_LOCAL_ERRNOS:
            fail(
                f"ホスト {host} で待ち受けできません(この PC のアドレスではありません)",
                _HOST_HINT,
            )
        return
    sock.close()


def _warn_if_multiple_workers() -> None:
    """WEB_CONCURRENCY>1 が設定されていても 1 プロセスで起動することを警告する。"""
    raw = os.environ.get("WEB_CONCURRENCY", "").strip()
    try:
        workers = int(raw)
    except ValueError:
        return
    if workers > 1:
        logger.warning(
            "WEB_CONCURRENCY=%s は無視して 1 プロセスで起動します"
            "(フォルダ監視・アラート評価はプロセスごとに動くため、複数プロセスでは"
            "取り込みや通知が重複します)",
            raw,
        )


# ---------------------------------------------------------------------------
# 起動時のログ(バージョン・URL・DB・公開範囲)
# ---------------------------------------------------------------------------
def describe_database_url(url: str) -> str:
    """接続 URL を、秘密を含まない 1 行の説明にする。

    SQLite はファイルの絶対パス(作業ディレクトリ基準)、それ以外はパスワード(と、
    クエリの password などの値)を ``***`` に伏せた URL。起動後に
    ``Database.describe()`` がログに出す説明と同じ(:func:`~monitor_app.db.engine.describe_url`)。
    """
    from ..db.engine import describe_url  # SQLAlchemy は使う時点で読み込む

    return describe_url(url)


def _url(host: str, port: int) -> str:
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"  # IPv6 リテラル
    return f"http://{host}:{port}/"


def _in_container() -> bool:
    return os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv")


def _primary_ip() -> str | None:
    """LAN 側の IP アドレス(既定の経路のもの)。分からなければ None。

    UDP ソケットの connect は経路を決めるだけでパケットを送らない。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.0.2.1", 9))  # TEST-NET-1(実在しない宛先)
            address = sock.getsockname()[0]
    except OSError:
        return None
    return None if ipaddress.ip_address(address).is_loopback else address


def lan_urls(host: str, port: int) -> list[str]:
    """ほかの PC から開くときの URL の候補(ホスト名・IP アドレス)。"""
    if host not in _ALL_INTERFACES:
        return [_url(host, port)]
    candidates = [socket.gethostname(), _primary_ip()]
    urls: list[str] = []
    for name in candidates:
        if name and _url(name, port) not in urls:
            urls.append(_url(name, port))
    return urls


def access_hint(host: str, port: int) -> str | None:
    """ほかの PC から開くときの案内(LAN の URL・コンテナの案内)。ループバックなら None。"""
    if is_loopback(host):
        return None
    if _in_container():
        return (
            "コンテナの外からは http://<ホストの IP アドレスまたは名前>:<公開したポート>/"
            " で開けます"
        )
    urls = lan_urls(host, port)
    if not urls:
        return None
    return (
        f"ほかの PC からは {' または '.join(urls)} で開けます(開けないときは"
        f"ファイアウォールでポート {port} の受信を許可してください)"
    )


def log_startup(settings: AppSettings, host: str, port: int) -> None:
    """起動の 1 行(バージョン・URL・DB)と、公開範囲に応じた案内・警告をログに出す。"""
    local = _url("127.0.0.1" if host in _ALL_INTERFACES else host, port)
    root = f"、公開パス {settings.root_path}" if settings.root_path else ""
    logger.info(
        "monitor-app %s を起動します: %s(待ち受け %s:%s%s、DB: %s)",
        __version__,
        local,
        host or "0.0.0.0",
        port,
        root,
        describe_database_url(settings.database_url),
    )
    if is_loopback(host):
        return
    if settings.api_key is None:
        logger.warning(
            "【注意】API キー(MONITOR_API_KEY)を設定せずに %s で公開しています。"
            "同じネットワークの誰でもデータの追加・変更・削除ができます。"
            ".env に MONITOR_API_KEY=<推測されにくい文字列> を設定してください(作り方: %s)",
            host or "0.0.0.0",
            _KEYGEN_HINT,
        )
    hint = access_hint(host, port)
    if hint:
        logger.info("%s", hint)


def log_project(
    config_path: Path, settings: AppSettings, cfg: MonitorConfig, *, importing: bool
) -> None:
    """設定ファイルと CSV のフォルダ(どの CSV をどのテーブルへ取り込むか)をログに出す。

    ``importing`` が True(起動前の取り込み・フォルダ監視)で、どのテーブルの CSV も
    無ければ、期待するファイル名を添えて警告する。
    """
    plan = plan_csv_import(settings, cfg)
    logger.info("設定: %s、CSV: %s", os.path.abspath(config_path), plan.describe())
    warning = plan.warning() if importing else None
    if warning:
        logger.warning("%s", warning)


def uvicorn_options(settings: AppSettings) -> dict[str, Any]:
    """実行時設定から決まる uvicorn.run の引数(ログ・アクセスログ・プロキシ)。"""
    options: dict[str, Any] = {
        "log_level": logging_conf.uvicorn_level(settings.log_level),
        "log_config": logging_conf.uvicorn_log_config(settings),
        "access_log": settings.access_log,
    }
    # 未設定なら渡さない(uvicorn の既定: 環境変数 FORWARDED_ALLOW_IPS、なければ 127.0.0.1)。
    if settings.forwarded_allow_ips:
        options["forwarded_allow_ips"] = settings.forwarded_allow_ips
    return options


def run_uvicorn(
    app: Any, settings: AppSettings, host: str, port: int, **options: Any
) -> None:
    """``app`` はアプリか import 文字列(``factory=True`` などは ``options`` で渡す)。
    ポートの事前確認と停止のシグナルの扱いは、取り込みより前に呼び出し側が行う。
    """
    # sse_starlette は import 時に uvicorn の終了処理へフックする。uvicorn が
    # シグナルハンドラを登録する前に読み込み、SSE 接続中でもすぐ停止できるようにする。
    import sse_starlette.sse  # noqa: F401
    import uvicorn

    uvicorn.run(
        app,
        host=host,
        port=port,
        # バックグラウンドタスク(フォルダ監視・アラート評価)はプロセスごとに動くため、
        # 常に 1 プロセスで起動する(WEB_CONCURRENCY による暗黙の複数ワーカー化を防ぐ)。
        workers=1,
        # SSE などの接続が残っていても、最大 5 秒で終了処理(lifespan)へ進む。
        timeout_graceful_shutdown=5,
        **uvicorn_options(settings),
        **options,
    )


def runserver(
    config: str = typer.Option("config.py", help=CONFIG_HELP),
    host: str | None = typer.Option(
        None, help="ホスト(既定: MONITOR_HOST または 127.0.0.1)"
    ),
    port: int | None = typer.Option(
        None, help="ポート(既定: MONITOR_PORT または 9990)"
    ),
    reload: bool = typer.Option(False, "--reload", help="コード変更時に自動リロード"),
    import_csv: bool = typer.Option(
        False,
        "--import-csv",
        help="起動前に CSV を取り込む(既存行を CSV の内容で置き換える)",
    ),
    import_csv_if_empty: bool = typer.Option(
        False,
        "--import-csv-if-empty",
        help=(
            "起動前に、行が 1 件も無いテーブルにだけ CSV を取り込む"
            "(初回だけ取り込み、画面やフォームで入力した行は再起動しても残す)"
        ),
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help=(
            "--import-csv で、config.py の列が CSV の見出しに無くても置き換える"
            "(無い列は空になる)"
        ),
    ),
) -> None:
    """Web サーバー(uvicorn)を起動する。"""
    try:
        # 準備(設定の確認・CSV の取り込み)の間の docker stop・systemctl stop・端末を
        # 閉じる操作で、準備を打ち切って終われるようにする。uvicorn にはここのハンドラを
        # 残さない(uvicorn は停止の処理のあと、受けたシグナルを元のハンドラへ送り直す)。
        with stop_signals():
            # 設定の誤りは、CSV の取り込みやサーバーの起動より前に短いメッセージで止める。
            # ログの形式(MONITOR_LOG_FORMAT)もここで反映する(取り込みや起動のログより前)。
            path, settings, cfg = load_project(config, check_paths=False)
            # config.py のフォルダにしか無い DB・(取り込むなら)CSV のフォルダを、作業
            # フォルダを基準に探して空の DB を作らないよう、取り込みの前に止める。
            check_relative_paths(
                path,
                settings,
                cfg,
                csv=import_csv or import_csv_if_empty or settings.ingest_watch,
            )
            host = host or settings.host
            port = port or settings.port
            check_port(port)  # --reload でも uvicorn のトレースバックにしない
            if not reload:
                # --reload では uvicorn の監視プロセスが起動前にポートを確保し、使用中なら
                # 1 行のエラーで止まるため事前確認を省く。
                ensure_port_available(host, port)
            log_startup(settings, host, port)
            log_project(
                path,
                settings,
                cfg,
                importing=import_csv or import_csv_if_empty or settings.ingest_watch,
            )
            if import_csv:
                run_csv_import(settings, cfg, force=force)
            elif import_csv_if_empty:
                run_csv_import(settings, cfg, only_empty=True)
            _warn_if_multiple_workers()
    except StopRequested:
        logger.info("停止の指示(SIGTERM)を受けたため、起動を中止しました")
        # 元のハンドラ(既定なら終了)で受け直す。PID 1(コンテナ)では無視されるため、
        # そのまま終了コード 0 で終わる(uvicorn の停止と同じ)。
        signal.raise_signal(signal.SIGTERM)
        raise typer.Exit(0) from None

    # app_factory が読む環境変数に設定パスを渡す(config.py のフォルダの .env もそこで読む)。
    os.environ["MONITOR_CONFIG_PATH"] = str(path)
    # 待ち受けるホストも渡す。--host 0.0.0.0 のとき、アプリ側の Host ヘッダの確認
    # (DNS リバインディング対策)が 127.0.0.1 で待ち受けていると誤らないように(#D6-03)。
    os.environ["MONITOR_HOST"] = host
    run_uvicorn(
        "monitor_app.main:app_factory",
        settings,
        host,
        port,
        factory=True,
        reload=reload,
        **_reload_dirs(path, reload),
    )


def _reload_dirs(config_path: Path, reload: bool) -> dict[str, Any]:
    """``--reload`` で監視するフォルダ(作業フォルダの外の config.py ならそのフォルダも)。

    uvicorn の既定は作業フォルダだけを監視するため、``--config <別のフォルダ>`` では
    config.py を保存しても再読み込みされない。
    """
    if not reload:
        return {}
    project_dir = Path(os.path.abspath(config_path)).parent
    cwd = Path.cwd()
    if same_dir(project_dir, cwd):
        return {}
    return {"reload_dirs": [str(cwd), str(project_dir)]}


def _browser_url(host: str, port: int) -> str:
    return _url(_LOOPBACK_OF_WILDCARD.get(host, host), port)


def _open_when_ready(url: str, host: str, port: int, timeout: float = OPEN_TIMEOUT):
    """サーバーが待ち受けを始めたらブラウザで開く(別スレッドで呼ぶ)。"""
    target = _LOOPBACK_OF_WILDCARD.get(host, host)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((target, port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.2)
    webbrowser.open(url)


class _ServingSession(ExitStack):
    def serve(
        self,
        app: FastAPI,
        settings: AppSettings,
        host: str,
        port: int,
        *,
        open_browser: bool,
    ) -> None:
        if not is_loopback(host) and not settings.api_key:
            typer.secho(
                f"警告: {host} で公開していますが API キーが未設定です。同じ"
                "ネットワークの誰でもデータを変更・削除できます"
                "(MONITOR_API_KEY を設定してください)",
                fg=typer.colors.RED,
                err=True,
            )
        hint = access_hint(host, port)
        if hint:
            typer.echo(f"  {hint}")
        url = _browser_url(host, port)
        typer.secho(f"✅ 起動します: {url} (Ctrl+C で終了)", fg=typer.colors.GREEN)
        # uvicorn は停止の処理のあと、受けたシグナルを送り直す(SIGINT は KeyboardInterrupt、
        # SIGTERM はこのブロックでは StopRequested。既定の動作のままだと後片付けの前に
        # プロセスが終わる)。どちらも正常な終了として扱う。
        with suppress(KeyboardInterrupt, StopRequested):
            if open_browser:
                threading.Thread(
                    target=_open_when_ready, args=(url, host, port), daemon=True
                ).start()
            run_uvicorn(app, settings, host, port)


@contextmanager
def serving_session() -> Iterator[_ServingSession]:
    try:
        with _ServingSession() as session, stop_signals():
            yield session
    except StopRequested:
        raise typer.Exit(143) from None
