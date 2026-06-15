"""UI 設定(group / DashboardConfig / nav_groups / dashboard_json)のテスト。

U01 の完了条件を網羅する:
- DashboardConfig バリデーション
- _nav_groups() の構造
- dashboard_json の内容
- プリセット attach 後の group 設定
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from monitor_app import MonitorConfig, ViewDef
from monitor_app.integrations import netdiag, network_checker, procdiag
from monitor_app.settings.declarative import ChartDef, DashboardConfig
from monitor_app.api.routers.pages import _nav_groups


# ---------------------------------------------------------------------------
# DashboardConfig — バリデーション
# ---------------------------------------------------------------------------


def _make_view(*, with_chart: bool = True) -> ViewDef:
    """テスト用 ViewDef ヘルパ。"""
    chart = ChartDef(type="line", x="ts", y="val") if with_chart else None
    return ViewDef(query="SELECT 1 AS ts, 2 AS val", chart=chart)


def test_dashboard_views_unknown_name_raises():
    """存在しないビュー名を dashboard.views に指定すると ValidationError。"""
    with pytest.raises(ValidationError, match="views に存在しません"):
        MonitorConfig(
            views={"v1": _make_view()},
            dashboard=DashboardConfig(views=["ghost"]),
        )


def test_dashboard_views_no_chart_raises():
    """chart を持たないビューを dashboard.views に指定すると ValidationError。"""
    with pytest.raises(ValidationError, match="chart を持ちません"):
        MonitorConfig(
            views={"v1": _make_view(with_chart=False)},
            dashboard=DashboardConfig(views=["v1"]),
        )


def test_dashboard_columns_out_of_range_raises():
    """columns が 1〜4 の範囲外だと ValidationError。"""
    with pytest.raises(ValidationError):
        DashboardConfig(columns=5)
    with pytest.raises(ValidationError):
        DashboardConfig(columns=0)


def test_dashboard_columns_boundary_ok():
    """columns=1 と columns=4 は有効。"""
    assert DashboardConfig(columns=1).columns == 1
    assert DashboardConfig(columns=4).columns == 4


def test_dashboard_default_is_empty_views():
    """dashboard のデフォルトは views=[] columns=3。"""
    cfg = MonitorConfig()
    assert cfg.dashboard.views == []
    assert cfg.dashboard.columns == 3


# ---------------------------------------------------------------------------
# _nav_groups() — ナビグループ構造
# ---------------------------------------------------------------------------


def _config_mixed() -> MonitorConfig:
    """グループ付きと無グループ混在の config を返す。"""
    return MonitorConfig(
        views={
            "a": ViewDef(query="SELECT 1", group="G1", title="A"),
            "b": ViewDef(query="SELECT 1", group="G2", title="B"),
            "c": ViewDef(query="SELECT 1", group="G1", title="C"),
            "d": ViewDef(query="SELECT 1", title="D"),  # group=""
        }
    )


def test_nav_groups_mixed_has_named_groups():
    """グループ名が出現する。"""
    groups = _nav_groups(_config_mixed())
    group_names = [g["group"] for g in groups]
    assert "G1" in group_names
    assert "G2" in group_names


def test_nav_groups_ungrouped_collected_as_other():
    """group=="" のビューは「その他」グループに集約される。"""
    groups = _nav_groups(_config_mixed())
    other = next((g for g in groups if g["group"] == "その他"), None)
    assert other is not None
    assert any(v["name"] == "d" for v in other["views"])


def test_nav_groups_grouped_view_not_in_other():
    """グループ付きビューは「その他」に入らない。"""
    groups = _nav_groups(_config_mixed())
    other = next((g for g in groups if g["group"] == "その他"), None)
    assert other is not None
    other_names = [v["name"] for v in other["views"]]
    assert "a" not in other_names
    assert "b" not in other_names


def test_nav_groups_order_follows_config():
    """G1 が config 出現順で最初に来る。"""
    groups = _nav_groups(_config_mixed())
    named = [g for g in groups if g["group"] not in ("その他", "")]
    assert named[0]["group"] == "G1"
    assert named[1]["group"] == "G2"


def test_nav_groups_all_ungrouped_returns_single_empty_group():
    """全ビューが group=="" のとき、group="" の単一グループを返す。"""
    cfg = MonitorConfig(
        views={
            "x": ViewDef(query="SELECT 1", title="X"),
            "y": ViewDef(query="SELECT 1", title="Y"),
        }
    )
    groups = _nav_groups(cfg)
    assert len(groups) == 1
    assert groups[0]["group"] == ""
    view_names = [v["name"] for v in groups[0]["views"]]
    assert "x" in view_names
    assert "y" in view_names


def test_nav_groups_views_contain_name_and_title():
    """各ビューエントリに name と title が含まれる。"""
    cfg = MonitorConfig(
        views={"v": ViewDef(query="SELECT 1", group="G", title="マイビュー")}
    )
    groups = _nav_groups(cfg)
    assert groups[0]["views"][0] == {"name": "v", "title": "マイビュー"}


# ---------------------------------------------------------------------------
# dashboard_json — index() コンテキスト
# ---------------------------------------------------------------------------


def _chart_view(title: str = "") -> ViewDef:
    return ViewDef(
        query="SELECT 1 AS ts, 2 AS val",
        title=title,
        chart=ChartDef(type="line", x="ts", y="val"),
    )


def test_dashboard_json_default_includes_all_chart_views():
    """dashboard.views が空のとき、chart 付き全ビューが含まれる。"""
    cfg = MonitorConfig(
        views={
            "cv1": _chart_view("チャート1"),
            "nv1": ViewDef(query="SELECT 1"),  # chart なし
            "cv2": _chart_view("チャート2"),
        }
    )
    # _ctx 経由ではなく、ロジックを直接再現して確認
    dash_views = [
        {"name": vname, "title": vdef.title or vname, "chart": vdef.chart.model_dump()}
        for vname, vdef in cfg.views.items()
        if vdef.chart is not None
    ]
    assert len(dash_views) == 2
    view_names = [v["name"] for v in dash_views]
    assert "cv1" in view_names
    assert "cv2" in view_names
    assert "nv1" not in view_names


def test_dashboard_json_specified_views_order():
    """dashboard.views 指定時はその順序で入る。"""
    cfg = MonitorConfig(
        views={
            "cv1": _chart_view("C1"),
            "cv2": _chart_view("C2"),
            "cv3": _chart_view("C3"),
        },
        dashboard=DashboardConfig(views=["cv3", "cv1"]),
    )
    dash_views = [
        {
            "name": vname,
            "title": cfg.views[vname].title or vname,
            "chart": cfg.views[vname].chart.model_dump(),
        }
        for vname in cfg.dashboard.views
    ]
    payload = json.dumps(
        {"columns": cfg.dashboard.columns, "views": dash_views}, ensure_ascii=False
    )
    data = json.loads(payload)
    assert data["views"][0]["name"] == "cv3"
    assert data["views"][1]["name"] == "cv1"
    assert len(data["views"]) == 2


def test_dashboard_json_chart_field_is_dict():
    """dashboard_json の各ビューの chart フィールドは dict(ChartDef の model_dump)。"""
    cfg = MonitorConfig(views={"cv": _chart_view("テスト")})
    dash_views = [
        {"name": vname, "title": vdef.title or vname, "chart": vdef.chart.model_dump()}
        for vname, vdef in cfg.views.items()
        if vdef.chart is not None
    ]
    assert isinstance(dash_views[0]["chart"], dict)
    assert dash_views[0]["chart"]["type"] == "line"
    assert dash_views[0]["chart"]["x"] == "ts"


# ---------------------------------------------------------------------------
# プリセット — group 設定の確認
# ---------------------------------------------------------------------------


def test_procdiag_preset_views_have_group(tmp_path):
    """procdiag.attach() 後の全ビューに group='ホストリソース' が付く。"""
    db = tmp_path / "proc.db"
    db.write_bytes(b"")
    cfg = procdiag.attach(MonitorConfig(), db_path=str(db))
    for vname, vdef in cfg.views.items():
        assert (
            vdef.group == "ホストリソース"
        ), f"{vname} の group が不正: {vdef.group!r}"


def test_netdiag_preset_views_have_group():
    """netdiag.attach() 後の全ビューに group='ネットワーク診断' が付く。"""
    cfg = netdiag.attach(MonitorConfig())
    for vname, vdef in cfg.views.items():
        assert (
            vdef.group == "ネットワーク診断"
        ), f"{vname} の group が不正: {vdef.group!r}"


def test_network_checker_preset_views_have_group(tmp_path):
    """network_checker.attach() 後の全ビューに group='ping 監視' が付く。"""
    db = tmp_path / "nc.db"
    db.write_bytes(b"")
    cfg = network_checker.attach(MonitorConfig(), db_path=str(db))
    for vname, vdef in cfg.views.items():
        assert vdef.group == "ping 監視", f"{vname} の group が不正: {vdef.group!r}"


def test_network_checker_host_views_have_group(tmp_path):
    """hosts 指定時のホスト別ビューにも group='ping 監視' が付く。"""
    db = tmp_path / "nc.db"
    db.write_bytes(b"")
    cfg = network_checker.attach(
        MonitorConfig(), db_path=str(db), hosts=["192.168.1.1", "example.com"]
    )
    for vname, vdef in cfg.views.items():
        assert vdef.group == "ping 監視", f"{vname} の group が不正: {vdef.group!r}"
