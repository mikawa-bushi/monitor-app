"""宣言的設定モデル。

ユーザーは ``config.py`` で ``MonitorConfig`` のインスタンスを組み立てる。
dict を直接書いていた v1 と異なり、typo や矛盾は ``MonitorConfig`` の構築時
(= アプリ起動時)に ``ValidationError`` として検出される。
"""

from __future__ import annotations

import re
from typing import Dict, List, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: 識別子として許可するパターン(SQL に組み込むフィールドのみ)
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: 列の論理型。CREATE TABLE 時の SQL 型と、CSV 取り込み時の値変換に使う。
ColumnType = Literal["int", "float", "str", "bool", "date"]


class Threshold(BaseModel):
    """セルスタイルの閾値ルール。``value`` と比較して ``css_class`` を適用する。"""

    # v1 では ``{"value": x, "class": y}`` だった。``class`` は予約語のため
    # フィールド名は ``css_class`` とし、入出力では別名 ``class`` を使う。
    model_config = ConfigDict(populate_by_name=True)

    value: float
    css_class: str = Field(..., alias="class")


class CellStyle(BaseModel):
    """1 つの列に対する条件付きスタイル定義。"""

    model_config = ConfigDict(populate_by_name=True)

    greater_than: Threshold | None = None
    less_than: Threshold | None = None
    equal_to: Threshold | None = None
    width: str | None = None
    font_size: str | None = None
    align: str | None = None
    bold: bool = False


class FormDef(BaseModel):
    """作業者向け入力フォームの定義(フェーズ3)。テーブルに付与する。"""

    fields: List[str] | None = None  # 入力対象列。None なら主キー以外の全列
    labels: Dict[str, str] = Field(default_factory=dict)  # 列 → 表示名
    choices: Dict[str, List[str]] = Field(default_factory=dict)  # 列 → 選択肢
    submit_label: str = "登録"


class ChartDef(BaseModel):
    """ビューをグラフ表示するための定義(フェーズ2)。"""

    type: Literal["line", "bar"] = "line"
    x: str  # 横軸の列(時刻・連番)
    y: Union[str, List[str]]  # 縦軸の列(複数系列可)
    x_label: str | None = None  # 横軸タイトル(未指定なら列名 x を使う)
    y_label: str | None = None  # 縦軸タイトル・単位(例: "%", "MB", "応答時間 (ms)")
    ucl: float | None = None  # 管理上限線(SPC)
    lcl: float | None = None  # 管理下限線(SPC)
    target: float | None = None  # 目標線

    @property
    def y_columns(self) -> List[str]:
        return [self.y] if isinstance(self.y, str) else list(self.y)


class TableDef(BaseModel):
    """CRUD 対象テーブルの定義。

    ``columns`` は ``["id", "name"]``(命名規則で型推論)または
    ``{"id": "int", "name": "str"}``(明示)のどちらでも書ける。
    """

    columns: Union[List[str], Dict[str, ColumnType]]
    primary_key: str = "id"
    foreign_keys: Dict[str, str] = Field(default_factory=dict)
    form: FormDef | None = None  # 作業者入力フォーム(フェーズ3)

    @property
    def column_names(self) -> List[str]:
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
    def _validate_pk(self) -> "TableDef":
        if self.primary_key not in self.column_names:
            raise ValueError(
                f"primary_key '{self.primary_key}' は columns "
                f"{self.column_names} に存在しません"
            )
        return self


class ViewDef(BaseModel):
    """表示用ビュー。生 SQL の SELECT で JOIN・集計を自由に書ける。"""

    query: str
    title: str = ""
    description: str = ""
    labels: Dict[str, str] = Field(
        default_factory=dict
    )  # 列名 → 表示見出し(単位込み可)
    styles: Dict[str, CellStyle] = Field(default_factory=dict)
    chart: ChartDef | None = None  # 設定するとグラフ表示になる(フェーズ2)
    group: str = ""  # ナビ・一覧でのセクション名(空は「その他」扱い)

    @model_validator(mode="after")
    def _validate_query(self) -> "ViewDef":
        head = self.query.strip().lstrip("(").lower()
        if not (head.startswith("select") or head.startswith("with")):
            raise ValueError(
                "ViewDef.query は SELECT または WITH で始まる必要があります"
            )
        return self


class AlertRule(BaseModel):
    """閾値アラートのルール(フェーズ1)。ビューの 1 列を監視する。"""

    view: str
    column: str
    op: Literal[">", ">=", "<", "<=", "==", "!="]
    value: float
    level: Literal["info", "warning", "critical"] = "warning"
    message: str = ""  # 空ならルールから自動生成
    notify: List[str] = Field(default_factory=list)  # 通知チャネル名

    def describe(self) -> str:
        if self.message:
            return self.message
        return f"{self.view}.{self.column} {self.op} {self.value}"


class KpiCard(BaseModel):
    """KPI サマリーカード(フェーズ2)。スカラー 1 値を返す SELECT を持つ。"""

    title: str
    query: str
    unit: str = ""
    target: float | None = None
    higher_is_better: bool = True
    format: str = "{:.0f}"
    link_view: str | None = None  # クリックで遷移するビュー(views のキー)

    @model_validator(mode="after")
    def _validate_query(self) -> "KpiCard":
        head = self.query.strip().lstrip("(").lower()
        if not (head.startswith("select") or head.startswith("with")):
            raise ValueError(
                "KpiCard.query は SELECT または WITH で始まる必要があります"
            )
        return self


class KioskConfig(BaseModel):
    """Andon / 大型表示モードの設定(フェーズ1)。"""

    views: List[str] = Field(default_factory=list)  # 空なら全ビューを対象
    rotate_seconds: int = 15
    theme: Literal["dark", "light"] = "dark"


class SourceDef(BaseModel):
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
    mapping: Dict[str, str] = Field(default_factory=dict)  # ソースキー → 列名
    mode: Literal["append", "replace"] = "append"

    @model_validator(mode="after")
    def _validate_source_def(self) -> "SourceDef":
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
        if self.kind == "json":
            if self.watermark_column is not None:
                raise ValueError("kind='json' では watermark_column は使用できません")
        if self.watermark_column is not None and self.mode != "append":
            raise ValueError(
                "watermark_column を指定する場合は mode='append' のみ許可されます"
            )
        return self


class DashboardConfig(BaseModel):
    """トップページのミニチャートグリッド設定。"""

    views: List[str] = Field(default_factory=list)  # 空なら chart 付き全ビュー
    columns: int = Field(default=3, ge=1, le=4)  # 1〜4


class MonitorConfig(BaseModel):
    """アプリ全体の宣言的設定。"""

    app_title: str = "Monitor App"
    header_text: str = "📊 Monitor Dashboard"
    footer_text: str = "© 2026 Monitor App"
    favicon_path: str = "img/favicon.ico"
    refresh_interval_ms: int = 2000
    refresh_mode: Literal["sse", "polling"] = "sse"
    tables: Dict[str, TableDef] = Field(default_factory=dict)
    views: Dict[str, ViewDef] = Field(default_factory=dict)
    alerts: List[AlertRule] = Field(default_factory=list)  # フェーズ1
    kpis: Dict[str, KpiCard] = Field(default_factory=dict)  # フェーズ2
    kiosk: KioskConfig | None = None  # フェーズ1
    sources: Dict[str, SourceDef] = Field(default_factory=dict)  # 外部データソース
    dashboard: DashboardConfig = Field(default_factory=DashboardConfig)  # フェーズ2

    @model_validator(mode="after")
    def _validate_relations(self) -> "MonitorConfig":
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

    def kiosk_views(self) -> List[str]:
        """Kiosk で表示するビュー名。未指定なら全ビュー。"""
        if self.kiosk and self.kiosk.views:
            return self.kiosk.views
        return list(self.views.keys())
