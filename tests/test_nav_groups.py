"""ナビゲーション用グループ(pages._nav_groups)の chart 絞り込みのテスト。

``_nav_groups_filtered`` は後方互換の別名として残り、同じ結果を返す。
"""

from __future__ import annotations

import pytest

from monitor_app import ChartDef, MonitorConfig, ViewDef
from monitor_app.api.routers.pages import _nav_groups, _nav_groups_filtered


def _config() -> MonitorConfig:
    chart = ChartDef(x="id", y="v")
    return MonitorConfig(
        views={
            "trend": ViewDef(query="SELECT 1", group="ライン", chart=chart),
            "table_a": ViewDef(query="SELECT 1", group="ライン"),
            "loose_chart": ViewDef(query="SELECT 1", chart=chart),
            "loose": ViewDef(query="SELECT 1", title="単独"),
        }
    )


def test_chart_none_returns_all_views():
    assert _nav_groups(_config(), chart=None) == _nav_groups(_config())
    assert _nav_groups(_config()) == [
        {
            "group": "ライン",
            "views": [
                {"name": "trend", "title": "trend"},
                {"name": "table_a", "title": "table_a"},
            ],
        },
        {
            "group": "その他",
            "views": [
                {"name": "loose_chart", "title": "loose_chart"},
                {"name": "loose", "title": "単独"},
            ],
        },
    ]


def test_chart_false_keeps_only_views_without_chart():
    assert _nav_groups(_config(), chart=False) == [
        {"group": "ライン", "views": [{"name": "table_a", "title": "table_a"}]},
        {"group": "その他", "views": [{"name": "loose", "title": "単独"}]},
    ]


def test_chart_true_keeps_only_chart_views():
    assert _nav_groups(_config(), chart=True) == [
        {"group": "ライン", "views": [{"name": "trend", "title": "trend"}]},
        {"group": "その他", "views": [{"name": "loose_chart", "title": "loose_chart"}]},
    ]


def test_no_named_group_after_filtering_returns_single_unnamed_group():
    cfg = MonitorConfig(
        views={
            "a": ViewDef(query="SELECT 1", group="g", chart=ChartDef(x="x", y="y")),
            "b": ViewDef(query="SELECT 1"),
        }
    )
    assert _nav_groups(cfg, chart=False) == [
        {"group": "", "views": [{"name": "b", "title": "b"}]}
    ]


@pytest.mark.parametrize("chart", [None, True, False])
def test_filtered_alias_matches(chart):
    assert _nav_groups_filtered(_config(), chart=chart) == _nav_groups(
        _config(), chart=chart
    )
