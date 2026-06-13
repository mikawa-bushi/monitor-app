"""integrations/network_checker の結合テスト。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from monitor_app import MonitorConfig
from monitor_app.db.engine import Database
from monitor_app.db.registry import TableRegistry
from monitor_app.integrations.network_checker import attach
from monitor_app.main import create_app
from monitor_app.services.kpi_service import KpiService
from monitor_app.services.view_service import ViewService
from monitor_app.settings.runtime import AppSettings


# ---------------------------------------------------------------------------
# attach() が返す config の構造検証
# ---------------------------------------------------------------------------


def _base_config() -> MonitorConfig:
    return MonitorConfig()


def test_attach_returns_monitor_config():
    """attach() は MonitorConfig を返す。"""
    cfg = attach(_base_config())
    assert isinstance(cfg, MonitorConfig)


def test_attach_table_exists():
    """ping テーブルが config に追加される。"""
    cfg = attach(_base_config())
    assert "netcheck_ping" in cfg.tables


def test_attach_source_exists_with_watermark():
    """ソースが watermark_column='id' で追加される。"""
    cfg = attach(_base_config())
    assert "netcheck_ping" in cfg.sources
    sdef = cfg.sources["netcheck_ping"]
    assert sdef.watermark_column == "id"
    assert sdef.source_table == "ping_results"
    assert sdef.kind == "sqlite"


def test_attach_summary_view_exists():
    """サマリービューが追加される。"""
    cfg = attach(_base_config())
    assert "netcheck_summary" in cfg.views


def test_attach_no_host_views_when_hosts_none():
    """hosts=None のときホスト別ビューは存在しない。"""
    cfg = attach(_base_config(), hosts=None)
    rt_views = [k for k in cfg.views if "_rt_" in k]
    assert rt_views == []


def test_attach_host_views_created_for_hosts():
    """hosts 指定時にホスト別ビューが hosts の数だけ追加される。"""
    cfg = attach(_base_config(), hosts=["plc-01", "8.8.8.8"])
    assert "netcheck_rt_plc_01" in cfg.views
    assert "netcheck_rt_8_8_8_8" in cfg.views


def test_attach_sanitized_view_names():
    """hosts=["plc-01", "8.8.8.8"] でサニタイズ済みビュー名が 2 つある。"""
    cfg = attach(_base_config(), hosts=["plc-01", "8.8.8.8"])
    rt_views = [k for k in cfg.views if k.startswith("netcheck_rt_")]
    assert len(rt_views) == 2


def test_attach_kpis_added():
    """KPI が 2 件追加される。"""
    cfg = attach(_base_config())
    assert "netcheck_success_pct_24h" in cfg.kpis
    assert "netcheck_avg_ms_24h" in cfg.kpis


def test_attach_alert_added_when_with_alerts():
    """with_alerts=True でアラートが 1 件追加される。"""
    cfg = attach(_base_config(), with_alerts=True)
    assert len(cfg.alerts) == 1
    rule = cfg.alerts[0]
    assert rule.view == "netcheck_summary"
    assert rule.column == "loss_pct_1h"
    assert rule.op == ">"
    assert rule.level == "warning"


def test_attach_no_alert_when_with_alerts_false():
    """with_alerts=False でアラートなし。"""
    cfg = attach(_base_config(), with_alerts=False)
    assert cfg.alerts == []


def test_attach_custom_prefix():
    """prefix 指定が反映される。"""
    cfg = attach(_base_config(), prefix="net")
    assert "net_ping" in cfg.tables
    assert "net_summary" in cfg.views
    assert "net_success_pct_24h" in cfg.kpis


def test_attach_custom_loss_threshold():
    """loss_threshold_pct がアラートルールの value に反映される。"""
    cfg = attach(_base_config(), loss_threshold_pct=10.0)
    rule = cfg.alerts[0]
    assert rule.value == 10.0


def test_attach_host_view_has_chart():
    """ホスト別ビューは line チャートを持つ。"""
    cfg = attach(_base_config(), hosts=["srv-01"])
    view = cfg.views["netcheck_rt_srv_01"]
    assert view.chart is not None
    assert view.chart.type == "line"
    assert view.chart.x == "timestamp"
    assert view.chart.y == "response_time"
    # 軸タイトル(単位)が設定され、フロントの目盛りラベルに使われる
    assert view.chart.y_label == "応答時間 (ms)"
    assert view.chart.x_label == "時刻"
    assert '"y_label"' in view.chart.model_dump_json()
    # テーブルヘッダーの単位付きラベル(列名 → 表示名)
    assert view.labels["response_time"] == "応答時間 (ms)"
    assert view.labels["timestamp"] == "時刻"


def test_attach_summary_view_styles():
    """サマリービューに success_pct と loss_pct_1h のスタイルが設定される。"""
    cfg = attach(_base_config())
    styles = cfg.views["netcheck_summary"].styles
    assert "success_pct" in styles
    assert "loss_pct_1h" in styles


# ---------------------------------------------------------------------------
# 実データ検証
# ---------------------------------------------------------------------------


def _now_str() -> str:
    """現在時刻を SQLite の datetime() 互換形式で返す。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")


@pytest.fixture()
def netcheck_app():
    """attach() で生成した config を create_app に渡し、ping データを投入した state。"""
    cfg = attach(
        _base_config(),
        hosts=["plc-01", "8.8.8.8"],
        db_path="~/network_check_tool.db",
    )
    settings = AppSettings(database_url="sqlite:///:memory:")
    app = create_app(cfg, settings)

    db: Database = app.state.db
    # ping データを直接 INSERT(コネクタを経由しない)
    with db.connect() as conn:
        conn.execute(
            text(
                "INSERT INTO netcheck_ping "
                "(id, target_host, timestamp, response_time, packet_loss, error_message) "
                "VALUES (:id, :host, :ts, :rt, :pl, :em)"
            ),
            [
                # plc-01: 8 件中 2 件ロス → 75% 成功
                {
                    "id": 1,
                    "host": "plc-01",
                    "ts": _now_str(),
                    "rt": 10.0,
                    "pl": 0,
                    "em": None,
                },
                {
                    "id": 2,
                    "host": "plc-01",
                    "ts": _now_str(),
                    "rt": 12.0,
                    "pl": 0,
                    "em": None,
                },
                {
                    "id": 3,
                    "host": "plc-01",
                    "ts": _now_str(),
                    "rt": None,
                    "pl": 1,
                    "em": "timeout",
                },
                {
                    "id": 4,
                    "host": "plc-01",
                    "ts": _now_str(),
                    "rt": None,
                    "pl": 1,
                    "em": "timeout",
                },
                {
                    "id": 5,
                    "host": "plc-01",
                    "ts": _now_str(),
                    "rt": 11.0,
                    "pl": 0,
                    "em": None,
                },
                {
                    "id": 6,
                    "host": "plc-01",
                    "ts": _now_str(),
                    "rt": 13.0,
                    "pl": 0,
                    "em": None,
                },
                {
                    "id": 7,
                    "host": "plc-01",
                    "ts": _now_str(),
                    "rt": 9.5,
                    "pl": 0,
                    "em": None,
                },
                {
                    "id": 8,
                    "host": "plc-01",
                    "ts": _now_str(),
                    "rt": 14.0,
                    "pl": 0,
                    "em": None,
                },
                # 8.8.8.8: 全件成功
                {
                    "id": 9,
                    "host": "8.8.8.8",
                    "ts": _now_str(),
                    "rt": 5.0,
                    "pl": 0,
                    "em": None,
                },
                {
                    "id": 10,
                    "host": "8.8.8.8",
                    "ts": _now_str(),
                    "rt": 6.0,
                    "pl": 0,
                    "em": None,
                },
            ],
        )

    return app


def test_summary_view_returns_rows(netcheck_app):
    """summary ビューがホスト別の行を返す。"""
    view_svc = ViewService(netcheck_app.state.config, netcheck_app.state.db)
    result = view_svc.get_view("netcheck_summary")
    hosts = {row["target_host"] for row in result["data"]}
    assert "plc-01" in hosts
    assert "8.8.8.8" in hosts


def test_summary_success_pct_plc01(netcheck_app):
    """plc-01 の success_pct は 75.0。"""
    view_svc = ViewService(netcheck_app.state.config, netcheck_app.state.db)
    result = view_svc.get_view("netcheck_summary")
    plc_row = next(r for r in result["data"] if r["target_host"] == "plc-01")
    assert abs(plc_row["success_pct"] - 75.0) < 0.01


def test_summary_success_pct_google(netcheck_app):
    """8.8.8.8 の success_pct は 100.0。"""
    view_svc = ViewService(netcheck_app.state.config, netcheck_app.state.db)
    result = view_svc.get_view("netcheck_summary")
    g_row = next(r for r in result["data"] if r["target_host"] == "8.8.8.8")
    assert abs(g_row["success_pct"] - 100.0) < 0.01


def test_summary_loss_pct_1h(netcheck_app):
    """plc-01 の loss_pct_1h は 25.0(直近 1h 内のデータで計算)。"""
    view_svc = ViewService(netcheck_app.state.config, netcheck_app.state.db)
    result = view_svc.get_view("netcheck_summary")
    plc_row = next(r for r in result["data"] if r["target_host"] == "plc-01")
    # loss_pct_1h: packet_loss=1 のとき 100.0 の平均 → 2/8 = 25.0
    assert abs(plc_row["loss_pct_1h"] - 25.0) < 0.01


def test_host_view_plc01_returns_rows(netcheck_app):
    """plc-01 のホスト別ビューが plc-01 の行だけを返す。"""
    view_svc = ViewService(netcheck_app.state.config, netcheck_app.state.db)
    result = view_svc.get_view("netcheck_rt_plc_01")
    assert len(result["data"]) > 0
    for row in result["data"]:
        # ホスト別ビューは timestamp と response_time のみ
        assert "timestamp" in row
        assert "response_time" in row


def test_host_view_google_returns_rows(netcheck_app):
    """8.8.8.8 のホスト別ビューが行を返す。"""
    view_svc = ViewService(netcheck_app.state.config, netcheck_app.state.db)
    result = view_svc.get_view("netcheck_rt_8_8_8_8")
    assert len(result["data"]) == 2


def test_kpi_success_pct_returns_value(netcheck_app):
    """KPI 全体成功率が数値を返す。"""
    kpi_svc = KpiService(netcheck_app.state.config, netcheck_app.state.db)
    results = kpi_svc.evaluate_all()
    kpi = next(k for k in results if k["key"] == "netcheck_success_pct_24h")
    assert kpi["value"] is not None
    assert isinstance(kpi["value"], float)


def test_kpi_avg_ms_returns_value(netcheck_app):
    """KPI 全体平均応答 ms が数値を返す。"""
    kpi_svc = KpiService(netcheck_app.state.config, netcheck_app.state.db)
    results = kpi_svc.evaluate_all()
    kpi = next(k for k in results if k["key"] == "netcheck_avg_ms_24h")
    assert kpi["value"] is not None
    assert isinstance(kpi["value"], float)


# ---------------------------------------------------------------------------
# シングルクォートを含むホスト名での SQL インジェクション耐性テスト
# ---------------------------------------------------------------------------


def test_host_with_single_quote_does_not_break_view():
    """ホスト名に ' が含まれてもビューが実行できる。"""
    cfg = attach(
        _base_config(),
        hosts=["host'with'quote"],
    )
    settings = AppSettings(database_url="sqlite:///:memory:")
    app = create_app(cfg, settings)

    # ビューが実行できる(行なしでもエラーにならない)
    view_svc = ViewService(app.state.config, app.state.db)
    view_name = "netcheck_rt_host_with_quote"
    assert view_name in cfg.views
    result = view_svc.get_view(view_name)
    assert result["data"] == []
