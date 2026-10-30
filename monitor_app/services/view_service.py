"""ビュー(表示用ビュー)の実行。生 SQL を読み取り専用接続で実行する。

結果は :class:`ViewSnapshot`(行データ・その JSON・JSON のハッシュ)として
:class:`ViewCache`(任意)で共有し(#18)、SSE・ダッシュボード・アラート評価による
同一ビューの多重クエリを 1 回にまとめる。JSON への変換もスナップショットごとに
1 回だけ行い、GET・一括取得・SSE が同じバイト列を使う。``limit``/``offset`` で
行数を絞れ(#17)、``ViewDef.max_rows`` で返す行数に上限を設けられる。
"""

from __future__ import annotations

import functools
import gzip
import hashlib
import itertools
import json
import logging
import re
import threading
from collections.abc import Sequence
from typing import Any

from pydantic_core import PydanticSerializationError, to_json
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from ..db.engine import Database
from ..db.gate import fetch_all
from ..exceptions import QueryExecutionError, ViewNotFoundError
from ..settings.declarative import MonitorConfig, ViewDef
from .view_cache import ViewCache

logger = logging.getLogger("monitor_app.views")

#: 上限(``max_rows``)のないビューがこの行数を超えたら 1 度だけ警告する。
LARGE_VIEW_ROWS = 50_000

#: 時刻・乱数に依存する SQL(データが変わらなくても結果が変わる)を見分ける。
#: 誤って当てはまっても従来どおりの TTL で判定するだけなので、広めに拾う。
_TIME_RELATIVE_RE = re.compile(
    r"""
    \bnow\b                                            # now() / 'now'
    | '(?:today|tomorrow|yesterday)'                   # PostgreSQL の日付の特殊値
    | \bcurrent_(?:date|time|timestamp)\b
    | \blocaltime(?:stamp)?\b
    | \b(?:utc_(?:date|time|timestamp)|sysdate|curdate|curtime)\b
    | \b(?:clock|statement|transaction)_timestamp\b | \btimeofday\b
    | \b(?:random|randomblob|rand|uuid|uuid_generate_v\d|gen_random_uuid|newid)\s*\(
    | \b(?:date|time|datetime|julianday|unixepoch|unix_timestamp)\s*\(\s*\)
    | \bstrftime\s*\(\s*'[^']*'\s*\)                   # 時刻を省略した strftime('%s')
    | \bage\s*\(                                       # PostgreSQL の age(ts) は今日が基準
    """,
    re.IGNORECASE | re.VERBOSE,
)

#: スナップショットの通し番号(計算を始めた順)。アラートが古いデータで評価し直さない
#: ための順序付けに使う。
_snapshot_seq = itertools.count(1)
#: スナップショットの推定メモリ ÷ JSON のバイト数。行の dict と値のオブジェクトが
#: JSON の 8〜9 倍ほどを使う(計測値。JSON のバイト列自体を含む)。
MEMORY_PER_JSON_BYTE = 9
#: ページ(``limit`` 付き)の結果を保存する上限 = キャッシュの上限 ÷ この値。
#: クライアントが決める ``limit``/``offset`` の組み合わせで大きなページを次々に
#: 求められても、よく使う全データのスナップショットを追い出さないため。
PAGE_SHARE = 16


@functools.lru_cache(maxsize=1024)
def is_time_relative(sql: str) -> bool:
    """SQL が現在時刻や乱数に依存するか(キャッシュの判定を TTL に限るため)。"""
    return _TIME_RELATIVE_RE.search(sql) is not None


def dumps(value: Any) -> bytes:
    """API 応答と同じ規則で JSON のバイト列にする。

    FastAPI の ``response_model`` と同じ pydantic-core の変換を使うため、
    PostgreSQL / MySQL が返す ``Decimal``・日時・``timedelta``・``bytes``・UUID なども
    GET と同じ表記になる。NaN / Infinity は null、未知の型は ``str()`` で表す。
    """
    try:
        return to_json(value, inf_nan_mode="null", fallback=str)
    except PydanticSerializationError:
        # UTF-8 として読めない bytes(BLOB)など。表示用に base64 で表す。
        return to_json(value, inf_nan_mode="null", fallback=str, bytes_mode="base64")


class ViewSnapshot:
    """ビュー 1 回分の計算結果。キャッシュで全クライアントが共有する(変更しないこと)。

    ``payload`` は ``alerts`` を除く応答の辞書、``json`` はそれを 1 回だけ JSON に
    したバイト列、``digest`` は ``json`` の SHA-256(内容が変わったかの判定に使う)。
    """

    __slots__ = (
        "payload",
        "json",
        "digest",
        "seq",
        "_text",
        "_gzip",
        "_gzip_lock",
        "nbytes",
    )

    def __init__(self, payload: dict[str, Any], *, seq: int | None = None) -> None:
        self.payload = payload
        self.json = dumps(payload)
        self.digest = hashlib.sha256(self.json).hexdigest()
        self.seq = next(_snapshot_seq) if seq is None else seq
        self._text: tuple[bytes, str] | None = None
        self._gzip: tuple[bytes, bytes] | None = None
        self._gzip_lock = threading.Lock()
        #: 推定メモリ(バイト)。キャッシュの上限(``MONITOR_VIEW_CACHE_MAX_MB``)の計算に使う。
        #: gzip 済みの本文(:meth:`gzip_body`。JSON の 1/7 ほど)もこの見積もりの余裕に収まる。
        self.nbytes = len(self.json) * MEMORY_PER_JSON_BYTE

    @property
    def rows(self) -> list[dict[str, Any]]:
        return self.payload["data"]

    def body(self, alerts_json: bytes) -> bytes:
        """``alerts`` を末尾に足した応答の JSON(``{..., "alerts": [...]}``)。"""
        return b"".join((memoryview(self.json)[:-1], b',"alerts":', alerts_json, b"}"))

    def text(self, alerts_json: bytes) -> str:
        """:meth:`body` の文字列版(SSE 用)。同じアラートなら全クライアントで共有する。"""
        cached = self._text
        if cached is not None and cached[0] == alerts_json:
            return cached[1]
        value = self.body(alerts_json).decode("utf-8")
        self._text = (alerts_json, value)
        return value

    def gzip_body(self, alerts_json: bytes, compresslevel: int) -> bytes:
        """:meth:`body` を gzip にしたもの。同じアラートなら全クライアントで共有する(#D2-01)。

        圧縮ミドルウェアは要求のたびに本文を圧縮し直すため、キャッシュから返す大きな
        ビュー(10 万行・13 MB で約 80 ミリ秒)では時間の大半が圧縮になる。スナップショット
        ごとに 1 回だけ圧縮し、同時に来た要求は最初の 1 回の結果を待って使う。
        """
        cached = self._gzip
        if cached is not None and cached[0] == alerts_json:
            return cached[1]
        with self._gzip_lock:
            cached = self._gzip
            if cached is not None and cached[0] == alerts_json:
                return cached[1]
            value = gzip.compress(
                self.body(alerts_json), compresslevel=compresslevel, mtime=0
            )
            self._gzip = (alerts_json, value)
            return value


class ViewService:
    def __init__(
        self,
        config: MonitorConfig,
        db: Database,
        cache: ViewCache | None = None,
    ) -> None:
        self.config = config
        self.db = db
        self.cache = cache
        self._warned_large: set[str] = set()
        self._warn_lock = threading.Lock()

    # ------------------------------------------------------------------
    # クエリの組み立てと実行
    # ------------------------------------------------------------------
    @staticmethod
    def _bounds(
        max_rows: int | None, limit: int | None, offset: int
    ) -> tuple[int | None, int]:
        """実際に使う (LIMIT, OFFSET)。None は「行数を絞らない」。

        ``max_rows`` はクエリ結果の先頭 ``max_rows`` 行をビューとみなす上限で、
        ``limit``/``offset`` のページはその範囲の中で切り出す。``limit`` が無いときは
        従来どおり ``offset`` を使わない。
        """
        if limit is None:
            return max_rows, 0
        offset = max(offset, 0)
        if max_rows is not None:
            limit = max(min(limit, max_rows - offset), 0)
        return limit, offset

    def _fetch(
        self, sql: str, params: dict[str, Any]
    ) -> tuple[list[str], Sequence[Sequence[Any]]]:
        """読み取り専用接続で実行し、(列名, 行のタプル) を返す。"""
        with self.db.readonly() as conn:
            result = conn.execute(text(sql), params)
            columns = list(result.keys())
            # 実行はゲートの外、行の取り出しはゲートの中で 1000 行ずつ(GIL コンボイ対策)。
            rows = fetch_all(result, self.db)
        return columns, rows

    def _compute_snapshot(
        self, view_name: str, vdef: ViewDef, limit: int | None, offset: int
    ) -> ViewSnapshot:
        """クエリを実行してスナップショットを作る。キャッシュの計算関数。"""
        seq = next(_snapshot_seq)  # データを読む前に採番する(読んだ時点の順序)
        eff_limit, eff_offset = self._bounds(vdef.max_rows, limit, offset)
        sql = vdef.query
        params: dict[str, Any] = {}
        if eff_limit is not None:
            # 任意クエリを副問い合わせで包んで行数を絞る(#17)。
            sql = f"SELECT * FROM ({vdef.query}) AS _sub LIMIT :_limit OFFSET :_offset"
            params = {"_limit": eff_limit, "_offset": eff_offset}

        try:
            columns, raw = self._fetch(sql, params)
        except SQLAlchemyError as exc:
            raise QueryExecutionError(
                f"ビュー '{view_name}' のクエリ実行に失敗しました"
            ) from exc
        rows: list[dict[str, Any]] = [dict(zip(columns, r)) for r in raw]
        if limit is None and vdef.max_rows is None and len(rows) > LARGE_VIEW_ROWS:
            self._warn_large(view_name, len(rows))

        cell_styles = {
            col: style.model_dump(by_alias=True, exclude_none=True)
            for col, style in vdef.styles.items()
        }
        payload = {
            "view_name": view_name,
            "title": vdef.title or view_name,
            "description": vdef.description,
            "columns": columns,
            "column_labels": dict(vdef.labels),
            "data": rows,
            "cell_styles": cell_styles,
        }
        return ViewSnapshot(payload, seq=seq)

    def _warn_large(self, view_name: str, count: int) -> None:
        with self._warn_lock:
            if view_name in self._warned_large:
                return
            self._warned_large.add(view_name)
        logger.warning(
            "ビュー '%s' が %d 行を返しました。データが増えるほど表示・通信・アラート評価"
            "が遅くなります。ViewDef(max_rows=...) か、クエリの LIMIT・期間の条件で"
            "行数を絞ってください。",
            view_name,
            count,
        )

    # ------------------------------------------------------------------
    # 公開 API
    # ------------------------------------------------------------------
    def get_snapshot(
        self,
        view_name: str,
        limit: int | None = None,
        offset: int = 0,
        *,
        wait: bool = True,
    ) -> ViewSnapshot:
        """ビューのスナップショット(共有オブジェクト。変更しないこと)を返す。

        ``wait=False`` なら、ほかの呼び出しが同じビューを計算中のとき、待たずに
        :class:`~monitor_app.services.view_cache.ComputationPending` を送出する。
        """
        # 定義は 1 回だけ読む(画面で設定を変えて差し替えても、計算中の定義は変わらない)
        vdef = self.config.views.get(view_name)
        if vdef is None:
            raise ViewNotFoundError(f"ビュー '{view_name}' は定義されていません")
        if limit is None:
            offset = 0  # limit が無ければ offset は使わない(同じキャッシュを共有する)
        if self.cache is None:
            return self._compute_snapshot(view_name, vdef, limit, offset)
        cacheable = None
        max_bytes = getattr(self.cache, "max_bytes", None)
        if limit is not None and max_bytes is not None:
            page_limit = max_bytes // PAGE_SHARE

            def cacheable(snap: ViewSnapshot) -> bool:
                # 大きなページは保存しない(計算中に相乗りした呼び出しには渡す)。
                return snap.nbytes <= page_limit

        return self.cache.get_or_compute(
            (view_name, limit, offset),
            lambda: self._compute_snapshot(view_name, vdef, limit, offset),
            time_relative=is_time_relative(vdef.query),
            cacheable=cacheable,
            **({} if wait else {"wait": False}),
        )

    def get_view(
        self, view_name: str, limit: int | None = None, offset: int = 0
    ) -> dict[str, Any]:
        """ビューの payload を返す。呼び出し側が ``alerts`` を足せるよう浅いコピー
        を返す(キャッシュ済みオブジェクトを破壊しない)。"""
        return dict(self.get_snapshot(view_name, limit, offset).payload)

    def get_view_hashed(
        self, view_name: str, limit: int | None = None, offset: int = 0
    ) -> tuple[dict[str, Any], str]:
        """(payload のコピー, 内容のハッシュ) を返す。差分検出用。
        ハッシュはスナップショットの作成時に 1 度だけ計算される(#18)。"""
        snap = self.get_snapshot(view_name, limit, offset)
        return dict(snap.payload), snap.digest

    @staticmethod
    def data_hash(payload: dict[str, Any]) -> str:
        """ビューデータの行内容からハッシュを作る(後方互換用)。"""
        blob = json.dumps(payload["data"], sort_keys=True, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()
