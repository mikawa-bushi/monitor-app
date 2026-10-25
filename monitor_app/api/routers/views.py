"""ビュー表示エンドポイント。一括取得と SSE ストリーム。

ビューの JSON はデータが変わったときに 1 回だけ作り(:class:`ViewSnapshot`)、
GET・一括取得・SSE がそのバイト列に現在アクティブなアラートを付けて返す。
全データ(``limit`` なし)を返すときだけアラートを評価し(ページ取得の一部の行では
評価しない)、データが前回の評価から変わっていなければ評価し直さない。誰も
見ていないビューはバックグラウンドの AlertScheduler が評価する(#13)。

GET と一括取得は ``ETag`` を返し、``If-None-Match`` が一致すれば本文なしの 304 を
返す(データもアラートも変わっていないポーリングは転送量 0)。
"""

from __future__ import annotations

import csv
import hashlib
import inspect
import io
import re
import unicodedata
from collections.abc import Callable
from urllib.parse import quote

import anyio
from anyio.lowlevel import RunVar
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response, StreamingResponse
from sse_starlette.sse import EventSourceResponse

from ...exceptions import ViewNotFoundError
from ...security import neutralize_formula
from ...services.alert_service import AlertEngine
from ...services.view_cache import ComputationPending
from ...services.view_service import ViewService, ViewSnapshot, dumps
from ...settings.declarative import MonitorConfig
from ...web.serving import GZIP_COMPRESSLEVEL, GZIP_MINIMUM_SIZE, accepts_gzip
from ..deps import get_alert_engine, get_config, get_view_service, require_read_auth
from ..schemas import ViewDataResponse

router = APIRouter(prefix="/api/views", tags=["Views"])

#: ポーリングのたびに必ずサーバへ確認させる(304 で本文の再送を省く)。共有の
#: キャッシュ(プロキシ)には保存させない(閲覧を保護したデータを他の人へ渡さない — #D6-11)。
_CACHE_CONTROL = "private, no-cache"
#: CSV に付ける UTF-8 の BOM(Excel が UTF-8 と判別できるように)。
_BOM = "\ufeff"
#: ファイル名の ASCII 代替(filename=)に残す文字。
_UNSAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
#: SSE のフレームの計算(クエリ・JSON 化)に同時に使うスレッドの最大数。
#: AnyIO の既定のスレッド数(40)を SSE だけで使い切らないようにする: 遅いビューを
#: 大勢が購読していても、ヘルスチェックや通常の API が待たされない。
SSE_THREADS = 8
_sse_limiter: RunVar[anyio.CapacityLimiter] = RunVar("monitor_app_sse_limiter")
#: ほかの購読者が計算中のビューの完了を、スレッドを使わずに待つときの確認の間隔(秒)。
_FLIGHT_POLL = 0.05
#: その待ちの上限(秒)。超えたら通常どおり取得する(キャッシュの相乗りの待ちに任せる)。
_FLIGHT_WAIT = 30.0
#: サーバーの停止のとき、SSE の配信を自分で終える(最後のチャンクを送る)のを待つ秒数。
#: 打ち切られた応答は uvicorn が ERROR(ASGI callable returned without completing
#: response)として接続ごとに記録するため、停止の合図で配信を終えて正常に閉じる。
_SSE_SHUTDOWN_GRACE = 1.0
#: 停止の合図(``shutdown_event``)を使える sse-starlette か(3.3 以降)。
_SSE_HAS_SHUTDOWN_EVENT = (
    "shutdown_event" in inspect.signature(EventSourceResponse.__init__).parameters
)


# ---------------------------------------------------------------------------
# アラート・ETag の共通処理
# ---------------------------------------------------------------------------
def _evaluate_full(engine: AlertEngine, view_name: str, snap: ViewSnapshot) -> None:
    """全データのスナップショットでアラートを評価する(同じデータなら何もしない)。"""
    engine.evaluate_view_if_changed(view_name, snap.digest, snap.rows, version=snap.seq)


def _etag(*parts: bytes) -> str:
    """応答の内容(スナップショットのハッシュとアラート)から弱い ETag を作る。

    弱い ETag なので、圧縮ミドルウェアが本文を gzip にしても同じ値のまま使える。
    """
    return 'W/"' + hashlib.sha256(b"\x00".join(parts)).hexdigest()[:32] + '"'


def _etag_matches(header: str | None, etag: str) -> bool:
    """``If-None-Match`` が ``etag`` に一致するか(弱い比較。``*`` は常に一致)。"""
    if not header:
        return False
    opaque = etag.removeprefix("W/")
    for candidate in header.split(","):
        candidate = candidate.strip()
        if candidate == "*" or candidate.removeprefix("W/") == opaque:
            return True
    return False


def _json_or_304(
    request: Request,
    etag: str,
    body: Callable[[], bytes],
    gzip_body: Callable[[], bytes] | None = None,
) -> Response:
    headers = {"ETag": etag, "Cache-Control": _CACHE_CONTROL}
    if _etag_matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers=headers)
    if gzip_body is not None and _serves_gzip(request):
        headers.update({"Content-Encoding": "gzip", "Vary": "Accept-Encoding"})
        return Response(
            content=gzip_body(), media_type="application/json", headers=headers
        )
    return Response(content=body(), media_type="application/json", headers=headers)


def _serves_gzip(request: Request) -> bool:
    """圧縮済みの本文を返す要求か(圧縮ミドルウェアが有効で、gzip を受け付ける要求)。"""
    settings = getattr(request.app.state, "settings", None)
    return bool(settings is not None and settings.compress_responses) and accepts_gzip(
        request.scope
    )


# ---------------------------------------------------------------------------
# エンドポイント
# ---------------------------------------------------------------------------
@router.get("/batch")
def get_views_batch(
    request: Request,
    names: str = Query(..., description="カンマ区切りのビュー名"),
    service: ViewService = Depends(get_view_service),
    engine: AlertEngine = Depends(get_alert_engine),
    _: None = Depends(require_read_auth),
):
    """複数ビューを 1 リクエストでまとめて返す(ダッシュボード用 — #19)。

    存在しないビュー名は黙って無視する。共有キャッシュ(#18)を通すため、
    同一ビューを各カードが個別取得するより DB 負荷が下がる。
    """
    snaps: dict[str, ViewSnapshot] = {}
    for raw in names.split(","):
        name = raw.strip()
        if not name or name in snaps:
            continue
        try:
            snap = service.get_snapshot(name)
        except ViewNotFoundError:
            continue
        _evaluate_full(engine, name, snap)
        snaps[name] = snap
    alerts = dumps(engine.active_alerts())
    keys = [dumps(name) for name in snaps]
    etag = _etag(
        *(key + b"=" + snap.digest.encode() for key, snap in zip(keys, snaps.values())),
        alerts,
    )

    def body() -> bytes:
        parts = [
            key + b":" + snap.body(alerts) for key, snap in zip(keys, snaps.values())
        ]
        return b'{"views":{' + b",".join(parts) + b"}}"

    return _json_or_304(request, etag, body)


@router.get(
    "/{view_name}",
    response_model=ViewDataResponse,
    responses={304: {"description": "If-None-Match の ETag と一致(本文なし)"}},
)
def get_view(
    view_name: str,
    request: Request,
    limit: int | None = Query(None, ge=1, description="返す最大行数(#17)"),
    offset: int = Query(0, ge=0, description="先頭からのスキップ行数(#17)"),
    service: ViewService = Depends(get_view_service),
    engine: AlertEngine = Depends(get_alert_engine),
    _: None = Depends(require_read_auth),
):
    """ビューを一度だけ取得する(ポーリング用)。"""
    snap = service.get_snapshot(view_name, limit=limit, offset=offset)
    if limit is None:  # ページ(一部の行)ではアラートを評価しない
        _evaluate_full(engine, view_name, snap)
    alerts = dumps(engine.active_alerts())
    etag = _etag(snap.digest.encode(), alerts)
    # 大きなビューは圧縮した本文をスナップショットに残し、要求ごとに圧縮し直さない(#D2-01)。
    # 小さな本文はミドルウェアと同じく圧縮しない(圧縮しても速くならない)。
    gzip_body = (
        (lambda: snap.gzip_body(alerts, GZIP_COMPRESSLEVEL))
        if len(snap.json) >= GZIP_MINIMUM_SIZE
        else None
    )
    return _json_or_304(request, etag, lambda: snap.body(alerts), gzip_body)


def _ascii_filename(stem: str) -> str:
    """``filename=`` 用の ASCII だけのファイル名(拡張子なし)。"""
    ascii_stem = (
        unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode("ascii")
    )
    ascii_stem = _UNSAFE_FILENAME_RE.sub("_", ascii_stem).strip("._")
    return ascii_stem or "export"


def _attachment(stem: str, ext: str) -> str:
    """Content-Disposition。日本語などの名前は RFC 5987 の ``filename*`` で渡す。

    ``filename*`` を解釈しない古いクライアント向けに ASCII の ``filename`` も付ける。
    """
    filename = f"{stem}.{ext}"
    fallback = f"{_ascii_filename(stem)}.{ext}"
    if fallback == filename:
        return f'attachment; filename="{filename}"'
    return (
        f'attachment; filename="{fallback}"; '
        f"filename*=UTF-8''{quote(filename, safe='')}"
    )


@router.get("/{view_name}/export")
def export_view(
    view_name: str,
    format: str = "csv",
    bom: bool = Query(
        False,
        description="CSV の先頭に UTF-8 の BOM を付ける(Excel で開いても文字化けしない)",
    ),
    raw: bool = Query(
        False,
        description=(
            "値をそのまま書き出す。既定では = + - @ などで始まる文字列が表計算ソフトで"
            "数式として実行されないよう、CSV では先頭に ' を付け、xlsx では文字列として書く"
        ),
    ),
    service: ViewService = Depends(get_view_service),
    _: None = Depends(require_read_auth),
):
    """ビューを CSV または Excel(xlsx)でダウンロードする(フェーズ4・H)。

    xlsx は openpyxl が導入されている場合のみ。未導入なら CSV にフォールバックする。
    """
    payload = service.get_view(view_name)
    columns = payload["columns"]
    rows = payload["data"]

    if format == "xlsx":
        try:
            from openpyxl import Workbook
        except ImportError:
            format = "csv"  # openpyxl 未導入なら CSV へ
        else:
            wb = Workbook()
            ws = wb.active

            def append(values: list) -> None:
                ws.append(values)
                # openpyxl は = で始まる文字列を数式として保存する。行の書き込みや API・
                # CSV の取り込みで入った値が Excel で実行されないよう、文字列に戻す(#D6-10)。
                if not raw and any(
                    isinstance(v, str) and v.startswith("=") for v in values
                ):
                    for cell in ws[ws.max_row]:
                        if cell.data_type == "f":
                            cell.data_type = "s"

            append(list(columns))
            for row in rows:
                append([row.get(c) for c in columns])
            buf = io.BytesIO()
            wb.save(buf)
            return Response(
                content=buf.getvalue(),
                media_type=(
                    "application/vnd.openxmlformats-officedocument."
                    "spreadsheetml.sheet"
                ),
                headers={"Content-Disposition": _attachment(view_name, "xlsx")},
            )

    buf = io.StringIO()
    if bom:
        buf.write(_BOM)
    writer = csv.writer(buf)

    # = + - @ などで始まる文字列は先頭に ' を付け、Excel などで数式として実行させない
    # (CSV インジェクション対策。#D6-10)。raw=1 なら値をそのまま書く。
    def cell(value: object) -> object:
        return value if raw else neutralize_formula(value)

    writer.writerow([cell(c) for c in columns])
    for row in rows:
        writer.writerow([cell(row.get(c)) for c in columns])
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": _attachment(view_name, "csv")},
    )


# ---------------------------------------------------------------------------
# SSE
# ---------------------------------------------------------------------------
def _sse_thread_limiter() -> anyio.CapacityLimiter:
    """SSE のフレームの計算用のスレッドの枠(イベントループごとに 1 つ)。"""
    try:
        return _sse_limiter.get()
    except LookupError:
        limiter = anyio.CapacityLimiter(SSE_THREADS)
        _sse_limiter.set(limiter)
        return limiter


def _frame(
    snap: ViewSnapshot,
    engine: AlertEngine,
    view_name: str,
    last_key: tuple | None,
) -> tuple | None:
    """スナップショットから SSE 1 フレーム分を作る(:func:`_sse_frame` を参照)。"""
    _evaluate_full(engine, view_name, snap)
    alerts = dumps(engine.active_alerts())
    key = (snap.digest, alerts)
    if key == last_key:
        return None
    return key, snap.text(alerts)


def _sse_frame(
    service: ViewService,
    engine: AlertEngine,
    view_name: str,
    last_key: tuple | None,
) -> tuple | None:
    """SSE 1 フレーム分の (変化の判定キー, data 文字列)。前回から変わらなければ None。

    data 文字列はスナップショットごとに 1 回だけ作り、全クライアントで共有する。
    ほかの購読者が同じビューを計算中なら、スレッドで待たずに
    :class:`ComputationPending` を送出する(呼び出し側が非同期で待つ)。
    """
    if isinstance(service, ViewService):
        snap = service.get_snapshot(view_name, wait=False)
    else:  # 差し替え・テスト用の代役
        snap = service.get_snapshot(view_name)
    return _frame(snap, engine, view_name, last_key)


def _joined_frame(
    pending: ComputationPending,
    service: ViewService,
    engine: AlertEngine,
    view_name: str,
    last_key: tuple | None,
) -> tuple | None:
    """相乗りした計算の結果から SSE 1 フレーム分を作る。

    計算が中断された・待ちの上限を超えたときは、通常どおり取得する。
    """
    try:
        snap = pending.result()
    except LookupError:
        snap = service.get_snapshot(view_name)
    return _frame(snap, engine, view_name, last_key)


async def _next_frame(
    service: ViewService,
    engine: AlertEngine,
    view_name: str,
    last_key: tuple | None,
) -> tuple | None:
    """次の SSE フレームをスレッドで作る(SSE 用の枠 ``SSE_THREADS`` の中で)。

    ほかの購読者が同じビューを計算中なら、その終わりをスレッドを使わずに待ってから
    結果を受け取る。大勢が同じ遅いビューを購読していても、待つだけのスレッドで枠が
    埋まらず、計算が終わればすぐ全員に届く。
    """
    limiter = _sse_thread_limiter()
    try:
        return await anyio.to_thread.run_sync(
            _sse_frame, service, engine, view_name, last_key, limiter=limiter
        )
    except ComputationPending as pending:
        with anyio.move_on_after(_FLIGHT_WAIT):
            while not pending.done.is_set():
                await anyio.sleep(_FLIGHT_POLL)
        return await anyio.to_thread.run_sync(
            _joined_frame,
            pending,
            service,
            engine,
            view_name,
            last_key,
            limiter=limiter,
        )


async def _sse_event_stream(
    service: ViewService,
    engine: AlertEngine,
    view_name: str,
    interval: float,
    is_disconnected,
    stop: anyio.Event | None = None,
):
    """SSE イベントを生成する。データかアラートが変わったときだけ ``message`` を送る。

    ``is_disconnected`` は ``request.is_disconnected``(テストでは差し替え可能)。
    ブロッキングなクエリ/JSON 化はスレッドへ逃がす(:func:`_next_frame`)。
    ``stop`` が立ったら(サーバーの停止)、次の確認を待たずに配信を終える。
    """
    last_key: tuple | None = None
    while True:
        if (stop is not None and stop.is_set()) or await is_disconnected():
            break
        frame = await _next_frame(service, engine, view_name, last_key)
        if frame is not None:
            last_key, data = frame
            yield {"event": "message", "data": data}
        if stop is None:
            await anyio.sleep(interval)
        else:
            with anyio.move_on_after(interval):
                await stop.wait()


@router.get("/{view_name}/stream")
async def stream_view(
    view_name: str,
    request: Request,
    service: ViewService = Depends(get_view_service),
    engine: AlertEngine = Depends(get_alert_engine),
    config: MonitorConfig = Depends(get_config),
    _: None = Depends(require_read_auth),
):
    """ビューデータを Server-Sent Events で配信する。

    ``refresh_interval_ms`` ごとに確認し、データかアラートが変化したときだけ
    ``message`` イベントを送る。差分がなければ送信しない。共有キャッシュに
    より、複数クライアントが同じビューを購読してもクエリ・JSON 化は 1 回。
    """
    if view_name not in config.views:
        # 配信を始めてからでは 404 を返せない(再接続のたびにエラーになる)ため先に確かめる
        raise ViewNotFoundError(f"ビュー '{view_name}' は定義されていません")
    interval = max(config.refresh_interval_ms, 250) / 1000.0
    if not _SSE_HAS_SHUTDOWN_EVENT:  # pragma: no cover - sse-starlette 3.3 未満
        return EventSourceResponse(
            _sse_event_stream(
                service, engine, view_name, interval, request.is_disconnected
            )
        )
    stop = anyio.Event()
    return EventSourceResponse(
        _sse_event_stream(
            service, engine, view_name, interval, request.is_disconnected, stop
        ),
        shutdown_event=stop,
        shutdown_grace_period=_SSE_SHUTDOWN_GRACE,
    )
