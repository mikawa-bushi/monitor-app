"""キオスク(Andon / 大型表示)v2.3 拡充のテスト。

kiosk-config JSON・KPI ストリップ・チャート読み込み・進行バーの出し分けを検証する。
"""

import json
import re

from fastapi.testclient import TestClient

from monitor_app import MonitorConfig, TableDef, ViewDef
from monitor_app.main import create_app
from monitor_app.settings.declarative import ChartDef, KpiCard
from monitor_app.settings.runtime import AppSettings


def _client(config: MonitorConfig) -> TestClient:
    settings = AppSettings(database_url="sqlite:///:memory:")
    return TestClient(create_app(config, settings))


def _config(*, with_chart: bool, with_kpis: bool) -> MonitorConfig:
    views = {
        "plain": ViewDef(query="SELECT * FROM t", title="テーブルだけ"),
    }
    if with_chart:
        views["trend"] = ViewDef(
            query="SELECT id, v FROM t",
            title="トレンド",
            chart=ChartDef(x="id", y="v", y_label="値"),
        )
    kpis = (
        {"total": KpiCard(title="合計", query="SELECT SUM(v) FROM t", unit="件")}
        if with_kpis
        else {}
    )
    return MonitorConfig(
        tables={"t": TableDef(columns={"id": "int", "v": "float"}, primary_key="id")},
        views=views,
        kpis=kpis,
    )


def _kiosk_config(html: str) -> dict:
    m = re.search(r'id="kiosk-config"[^>]*>(.*?)</script>', html, re.S)
    assert m, "kiosk-config JSON がページに埋め込まれていない"
    return json.loads(m.group(1))


def test_kiosk_config_json_has_views_and_charts():
    client = _client(_config(with_chart=True, with_kpis=True))
    html = client.get("/kiosk").text
    cfg = _kiosk_config(html)
    by_name = {v["name"]: v for v in cfg["views"]}
    assert by_name["plain"]["chart"] is None
    assert by_name["trend"]["chart"]["y_label"] == "値"
    assert by_name["trend"]["title"] == "トレンド"


def test_kiosk_loads_chart_js_only_when_needed():
    with_chart = _client(_config(with_chart=True, with_kpis=False)).get("/kiosk").text
    without = _client(_config(with_chart=False, with_kpis=False)).get("/kiosk").text
    assert "chart.umd.min.js" in with_chart and "chart-view.js" in with_chart
    assert "chart.umd.min.js" not in without


def test_kiosk_kpi_strip_only_when_kpis_defined():
    with_kpis = _client(_config(with_chart=False, with_kpis=True)).get("/kiosk").text
    without = _client(_config(with_chart=False, with_kpis=False)).get("/kiosk").text
    assert 'id="kiosk-kpis"' in with_kpis
    assert 'id="kiosk-kpis"' not in without


def test_kiosk_progress_and_areas_present():
    html = _client(_config(with_chart=True, with_kpis=False)).get("/kiosk").text
    assert 'id="kiosk-progress-fill"' in html
    assert 'id="kiosk-charts"' in html
    assert 'id="kiosk-table-wrap"' in html


def test_kpi_link_view_in_api_and_validated():
    """link_view が KPI API に載り、存在しないビュー名は構築時に弾かれる。"""
    import pytest
    from pydantic import ValidationError

    config = _config(with_chart=False, with_kpis=True)
    linked = config.model_copy(deep=True)
    linked.kpis["total"].link_view = "plain"
    linked = MonitorConfig.model_validate(linked.model_dump())
    client = _client(linked)
    kpis = client.get("/api/kpis").json()["kpis"]
    assert kpis[0]["link_view"] == "plain"

    with pytest.raises(ValidationError):
        bad = config.model_dump()
        bad["kpis"]["total"]["link_view"] = "ghost"
        MonitorConfig.model_validate(bad)
