"""``migrate`` も PostgreSQL の遅れた連番を進める(D5-01 の続き)。

起動時・取り込み時と同じ :meth:`TableRegistry.repair_sequences` を呼び、進めた表を
表示する。``--dry-run`` は報告だけで変えない(実際の PostgreSQL での確認は
tests/server_db/test_migrate_sequence_repair_server.py)。
"""

from __future__ import annotations

import pytest
from contract import helpers
from typer.testing import CliRunner

from monitor_app.cli import app
from monitor_app.db.registry import TableRegistry


@pytest.fixture
def project(tmp_path, monkeypatch):
    proj = helpers.start_project(tmp_path)
    monkeypatch.chdir(proj)
    runner = CliRunner()
    result = runner.invoke(app, ["import-csv"])
    assert result.exit_code == 0, result.output
    return proj


def test_migrate_repairs_and_reports_lagging_sequences(project, monkeypatch):
    calls: list[str] = []

    def repair(self, db):
        calls.append(db.dialect)
        return {"users": (0, 5)}

    monkeypatch.setattr(TableRegistry, "repair_sequences", repair)
    result = CliRunner().invoke(app, ["migrate"])
    assert result.exit_code == 0, result.output
    assert calls == ["sqlite"]
    assert "テーブル 'users' の連番を表の最大値に合わせました(0 → 5)" in result.stdout


def test_migrate_dry_run_does_not_repair(project, monkeypatch):
    def repair(self, db):  # pragma: no cover - 呼ばれたら失敗
        raise AssertionError("--dry-run では連番を変えない")

    monkeypatch.setattr(TableRegistry, "repair_sequences", repair)
    result = CliRunner().invoke(app, ["migrate", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "連番" not in result.stdout
