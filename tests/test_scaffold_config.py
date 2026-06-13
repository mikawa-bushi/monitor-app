"""scaffold/config.py の回帰テストと integrations プリセットの動作確認。

テスト 1: scaffold/config.py を load_config() で読み込み、有効な MonitorConfig が得られること
          (scaffold に追記したコメントが動作を壊していないことの確認)

テスト 2: docs/integrations_v2_2.md に記載した attach() 使用例が実際に動くこと
          (procdiag / netdiag / network_checker の 3 プリセットを scaffold 由来 config に
           attach してバリデーションが通ること)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from monitor_app.settings.declarative import MonitorConfig, SourceDef, TableDef, ViewDef
from monitor_app.settings.loader import load_config

# scaffold/config.py の絶対パス
_SCAFFOLD_CONFIG = (
    Path(__file__).parent.parent / "monitor_app" / "scaffold" / "config.py"
)

# integrations プリセット — 遅延インポートして依存エラーを明確化
from monitor_app.integrations import procdiag, netdiag, network_checker


# ---------------------------------------------------------------------------
# テスト 1: scaffold/config.py のロード
# ---------------------------------------------------------------------------


def test_scaffold_config_loads():
    """scaffold/config.py が load_config() で正常に読み込める。"""
    config = load_config(_SCAFFOLD_CONFIG)
    assert isinstance(config, MonitorConfig)


def test_scaffold_config_has_expected_tables():
    """scaffold/config.py の tables が壊れていない。"""
    config = load_config(_SCAFFOLD_CONFIG)
    assert "users" in config.tables
    assert "products" in config.tables
    assert "orders" in config.tables


def test_scaffold_config_has_expected_views():
    """scaffold/config.py の views が壊れていない。"""
    config = load_config(_SCAFFOLD_CONFIG)
    assert "users_view" in config.views
    assert "products_view" in config.views
    assert "orders_summary" in config.views


def test_scaffold_config_has_expected_kpis():
    """scaffold/config.py の kpis が壊れていない。"""
    config = load_config(_SCAFFOLD_CONFIG)
    assert "order_count" in config.kpis
    assert "total_amount" in config.kpis


def test_scaffold_config_has_alerts():
    """scaffold/config.py の alerts が壊れていない。"""
    config = load_config(_SCAFFOLD_CONFIG)
    assert len(config.alerts) >= 1


# ---------------------------------------------------------------------------
# テスト 1-b: v2.3 追加フィールド(group / dashboard)の確認
# ---------------------------------------------------------------------------


def test_scaffold_products_view_has_group():
    """products_view に group='販売' が設定されている。"""
    config = load_config(_SCAFFOLD_CONFIG)
    assert config.views["products_view"].group == "販売"


def test_scaffold_dashboard_columns():
    """dashboard.columns が 2 に設定されている。"""
    config = load_config(_SCAFFOLD_CONFIG)
    assert config.dashboard.columns == 2


def test_scaffold_dashboard_views_chart_only():
    """dashboard.views に指定されたビューはすべて chart を持つ。"""
    config = load_config(_SCAFFOLD_CONFIG)
    for vname in config.dashboard.views:
        assert vname in config.views, f"dashboard.views に未定義のビュー: {vname}"
        assert (
            config.views[vname].chart is not None
        ), f"dashboard.views: '{vname}' は chart を持たない"


# ---------------------------------------------------------------------------
# テスト 2: integrations attach() の動作確認
# ---------------------------------------------------------------------------


def _base_config() -> MonitorConfig:
    """プリセット attach() のベースとなる最小 MonitorConfig を返す。"""
    return load_config(_SCAFFOLD_CONFIG)


def test_procdiag_attach_no_findings():
    """procdiag.attach() が findings_json なしで valid な MonitorConfig を返す。"""
    config = _base_config()
    config = procdiag.attach(config, db_path="/tmp/procdiag.db")

    # テーブルが追加されている
    assert "procdiag_sys" in config.tables

    # ビューが追加されている
    assert "procdiag_cpu" in config.views
    assert "procdiag_memory" in config.views
    assert "procdiag_psi" in config.views

    # KPI が追加されている
    assert "procdiag_cpu_avg" in config.kpis
    assert "procdiag_mem_used_avg" in config.kpis

    # findings 系は存在しない
    assert "procdiag_findings" not in config.tables
    assert "procdiag_critical_count" not in config.kpis

    # ソースが追加されている
    assert "procdiag_sys" in config.sources
    sdef = config.sources["procdiag_sys"]
    assert sdef.kind == "sqlite"
    assert sdef.watermark_column == "ts_min"

    # Pydantic の full バリデーションが通る
    MonitorConfig.model_validate(config.model_dump())


def test_procdiag_attach_with_findings(tmp_path):
    """procdiag.attach() が findings_json ありで findings 系定義を追加する。"""
    import json

    findings_file = tmp_path / "findings.json"
    findings_file.write_text(
        json.dumps({"findings": [], "quality": {}}), encoding="utf-8"
    )

    config = _base_config()
    config = procdiag.attach(
        config,
        db_path="/tmp/procdiag.db",
        findings_json=str(findings_file),
    )

    assert "procdiag_findings" in config.tables
    assert "procdiag_findings" in config.views
    assert "procdiag_critical_count" in config.kpis
    assert any(a.view == "procdiag_findings" for a in config.alerts)

    MonitorConfig.model_validate(config.model_dump())


def test_netdiag_attach_default():
    """netdiag.attach() がデフォルト引数で valid な MonitorConfig を返す。"""
    config = _base_config()
    config = netdiag.attach(config)

    assert "netdiag_alerts" in config.tables
    assert "netdiag_timeline" in config.views
    assert "netdiag_state" in config.views
    assert "netdiag_degraded_24h" in config.kpis
    assert "netdiag_avg_recovery_min" in config.kpis

    sdef = config.sources["netdiag_alerts"]
    assert sdef.kind == "jsonl"
    assert sdef.mode == "append"

    assert any(a.view == "netdiag_state" for a in config.alerts)

    MonitorConfig.model_validate(config.model_dump())


def test_netdiag_attach_custom_path():
    """netdiag.attach() がカスタムパスで動く。"""
    config = _base_config()
    config = netdiag.attach(config, alerts_jsonl="/var/log/netdiag/alerts.jsonl")
    assert config.sources["netdiag_alerts"].path == "/var/log/netdiag/alerts.jsonl"


def test_network_checker_attach_no_hosts():
    """network_checker.attach() が hosts=None で集約ビューのみ生成する。"""
    config = _base_config()
    config = network_checker.attach(config)

    assert "netcheck_ping" in config.tables
    assert "netcheck_summary" in config.views
    assert "netcheck_success_pct_24h" in config.kpis
    assert "netcheck_avg_ms_24h" in config.kpis

    sdef = config.sources["netcheck_ping"]
    assert sdef.kind == "sqlite"
    assert sdef.source_table == "ping_results"
    assert sdef.watermark_column == "id"

    assert any(a.view == "netcheck_summary" for a in config.alerts)

    MonitorConfig.model_validate(config.model_dump())


def test_network_checker_attach_with_hosts():
    """network_checker.attach() が hosts 指定でホスト別ビューを生成する。"""
    config = _base_config()
    config = network_checker.attach(
        config, hosts=["plc-01", "gateway"], loss_threshold_pct=10.0
    )

    # ホスト名の非識別子文字は _ に置換される
    assert "netcheck_rt_plc_01" in config.views
    assert "netcheck_rt_gateway" in config.views

    MonitorConfig.model_validate(config.model_dump())


def test_all_three_presets_combined():
    """3 つのプリセットを組み合わせても名前衝突なく valid な config になる。"""
    import json
    import tempfile

    config = _base_config()

    # procdiag
    config = procdiag.attach(config, db_path="/tmp/procdiag.db")

    # netdiag
    config = netdiag.attach(config)

    # network_checker
    config = network_checker.attach(config, hosts=["plc-01", "gateway"])

    # 全部合わせて Pydantic バリデーションが通る
    MonitorConfig.model_validate(config.model_dump())

    # それぞれのソースが存在する
    assert "procdiag_sys" in config.sources
    assert "netdiag_alerts" in config.sources
    assert "netcheck_ping" in config.sources
