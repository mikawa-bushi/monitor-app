"""procdiag プリセット統合テスト。

attach() が返す config の正確性と、in-memory SQLite に手動投入したデータに対する
ビュー・KPI API の動作を確認する。
コネクタ(T02)は並列実装中のため、データ投入は ingest エンドポイント経由または
直接 SQL で行い、コネクタを使わない。
"""

from __future__ import annotations

import json
import tempfile
import os

import pytest
from fastapi.testclient import TestClient

from monitor_app.integrations.procdiag import attach
from monitor_app.main import create_app
from monitor_app.settings.declarative import MonitorConfig
from monitor_app.settings.runtime import AppSettings


# ---------------------------------------------------------------------------
# ヘルパー
# ---------------------------------------------------------------------------

SAMPLE_TS = 1760000000.0  # epoch 秒


def _make_findings_json(findings: list) -> str:
    """一時 JSON ファイルを作成し、パスを返す。テスト側で削除すること。"""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as f:
        json.dump({"findings": findings, "quality": {}}, f)
        return f.name


SAMPLE_FINDINGS = [
    {
        "rule_id": "MEM-LEAK-01",
        "severity": "critical",
        "subject": "myapp.service",
        "summary": "RSS が単調増加しています(リーク疑い)",
        "evidence": {"slope_mb_h": 12.5},
        "detected_at": [1760000000.0, 1760086400.0],
        "confidence": "high",
        "related": [],
        "proposal": "再起動を検討してください",
    },
    {
        "rule_id": "CPU-HIGH-01",
        "severity": "warning",
        "subject": "worker.service",
        "summary": "CPU 使用率が高い状態が続いています",
        "evidence": {"avg_pct": 85.0},
        "detected_at": [1760000100.0, 1760086500.0],
        "confidence": "med",
        "related": [],
        "proposal": "スケールアウトを検討してください",
    },
    {
        "rule_id": "SWAP-LOW-01",
        "severity": "info",
        "subject": "system",
        "summary": "スワップ使用量が増加しています",
        "evidence": {"swap_mb": 200},
        "detected_at": [1760000200.0, 1760086600.0],
        "confidence": "low",
        "related": [],
        "proposal": "メモリ増設を検討してください",
    },
]


# ---------------------------------------------------------------------------
# config 構造テスト
# ---------------------------------------------------------------------------


class TestAttachStructure:
    """attach() が返す MonitorConfig の構造を確認する。"""

    def test_without_findings_returns_valid_config(self):
        """findings_json=None でも有効な config が返る。"""
        cfg = attach(MonitorConfig(), db_path="/tmp/procdiag.db")
        assert isinstance(cfg, MonitorConfig)

    def test_without_findings_tables(self):
        cfg = attach(MonitorConfig(), db_path="/tmp/procdiag.db")
        assert "procdiag_sys" in cfg.tables
        assert "procdiag_findings" not in cfg.tables

    def test_without_findings_sources(self):
        cfg = attach(MonitorConfig(), db_path="/tmp/procdiag.db")
        assert "procdiag_sys" in cfg.sources
        assert "procdiag_findings" not in cfg.sources

    def test_without_findings_views(self):
        cfg = attach(MonitorConfig(), db_path="/tmp/procdiag.db")
        assert "procdiag_cpu" in cfg.views
        assert "procdiag_memory" in cfg.views
        assert "procdiag_psi" in cfg.views
        assert "procdiag_findings" not in cfg.views

    def test_without_findings_kpis(self):
        cfg = attach(MonitorConfig(), db_path="/tmp/procdiag.db")
        assert "procdiag_cpu_avg" in cfg.kpis
        assert "procdiag_mem_used_avg" in cfg.kpis
        assert "procdiag_critical_count" not in cfg.kpis

    def test_without_findings_no_alerts(self):
        cfg = attach(MonitorConfig(), db_path="/tmp/procdiag.db")
        assert cfg.alerts == []

    def test_with_findings_tables(self):
        fp = _make_findings_json(SAMPLE_FINDINGS)
        try:
            cfg = attach(MonitorConfig(), db_path="/tmp/procdiag.db", findings_json=fp)
            assert "procdiag_sys" in cfg.tables
            assert "procdiag_findings" in cfg.tables
        finally:
            os.unlink(fp)

    def test_with_findings_sources(self):
        fp = _make_findings_json(SAMPLE_FINDINGS)
        try:
            cfg = attach(MonitorConfig(), db_path="/tmp/procdiag.db", findings_json=fp)
            assert "procdiag_sys" in cfg.sources
            assert "procdiag_findings" in cfg.sources
        finally:
            os.unlink(fp)

    def test_with_findings_views(self):
        fp = _make_findings_json(SAMPLE_FINDINGS)
        try:
            cfg = attach(MonitorConfig(), db_path="/tmp/procdiag.db", findings_json=fp)
            assert "procdiag_cpu" in cfg.views
            assert "procdiag_memory" in cfg.views
            assert "procdiag_psi" in cfg.views
            assert "procdiag_findings" in cfg.views
        finally:
            os.unlink(fp)

    def test_with_findings_kpis(self):
        fp = _make_findings_json(SAMPLE_FINDINGS)
        try:
            cfg = attach(MonitorConfig(), db_path="/tmp/procdiag.db", findings_json=fp)
            assert "procdiag_cpu_avg" in cfg.kpis
            assert "procdiag_mem_used_avg" in cfg.kpis
            assert "procdiag_critical_count" in cfg.kpis
        finally:
            os.unlink(fp)

    def test_with_findings_alerts(self):
        fp = _make_findings_json(SAMPLE_FINDINGS)
        try:
            cfg = attach(MonitorConfig(), db_path="/tmp/procdiag.db", findings_json=fp)
            assert len(cfg.alerts) == 1
            alert = cfg.alerts[0]
            assert alert.view == "procdiag_findings"
            assert alert.column == "sev_rank"
            assert alert.op == ">="
            assert alert.value == 2.0
            assert alert.level == "critical"
        finally:
            os.unlink(fp)

    def test_with_alerts_false_no_alerts(self):
        fp = _make_findings_json(SAMPLE_FINDINGS)
        try:
            cfg = attach(
                MonitorConfig(),
                db_path="/tmp/procdiag.db",
                findings_json=fp,
                with_alerts=False,
            )
            assert cfg.alerts == []
        finally:
            os.unlink(fp)

    def test_source_sys_watermark(self):
        """procdiag_sys ソースの watermark_column が ts_min であることを確認。"""
        cfg = attach(MonitorConfig(), db_path="/tmp/procdiag.db")
        src = cfg.sources["procdiag_sys"]
        assert src.watermark_column == "ts_min"
        assert src.kind == "sqlite"
        assert src.source_table == "sys_rollup_1m"

    def test_source_findings_mapping(self):
        """procdiag_findings ソースの mapping が detected_at のドット記法を含む。"""
        fp = _make_findings_json(SAMPLE_FINDINGS)
        try:
            cfg = attach(MonitorConfig(), db_path="/tmp/procdiag.db", findings_json=fp)
            src = cfg.sources["procdiag_findings"]
            assert src.kind == "json"
            assert src.record_path == "findings"
            assert src.mode == "replace"
            assert src.mapping.get("detected_at.0") == "start_ts"
            assert src.mapping.get("detected_at.1") == "end_ts"
        finally:
            os.unlink(fp)


class TestPrefixIsolation:
    """prefix 変更で複数の procdiag インスタンスを共存できることを確認。"""

    def test_different_prefixes_coexist(self):
        cfg = MonitorConfig()
        cfg = attach(cfg, db_path="/tmp/host1.db", prefix="host1")
        cfg = attach(cfg, db_path="/tmp/host2.db", prefix="host2")
        assert "host1_sys" in cfg.tables
        assert "host2_sys" in cfg.tables
        assert "host1_cpu" in cfg.views
        assert "host2_cpu" in cfg.views

    def test_duplicate_prefix_raises(self):
        """同じ prefix を 2 度 attach すると ValueError が発生する。"""
        cfg = attach(MonitorConfig(), db_path="/tmp/procdiag.db")
        with pytest.raises(ValueError, match="merge"):
            attach(cfg, db_path="/tmp/procdiag.db")


# ---------------------------------------------------------------------------
# 実データ検証
# ---------------------------------------------------------------------------


@pytest.fixture
def sys_config():
    """procdiag_sys のみの config(findings なし)と create_app。"""
    cfg = attach(MonitorConfig(), db_path="/tmp/procdiag.db")
    settings = AppSettings(database_url="sqlite:///:memory:")
    app = create_app(cfg, settings)
    client = TestClient(app)
    return cfg, app, client


@pytest.fixture
def full_config():
    """procdiag_sys + procdiag_findings の config と create_app。"""
    fp = _make_findings_json(SAMPLE_FINDINGS)
    cfg = attach(MonitorConfig(), db_path="/tmp/procdiag.db", findings_json=fp)
    os.unlink(fp)  # JSON ファイルはもう不要(attach 済み)
    settings = AppSettings(database_url="sqlite:///:memory:")
    app = create_app(cfg, settings)
    client = TestClient(app)
    return cfg, app, client


class TestSysView:
    """procdiag_sys テーブルにデータを投入し、ビュー API が返ることを確認する。"""

    def _insert_sys_row(self, client: TestClient, ts_offset: int = 0) -> None:
        """直近(now)の sys 行を ingest エンドポイント経由で投入する。"""
        import time

        ts = time.time() + ts_offset
        client.post(
            "/api/ingest/procdiag_sys",
            json={
                "ts_min": ts,
                "cpu_avg": 45.5,
                "cpu_max": 82.3,
                "cpu_p95": 78.1,
                "cpu_max_core_max": 95.0,
                "mem_used_avg": 2147483648,  # 2 GB in bytes
                "mem_used_max": 3221225472,  # 3 GB in bytes
                "psi_cpu_max": 12.5,
                "psi_mem_max": 5.3,
                "psi_io_max": 2.1,
                "swap_out_sum": 1024,
            },
        )

    def test_cpu_view_returns_data(self, sys_config):
        _, _app, client = sys_config
        self._insert_sys_row(client)
        res = client.get("/api/views/procdiag_cpu")
        assert res.status_code == 200
        data = res.json()["data"]
        assert len(data) >= 1
        row = data[0]
        assert "ts" in row
        assert "cpu_avg" in row
        assert "cpu_max" in row

    def test_memory_view_mb_conversion(self, sys_config):
        """メモリビューで MB 変換されていることを確認。"""
        _, _app, client = sys_config
        self._insert_sys_row(client)
        res = client.get("/api/views/procdiag_memory")
        assert res.status_code == 200
        data = res.json()["data"]
        assert len(data) >= 1
        row = data[0]
        # 2 GB = 2048 MB
        assert "mem_used_avg" in row
        assert float(row["mem_used_avg"]) == pytest.approx(2048.0, rel=0.01)

    def test_psi_view_columns(self, sys_config):
        _, _app, client = sys_config
        self._insert_sys_row(client)
        res = client.get("/api/views/procdiag_psi")
        assert res.status_code == 200
        data = res.json()["data"]
        assert len(data) >= 1
        row = data[0]
        assert "psi_cpu_max" in row
        assert "psi_mem_max" in row
        assert "psi_io_max" in row

    def test_kpi_cpu_avg_returns_value(self, sys_config):
        """cpu_avg KPI が数値を返すことを確認。"""
        _, _app, client = sys_config
        self._insert_sys_row(client)
        kpis = client.get("/api/kpis").json()["kpis"]
        cpu_kpi = next(k for k in kpis if k["key"] == "procdiag_cpu_avg")
        assert cpu_kpi["value"] == pytest.approx(45.5)

    def test_kpi_mem_used_avg_returns_mb(self, sys_config):
        """mem_used_avg KPI が MB で数値を返すことを確認。"""
        _, _app, client = sys_config
        self._insert_sys_row(client)
        kpis = client.get("/api/kpis").json()["kpis"]
        mem_kpi = next(k for k in kpis if k["key"] == "procdiag_mem_used_avg")
        assert mem_kpi["value"] == pytest.approx(2048.0, rel=0.01)


class TestFindingsView:
    """procdiag_findings テーブルにデータを投入し、ビュー・KPI・アラートを確認。"""

    def _insert_findings(self, client: TestClient) -> None:
        """サンプル所見を ingest エンドポイント経由で投入。"""
        rows = [
            {
                "rule_id": "MEM-LEAK-01",
                "severity": "critical",
                "subject": "myapp.service",
                "summary": "RSS が単調増加",
                "confidence": "high",
                "proposal": "再起動を検討",
                "start_ts": 1760000000.0,
                "end_ts": 1760086400.0,
            },
            {
                "rule_id": "CPU-HIGH-01",
                "severity": "warning",
                "subject": "worker.service",
                "summary": "CPU 高",
                "confidence": "med",
                "proposal": "スケールアウトを検討",
                "start_ts": 1760000100.0,
                "end_ts": 1760086500.0,
            },
            {
                "rule_id": "SWAP-LOW-01",
                "severity": "info",
                "subject": "system",
                "summary": "スワップ増加",
                "confidence": "low",
                "proposal": "メモリ増設を検討",
                "start_ts": 1760000200.0,
                "end_ts": 1760086600.0,
            },
        ]
        res = client.post("/api/ingest/procdiag_findings", json=rows)
        assert res.json()["inserted"] == 3

    def test_findings_view_has_sev_rank(self, full_config):
        """findings ビューに sev_rank 列が含まれることを確認。"""
        _, _app, client = full_config
        self._insert_findings(client)
        res = client.get("/api/views/procdiag_findings")
        assert res.status_code == 200
        data = res.json()["data"]
        assert len(data) >= 3
        for row in data:
            assert "sev_rank" in row

    def test_findings_sev_rank_values(self, full_config):
        """severity に応じた sev_rank の値を確認。"""
        _, _app, client = full_config
        self._insert_findings(client)
        res = client.get("/api/views/procdiag_findings")
        data = res.json()["data"]
        by_rule = {row["rule_id"]: row for row in data}
        assert int(by_rule["MEM-LEAK-01"]["sev_rank"]) == 2  # critical
        assert int(by_rule["CPU-HIGH-01"]["sev_rank"]) == 1  # warning
        assert int(by_rule["SWAP-LOW-01"]["sev_rank"]) == 0  # info

    def test_findings_sorted_by_sev_rank_desc(self, full_config):
        """findings ビューが sev_rank 降順になっていることを確認。"""
        _, _app, client = full_config
        self._insert_findings(client)
        res = client.get("/api/views/procdiag_findings")
        data = res.json()["data"]
        ranks = [int(row["sev_rank"]) for row in data]
        assert ranks == sorted(ranks, reverse=True)

    def test_kpi_critical_count(self, full_config):
        """critical 所見件数 KPI が正しい値を返すことを確認。"""
        _, _app, client = full_config
        self._insert_findings(client)
        kpis = client.get("/api/kpis").json()["kpis"]
        crit_kpi = next(k for k in kpis if k["key"] == "procdiag_critical_count")
        assert crit_kpi["value"] == pytest.approx(1.0)

    def test_alert_fires_for_critical(self, full_config):
        """critical 所見が存在するとアラートが発火することを確認。"""
        _, _app, client = full_config
        self._insert_findings(client)
        # ビュー取得でアラートエンジンを駆動する
        res = client.get("/api/views/procdiag_findings")
        assert res.status_code == 200
        alerts = res.json()["alerts"]
        active_levels = [a["level"] for a in alerts]
        assert "critical" in active_levels

    def test_no_critical_findings_no_alert(self):
        """critical 所見がない場合はアラートが発火しないことを確認。"""
        fp = _make_findings_json([])
        try:
            cfg = attach(MonitorConfig(), db_path="/tmp/procdiag.db", findings_json=fp)
        finally:
            os.unlink(fp)
        settings = AppSettings(database_url="sqlite:///:memory:")
        app = create_app(cfg, settings)
        client = TestClient(app)

        # info のみ投入
        client.post(
            "/api/ingest/procdiag_findings",
            json=[
                {
                    "rule_id": "SWAP-LOW-01",
                    "severity": "info",
                    "subject": "system",
                    "summary": "スワップ増加",
                    "confidence": "low",
                    "proposal": "メモリ増設を検討",
                    "start_ts": 1760000200.0,
                    "end_ts": 1760086600.0,
                }
            ],
        )
        res = client.get("/api/views/procdiag_findings")
        alerts = res.json()["alerts"]
        # critical アラートは発火しない
        assert not any(a["level"] == "critical" for a in alerts)
