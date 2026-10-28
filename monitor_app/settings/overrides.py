"""画面(設定ページ)で変えた設定を、config.py の設定に重ねる。

画面で編集できるのは、表示・ビュー・KPI・アラート・大型表示・ダッシュボードの項目
(:data:`EDITABLE_FIELDS`)だけ。テーブルの定義・外部データソース・favicon は
config.py でだけ変える(表の作り替えやサーバーのファイルの読み取りになるため)。

保存するのは config.py との差分だけで、形は JSON Merge Patch(RFC 7386)。画面で
変えていない項目は config.py の値がそのまま使われ、config.py を書き換えて再起動すれば
反映される。差分の ``null`` は「その項目を消す」を表す。編集できる項目のうち null を
許すもの(グラフ・管理限界線・行数の上限など)は既定値がどれも None なので、``null`` を
入れた項目と項目が無いことは同じ結果になる。リストは丸ごと置き換わる(アラートを
1 つ変えると、アラートの一覧全体が画面の値になる)。
"""

from __future__ import annotations

import hashlib
from typing import Any

from .declarative import MonitorConfig

#: 画面で編集できる MonitorConfig の項目(この順で画面に並べる)。
EDITABLE_FIELDS: tuple[str, ...] = (
    "app_title",
    "header_text",
    "footer_text",
    "refresh_interval_ms",
    "refresh_mode",
    "views",
    "kpis",
    "alerts",
    "kiosk",
    "dashboard",
)


def editable_settings(config: MonitorConfig) -> dict[str, Any]:
    """画面で編集できる項目を JSON にできる辞書で返す(閾値の色は ``class`` のキー)。"""
    return config.model_dump(mode="json", by_alias=True, include=set(EDITABLE_FIELDS))


def build_config(base: MonitorConfig, editable: dict[str, Any]) -> MonitorConfig:
    """``base`` の編集できない項目(テーブルなど)と ``editable`` から設定を組み立てる。

    ``editable`` の編集できない項目は無視する。検証に失敗すれば ``ValidationError``。
    """
    data: dict[str, Any] = {
        name: getattr(base, name)
        for name in MonitorConfig.model_fields
        if name not in EDITABLE_FIELDS
    }
    data.update({k: v for k, v in editable.items() if k in EDITABLE_FIELDS})
    return MonitorConfig.model_validate(data)


def merge_patch(target: Any, patch: Any) -> Any:
    """RFC 7386 の Merge Patch を当てた値を返す(``target`` は変えない)。"""
    if not isinstance(patch, dict):
        return patch
    result = dict(target) if isinstance(target, dict) else {}
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = merge_patch(result.get(key), value)
    return result


def _without_nulls(value: Any) -> Any:
    """辞書の中の ``null`` の項目を除く(リストの中には立ち入らない)。"""
    if not isinstance(value, dict):
        return value
    return {k: _without_nulls(v) for k, v in value.items() if v is not None}


def diff_patch(base: Any, desired: Any) -> Any:
    """``merge_patch(base, patch)`` が ``desired`` になる patch(変わった項目だけ)。

    値が ``null`` の項目と項目が無いことは区別しない(:func:`merge_patch` が
    ``null`` を項目の削除として扱うため)。新しく現れた辞書(グラフを足したときなど)
    からは ``null`` の項目を除いて、保存する差分を小さくする。
    """
    if not (isinstance(base, dict) and isinstance(desired, dict)):
        return _without_nulls(desired)
    patch: dict[str, Any] = {key: None for key in base if key not in desired}
    for key, value in desired.items():
        if key not in base:
            if value is not None:
                patch[key] = _without_nulls(value)
        elif base[key] != value:
            patch[key] = diff_patch(base[key], value)
    return patch


def apply_overrides(base: MonitorConfig, patch: dict[str, Any] | None) -> MonitorConfig:
    """config.py の設定 ``base`` に、画面で変えた差分 ``patch`` を重ねた設定。

    差分が無ければ ``base`` をそのまま返す。重ねた結果が検証に失敗すれば
    ``ValidationError``(config.py からビューを消した後に、そのビューへの差分が残って
    いるときなど)。
    """
    if not patch:
        return base
    return build_config(base, merge_patch(editable_settings(base), patch))


def config_revision(config: MonitorConfig) -> str:
    """画面に関わる設定の版(内容のハッシュ)。設定が変わったら開いている画面を読み直す。"""
    raw = config.model_dump_json(by_alias=True, include=set(EDITABLE_FIELDS))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
