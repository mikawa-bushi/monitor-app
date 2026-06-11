"""宣言的設定モデル。

ユーザーは ``config.py`` で ``MonitorConfig`` のインスタンスを組み立てる。
dict を直接書いていた v1 と異なり、typo や矛盾は ``MonitorConfig`` の構築時
(= アプリ起動時)に ``ValidationError`` として検出される。
"""

from __future__ import annotations

from typing import Dict, List, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

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
    styles: Dict[str, CellStyle] = Field(default_factory=dict)
    chart: ChartDef | None = None  # 設定するとグラフ表示になる(フェーズ2)

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
        return self

    def kiosk_views(self) -> List[str]:
        """Kiosk で表示するビュー名。未指定なら全ビュー。"""
        if self.kiosk and self.kiosk.views:
            return self.kiosk.views
        return list(self.views.keys())
