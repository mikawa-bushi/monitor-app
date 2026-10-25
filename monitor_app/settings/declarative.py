"""宣言的設定モデル。

ユーザーは ``config.py`` で ``MonitorConfig`` のインスタンスを組み立てる。
dict を直接書いていた v1 と異なり、型の誤りや矛盾は ``MonitorConfig`` の構築時
(= アプリ起動時)に ``ValidationError`` として検出される。

未知のキー(``primay_key`` のような綴りの誤り)は 2.x では読み込みを止めず、
:class:`ConfigWarning` で警告する(近い正しいキーを提案する)。既存の config.py を
壊さないための段階導入で、エラーにするのは次のメジャーバージョンとする(#DX-07)。
"""

from __future__ import annotations

import difflib
import linecache
import os
import re
import sys
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Literal

import pydantic
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

#: 識別子として許可するパターン(SQL に組み込むフィールドのみ)
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: 列の論理型。CREATE TABLE 時の SQL 型と、CSV 取り込み時の値変換に使う。
#: ``datetime`` は日時(時刻つき)。時系列データの並べ替え・期間指定を正しく行える。
ColumnType = Literal["int", "float", "str", "bool", "date", "datetime"]


# ---------------------------------------------------------------------------
# 未知のキー(綴りの誤り)の警告
# ---------------------------------------------------------------------------
class ConfigWarning(UserWarning):
    """config.py の書き方への警告(未知のキー・綴りの誤りなど)。読み込みは止めない。

    ``monitor-app check`` は
    :func:`~monitor_app.settings.loader.load_config_with_warnings` で集めて表示する。
    警告の場所(``filename`` / ``lineno``)は、モデルを組み立てた利用者のコードの行。
    """

    def __init__(
        self,
        message: str,
        *,
        model: str = "",
        key: str = "",
        suggestion: str | None = None,
        filename: str | None = None,
        lineno: int | None = None,
    ) -> None:
        super().__init__(message)
        self.model = model
        self.key = key
        self.suggestion = suggestion
        self.filename = filename
        self.lineno = lineno


#: :func:`collect_config_warnings` の収集先(ブロックの外では None)。
_WARNING_SINK: ContextVar[list[ConfigWarning] | None] = ContextVar(
    "monitor_app_config_warnings", default=None
)
#: 警告を Python の警告としてその場で表示するか(collect_config_warnings で切り替える)。
#: ``warnings`` のフィルタ(プロセス全体の状態)には触れずに表示を止めるため。
_WARNING_DISPLAY: ContextVar[bool] = ContextVar(
    "monitor_app_config_warning_display", default=True
)
#: ``warnings`` の既定の動作(同じ場所・文言の警告は 1 回だけ表示)のための記録
#: (ファイルごと)。config.py の読み込みでの重複の抑止は loader が別に行う。
_WARNING_REGISTRIES: dict[str, dict] = {}
#: 呼び出し元を探すときに飛ばすファイル(pydantic の内部とこのモジュール)。
_PYDANTIC_DIR = os.path.normcase(os.path.dirname(os.path.abspath(pydantic.__file__)))
_THIS_FILE = os.path.normcase(os.path.abspath(__file__))
#: 未知のキーの書かれた行を、モデルを組み立てた行から何行先まで探すか。
_KEY_SEARCH_LINES = 300


@contextmanager
def collect_config_warnings(*, display: bool = True) -> Iterator[list[ConfigWarning]]:
    """ブロック内で出た :class:`ConfigWarning` をリストに集める。

    警告の表示(``warnings`` の仕組み)とは独立に集めるため、警告のフィルタ設定に
    左右されない。``display=False`` ならブロック内では Python の警告として表示しない
    (呼び出し側が表示する)。入れ子にした場合は、内側で集めた警告も外側へ伝え、
    外側で表示を止めていれば内側でも止める。
    """
    outer = _WARNING_SINK.get()
    found: list[ConfigWarning] = []
    token = _WARNING_SINK.set(found)
    display_token = _WARNING_DISPLAY.set(_WARNING_DISPLAY.get() and display)
    try:
        yield found
    finally:
        _WARNING_DISPLAY.reset(display_token)
        _WARNING_SINK.reset(token)
        if outer is not None:
            outer.extend(found)


def config_warnings_displayed() -> bool:
    """いま :class:`ConfigWarning` を Python の警告として表示する状態か。"""
    return _WARNING_DISPLAY.get()


def _caller_frame():
    """モデルを組み立てた利用者側のフレーム(pydantic とこのモジュールの外)を返す。"""
    frame = sys._getframe(1)
    while frame is not None:
        filename = os.path.normcase(os.path.abspath(frame.f_code.co_filename))
        if not (
            filename == _THIS_FILE
            or filename.startswith(_PYDANTIC_DIR)
            or frame.f_code.co_filename.startswith("<frozen")
        ):
            return frame
        frame = frame.f_back
    return None


def _key_lineno(
    filename: str, start: int, key: str, *, limit: int | None = _KEY_SEARCH_LINES
) -> int:
    """モデルを組み立てた行 ``start`` 以降で ``key`` を書いた行を探す(無ければ start)。

    ``Model(\\n  key=...,\\n)`` のように複数行で書いた呼び出しでも、誤ったキーの
    行を指せるようにする(``key=`` と ``"key":`` の両方の書き方を探す)。
    ``limit`` は探す行数(None ならファイルの終わりまで)。
    """
    # 同じプロセスで書き換えられたファイルの古い行を使わない
    linecache.checkcache(filename)
    escaped = re.escape(key)
    pattern = re.compile(rf"(?<!\w){escaped}\s*=(?!=)|[\"']{escaped}[\"']\s*:")
    stop = start + limit if limit is not None else sys.maxsize
    lineno = start
    while lineno < stop:
        line = linecache.getline(filename, lineno)
        if not line:  # ファイルの終わり
            break
        if pattern.search(line):
            return lineno
        lineno += 1
    return start


def _known_keys(cls: type[BaseModel]) -> list[str]:
    """モデルが受け付けるキー(別名と、許可されていればフィールド名)。"""
    config = cls.model_config
    by_name = bool(config.get("populate_by_name") or config.get("validate_by_name"))
    keys: list[str] = []
    for name, field in cls.model_fields.items():
        alias = (
            field.validation_alias if isinstance(field.validation_alias, str) else None
        )
        alias = alias or field.alias
        if alias:
            keys.append(alias)
            if by_name:
                keys.append(name)
        else:
            keys.append(name)
    return keys


def _suggest_key(key: str, candidates: list[str]) -> str | None:
    """``key`` に最も近い正しいキー(大文字小文字違いを優先し、次に綴りの近さ)。"""
    lowered = {c.lower(): c for c in candidates}
    if key.lower() in lowered:
        return lowered[key.lower()]
    matches = difflib.get_close_matches(key, candidates, n=1, cutoff=0.6)
    return matches[0] if matches else None


def _warn_unknown_key(model: str, key: str, candidates: list[str]) -> None:
    suggestion = _suggest_key(key, candidates)
    if suggestion:
        message = (
            f"{model} の未知のキー '{key}' は無視されます"
            f"('{suggestion}' の誤りではありませんか?)"
        )
    else:
        message = (
            f"{model} の未知のキー '{key}' は無視されます"
            f"(指定できるキー: {', '.join(candidates)})"
        )
    frame = _caller_frame()
    filename = frame.f_code.co_filename if frame is not None else None
    lineno = frame.f_lineno if frame is not None else None
    if filename is not None and lineno is not None:
        lineno = _key_lineno(filename, lineno, key)
    warning = ConfigWarning(
        message,
        model=model,
        key=key,
        suggestion=suggestion,
        filename=filename,
        lineno=lineno,
    )
    sink = _WARNING_SINK.get()
    if sink is not None:
        sink.append(warning)
    if _WARNING_DISPLAY.get():
        show_config_warning(warning)


def show_config_warning(warning: ConfigWarning) -> None:
    """警告を Python の警告として表示する(場所は利用者のコードの行)。

    表示するかどうかは ``warnings`` のフィルタに従う(``-W error`` なら例外になる)。
    同じファイル・行・文言の警告は 1 プロセスで 1 回だけ表示する。
    """
    filename = warning.filename or "<config>"
    warnings.warn_explicit(
        warning,
        ConfigWarning,
        filename,
        warning.lineno or 0,
        registry=_WARNING_REGISTRIES.setdefault(filename, {}),
    )


class _ConfigModel(BaseModel):
    """宣言モデルの共通基底。未知のキーを :class:`ConfigWarning` で警告する。

    ``extra="ignore"``(pydantic の既定)のまま、構築前の入力(dict)を調べるだけなので、
    受け付ける値・既定値・JSON スキーマは変わらない。別名(Threshold の ``class``)と
    ``populate_by_name`` を考慮する。``extra="allow"`` は使わない(誤ったキーが
    ``model_dump`` に紛れ込み、セルスタイルの JSON などへ漏れるため)。
    """

    @model_validator(mode="before")
    @classmethod
    def _warn_unknown_keys(cls, data: Any) -> Any:
        if isinstance(data, dict):
            candidates = _known_keys(cls)
            known = set(candidates)
            for key in data:
                if key not in known:
                    _warn_unknown_key(cls.__name__, str(key), candidates)
        return data


def _strip_trailing_semicolons(query: str) -> str:
    """クエリ末尾の ``;`` を取り除く(行数を絞る副問い合わせ ``(...) LIMIT`` を壊すため)。"""
    stripped = query.rstrip()
    if not stripped.endswith(";"):
        return query
    while stripped.endswith(";"):
        stripped = stripped[:-1].rstrip()
    return stripped


class Threshold(_ConfigModel):
    """セルスタイルの閾値ルール。``value`` と比較して ``css_class`` を適用する。"""

    # v1 では ``{"value": x, "class": y}`` だった。``class`` は予約語のため
    # フィールド名は ``css_class`` とし、入出力では別名 ``class`` を使う。
    model_config = ConfigDict(populate_by_name=True)

    value: float
    css_class: str = Field(..., alias="class")


class CellStyle(_ConfigModel):
    """1 つの列に対する条件付きスタイル定義。"""

    model_config = ConfigDict(populate_by_name=True)

    greater_than: Threshold | None = None
    less_than: Threshold | None = None
    equal_to: Threshold | None = None
    width: str | None = None
    font_size: str | None = None
    align: str | None = None
    bold: bool = False


class FormDef(_ConfigModel):
    """作業者向け入力フォームの定義(フェーズ3)。テーブルに付与する。"""

    fields: list[str] | None = None  # 入力対象列。None なら主キー以外の全列
    labels: dict[str, str] = Field(default_factory=dict)  # 列 → 表示名
    choices: dict[str, list[str]] = Field(default_factory=dict)  # 列 → 選択肢
    submit_label: str = "登録"


class ChartDef(_ConfigModel):
    """ビューをグラフ表示するための定義(フェーズ2)。"""

    type: Literal["line", "bar"] = "line"
    x: str  # 横軸の列(時刻・連番)
    y: str | list[str]  # 縦軸の列(複数系列可)
    x_label: str | None = None  # 横軸タイトル(未指定なら列名 x を使う)
    y_label: str | None = None  # 縦軸タイトル・単位(例: "%", "MB", "応答時間 (ms)")
    ucl: float | None = None  # 管理上限線(SPC)
    lcl: float | None = None  # 管理下限線(SPC)
    target: float | None = None  # 目標線

    @property
    def y_columns(self) -> list[str]:
        return [self.y] if isinstance(self.y, str) else list(self.y)


class Retention(_ConfigModel):
    """テーブルの保持期間。古い行をバックグラウンドで定期的に削除する(任意)。

    センサ・PLC から追記し続けるテーブルが無制限に大きくなり、表示や集計が
    徐々に遅くなるのを防ぐ。``keep_days`` と ``keep_rows`` は併用できる。
    """

    #: この日数より古い行を削除する。``column``(日付・日時の列)が必須。
    keep_days: float | None = Field(default=None, gt=0)
    #: 新しい順にこの行数だけ残す。並び順は ``column``(省略時は主キー)。
    keep_rows: int | None = Field(default=None, ge=1)
    #: 古さを判定する列。``keep_days`` では日付・日時(ISO 形式の文字列も可)の列。
    column: str | None = None

    @model_validator(mode="after")
    def _validate_retention(self) -> Retention:
        if self.keep_days is None and self.keep_rows is None:
            raise ValueError(
                "retention には keep_days か keep_rows のどちらかが必要です"
            )
        if self.keep_days is not None and self.column is None:
            raise ValueError("retention の keep_days には column(日時の列)が必要です")
        return self


class TableDef(_ConfigModel):
    """CRUD 対象テーブルの定義。

    ``columns`` は ``["id", "name"]``(命名規則で型推論)または
    ``{"id": "int", "name": "str"}``(明示)のどちらでも書ける。
    """

    columns: list[str] | dict[str, ColumnType]
    primary_key: str = "id"
    foreign_keys: dict[str, str] = Field(default_factory=dict)
    #: 追加インデックス(#V6)。各要素は索引対象の列名リスト(複合可)。
    #: 外部キー列には自動で索引が張られるため、ここでは時刻/ウォーターマーク
    #: 列など WHERE/ORDER BY で使う列を指定する。
    indexes: list[list[str]] = Field(default_factory=list)
    form: FormDef | None = None  # 作業者入力フォーム(フェーズ3)
    #: 保持期間(任意)。指定すると古い行を定期的に削除する。
    retention: Retention | None = None

    @property
    def column_names(self) -> list[str]:
        if isinstance(self.columns, dict):
            return list(self.columns.keys())
        return list(self.columns)

    def column_type(self, col: str) -> ColumnType:
        """列の論理型を解決する。明示指定 > 命名規則 > ``str`` の順。"""
        if isinstance(self.columns, dict):
            declared = self.columns.get(col)
            if declared:
                return declared
        if col == self.primary_key or col.endswith("_id"):
            return "int"
        if "price" in col or "amount" in col:
            return "float"
        return "str"

    @model_validator(mode="after")
    def _validate_pk(self) -> TableDef:
        if self.primary_key not in self.column_names:
            raise ValueError(
                f"primary_key '{self.primary_key}' は columns "
                f"{self.column_names} に存在しません"
            )
        names = set(self.column_names)
        for cols in self.indexes:
            for col in cols:
                if col not in names:
                    raise ValueError(
                        f"index 列 '{col}' は columns {self.column_names} に"
                        " 存在しません"
                    )
        if (
            self.retention is not None
            and self.retention.column is not None
            and self.retention.column not in names
        ):
            raise ValueError(
                f"retention の column '{self.retention.column}' は columns "
                f"{self.column_names} に存在しません"
            )
        return self


class ViewDef(_ConfigModel):
    """表示用ビュー。生 SQL の SELECT で JOIN・集計を自由に書ける。"""

    query: str
    title: str = ""
    description: str = ""
    labels: dict[str, str] = Field(
        default_factory=dict
    )  # 列名 → 表示見出し(単位込み可)
    styles: dict[str, CellStyle] = Field(default_factory=dict)
    chart: ChartDef | None = None  # 設定するとグラフ表示になる(フェーズ2)
    group: str = ""  # ナビ・一覧でのセクション名(空は「その他」扱い)
    #: 返す最大行数(任意)。クエリ結果の先頭からこの行数で打ち切る。最新 N 行を
    #: 出したい場合はクエリ側で ``ORDER BY ... DESC`` を指定する。
    max_rows: int | None = Field(default=None, ge=1)

    @field_validator("query")
    @classmethod
    def _strip_semicolons(cls, value: str) -> str:
        return _strip_trailing_semicolons(value)

    @model_validator(mode="after")
    def _validate_query(self) -> ViewDef:
        head = self.query.strip().lstrip("(").lower()
        if not (head.startswith("select") or head.startswith("with")):
            raise ValueError(
                "ViewDef.query は SELECT または WITH で始まる必要があります"
            )
        return self


class AlertRule(_ConfigModel):
    """閾値アラートのルール(フェーズ1)。ビューの 1 列を監視する。"""

    view: str
    column: str
    op: Literal[">", ">=", "<", "<=", "==", "!="]
    value: float
    level: Literal["info", "warning", "critical"] = "warning"
    message: str = ""  # 空ならルールから自動生成
    notify: list[str] = Field(default_factory=list)  # 通知チャネル名

    def describe(self) -> str:
        if self.message:
            return self.message
        return f"{self.view}.{self.column} {self.op} {self.value}"


class KpiCard(_ConfigModel):
    """KPI サマリーカード(フェーズ2)。スカラー 1 値を返す SELECT を持つ。"""

    title: str
    query: str
    unit: str = ""
    target: float | None = None
    higher_is_better: bool = True
    format: str = "{:.0f}"
    link_view: str | None = None  # クリックで遷移するビュー(views のキー)

    @field_validator("query")
    @classmethod
    def _strip_semicolons(cls, value: str) -> str:
        return _strip_trailing_semicolons(value)

    @model_validator(mode="after")
    def _validate_query(self) -> KpiCard:
        head = self.query.strip().lstrip("(").lower()
        if not (head.startswith("select") or head.startswith("with")):
            raise ValueError(
                "KpiCard.query は SELECT または WITH で始まる必要があります"
            )
        return self


class KioskConfig(_ConfigModel):
    """Andon / 大型表示モードの設定(フェーズ1)。"""

    views: list[str] = Field(default_factory=list)  # 空なら全ビューを対象
    rotate_seconds: int = 15
    theme: Literal["dark", "light"] = "dark"


class SourceDef(_ConfigModel):
    """外部データソース宣言。MonitorConfig.sources の値。"""

    kind: Literal["sqlite", "jsonl", "json"]
    path: str  # 対象ファイル。~ はコネクタ側で展開
    table: str  # 取り込み先テーブル(tables に必須)
    # --- kind="sqlite" ---
    source_table: str | None = None  # 取得元テーブル(query 省略時は必須)
    query: str | None = None  # カスタム SELECT。:wm 名前付きパラメータ可
    watermark_column: str | None = None  # 増分取り込みキー列
    # --- kind="json" ---
    record_path: str | None = None  # レコード配列までのドット区切りパス
    # --- 共通 ---
    mapping: dict[str, str] = Field(default_factory=dict)  # ソースキー → 列名
    mode: Literal["append", "replace"] = "append"

    @model_validator(mode="after")
    def _validate_source_def(self) -> SourceDef:
        if self.kind == "sqlite":
            if self.source_table is None and self.query is None:
                raise ValueError(
                    "kind='sqlite' では source_table か query のどちらか必須です"
                )
            if self.source_table is not None and not _IDENTIFIER_RE.match(
                self.source_table
            ):
                raise ValueError(
                    f"source_table '{self.source_table}' は識別子"
                    f"(^[A-Za-z_][A-Za-z0-9_]*$)のみ許可されます"
                )
            if self.watermark_column is not None and not _IDENTIFIER_RE.match(
                self.watermark_column
            ):
                raise ValueError(
                    f"watermark_column '{self.watermark_column}' は識別子"
                    f"(^[A-Za-z_][A-Za-z0-9_]*$)のみ許可されます"
                )
        if self.kind == "jsonl":
            if self.mode != "append":
                raise ValueError("kind='jsonl' では mode='append' のみ許可されます")
            if self.watermark_column is not None:
                raise ValueError("kind='jsonl' では watermark_column は使用できません")
        if self.kind == "json" and self.watermark_column is not None:
            raise ValueError("kind='json' では watermark_column は使用できません")
        if self.watermark_column is not None and self.mode != "append":
            raise ValueError(
                "watermark_column を指定する場合は mode='append' のみ許可されます"
            )
        return self


class DashboardConfig(_ConfigModel):
    """トップページのミニチャートグリッド設定。"""

    views: list[str] = Field(default_factory=list)  # 空なら chart 付き全ビュー
    columns: int = Field(default=3, ge=1, le=4)  # 1〜4


class MonitorConfig(_ConfigModel):
    """アプリ全体の宣言的設定。"""

    app_title: str = "Monitor App"
    header_text: str = "📊 Monitor Dashboard"
    footer_text: str = "© 2026 Monitor App"
    favicon_path: str = "img/favicon.ico"
    refresh_interval_ms: int = 2000
    refresh_mode: Literal["sse", "polling"] = "sse"
    tables: dict[str, TableDef] = Field(default_factory=dict)
    views: dict[str, ViewDef] = Field(default_factory=dict)
    alerts: list[AlertRule] = Field(default_factory=list)  # フェーズ1
    kpis: dict[str, KpiCard] = Field(default_factory=dict)  # フェーズ2
    kiosk: KioskConfig | None = None  # フェーズ1
    sources: dict[str, SourceDef] = Field(default_factory=dict)  # 外部データソース
    dashboard: DashboardConfig = Field(default_factory=DashboardConfig)  # フェーズ2

    @model_validator(mode="after")
    def _validate_relations(self) -> MonitorConfig:
        for tname, tdef in self.tables.items():
            for col, ref in tdef.foreign_keys.items():
                if col not in tdef.column_names:
                    raise ValueError(
                        f"tables['{tname}']: foreign_keys のキー '{col}' は "
                        f"columns に存在しません"
                    )
                ref_table = ref.split(".")[0]
                if ref_table not in self.tables:
                    raise ValueError(
                        f"tables['{tname}']: foreign_keys の参照先テーブル "
                        f"'{ref_table}' が tables に定義されていません"
                    )
        for i, rule in enumerate(self.alerts):
            if rule.view not in self.views:
                raise ValueError(
                    f"alerts[{i}]: 監視対象ビュー '{rule.view}' が views に存在しません"
                )
        if self.kiosk:
            for v in self.kiosk.views:
                if v not in self.views:
                    raise ValueError(
                        f"kiosk.views: ビュー '{v}' が views に存在しません"
                    )
        for sname, sdef in self.sources.items():
            if sdef.table not in self.tables:
                raise ValueError(
                    f"sources['{sname}']: 取り込み先テーブル '{sdef.table}' が"
                    f" tables に存在しません"
                )
        for vname in self.dashboard.views:
            if vname not in self.views:
                raise ValueError(
                    f"dashboard.views: ビュー '{vname}' が views に存在しません"
                )
            if self.views[vname].chart is None:
                raise ValueError(
                    f"dashboard.views: ビュー '{vname}' は chart を持ちません"
                )
        for kname, card in self.kpis.items():
            if card.link_view is not None and card.link_view not in self.views:
                raise ValueError(
                    f"kpis['{kname}']: link_view '{card.link_view}' が"
                    f" views に存在しません"
                )
        return self

    def kiosk_views(self) -> list[str]:
        """Kiosk で表示するビュー名。未指定なら全ビュー。"""
        if self.kiosk and self.kiosk.views:
            return self.kiosk.views
        return list(self.views.keys())
