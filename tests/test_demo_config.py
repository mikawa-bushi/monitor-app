"""倉庫デモの config.py(``monitor_app/demo/warehouse/config.py``)。

利用者が手本として写す設定なので、読み込みの警告が無いこと・画面の参照先が揃っている
こと・通知を送らないことを確かめる(画面に出る行は tests/test_cli_demo.py)。
"""

from __future__ import annotations

from monitor_app.demo import SOURCE
from monitor_app.settings.loader import load_config_with_warnings


def test_config_loads_without_warnings():
    config, warnings = load_config_with_warnings(SOURCE / "config.py")
    assert warnings == []
    assert set(config.tables) == {"items", "locations", "lots", "movements"}


def test_dashboard_and_kiosk_show_existing_charted_views():
    config, _ = load_config_with_warnings(SOURCE / "config.py")
    assert config.dashboard is not None and config.kiosk is not None
    for name in config.dashboard.views:
        assert config.views[name].chart is not None, name
    assert set(config.kiosk.views) <= set(config.views)
    for kpi in config.kpis.values():
        assert kpi.link_view in config.views


def test_alerts_send_no_notifications():
    config, _ = load_config_with_warnings(SOURCE / "config.py")
    assert {rule.level for rule in config.alerts} == {"warning", "critical"}
    assert all(rule.notify == [] for rule in config.alerts)
