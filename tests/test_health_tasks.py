"""``/api/health`` のバックグラウンドタスクの表示(フォルダ監視・アラート評価・保持期間・
定期バックアップ)を、すべてを有効にしたアプリで確かめる。

- 名前・稼働中か・最後に成功した時刻(``last_tick``)・直近の失敗(``last_error``)。
- 起動直後のバックアップ(``setup`` で行う)も、次の確認を待たずに記録される。
- 1 ファイル・1 ビューの失敗を個別に処理するタスクでも、失敗が ``last_error`` に出る。
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from monitor_app import AlertRule, MonitorConfig, Retention, TableDef, ViewDef
from monitor_app.main import create_app
from monitor_app.settings.runtime import AppSettings

TASKS = {"ingest watcher", "alert scheduler", "retention", "backup"}


def _config(view_query: str = "SELECT * FROM readings") -> MonitorConfig:
    return MonitorConfig(
        tables={
            "readings": TableDef(
                columns={"id": "int", "ts": "datetime", "temp": "float"},
                retention=Retention(keep_rows=1000),
            )
        },
        views={"readings_view": ViewDef(query=view_query)},
        alerts=[AlertRule(view="readings_view", column="temp", op=">", value=80)],
        refresh_interval_ms=1000,
    )


def _settings(tmp_path, **overrides) -> AppSettings:
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir(exist_ok=True)
    values = dict(
        database_url=f"sqlite:///{(tmp_path / 'app.db').as_posix()}",
        csv_dir=csv_dir,
        ingest_watch=True,
        ingest_interval=0.5,
        backup_dir=tmp_path / "backups",
        backup_interval_hours=24,
        retention_interval_minutes=0.01,
    )
    values.update(overrides)
    return AppSettings(_env_file=None, **values)


def _wait(client: TestClient, pred, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        body = client.get("/api/health").json()
        if pred(body["tasks"]) or time.monotonic() > deadline:
            return body
        time.sleep(0.1)


def test_health_lists_every_background_task_with_its_results(tmp_path):
    app = create_app(_config(), _settings(tmp_path))
    with TestClient(app) as client:
        body = _wait(
            client,
            lambda tasks: all(
                tasks.get(name, {}).get("last_tick")
                for name in ("alert scheduler", "retention", "backup")
            ),
        )
        assert body["status"] == "ok" and body["database"] == "ok"
        tasks = body["tasks"]
        assert set(tasks) == TASKS
        for name, status in tasks.items():
            assert status["running"] is True, name
            assert status["last_error"] is None, (name, status)
        # 起動直後のバックアップは setup で取り、すぐに記録される
        assert tasks["backup"]["last_tick"] is not None
        assert list((tmp_path / "backups").glob("app-*.db"))
        assert tasks["alert scheduler"]["last_tick"] is not None
        assert tasks["retention"]["last_tick"] is not None
    # 停止後はアプリのタスクが動いていない
    assert not any(task.running for task in app.state.services.background_tasks)


def test_failures_handled_per_view_are_shown_as_the_last_error(tmp_path):
    """アラート評価はビューごとの失敗でほかを止めないが、失敗は /api/health に出す。"""
    app = create_app(_config("SELECT * FROM no_such_table"), _settings(tmp_path))
    with TestClient(app) as client:
        body = _wait(client, lambda tasks: tasks["alert scheduler"]["last_error"])
        status = body["tasks"]["alert scheduler"]
        assert status["running"] is True
        assert status["last_error"] and status["last_tick"] is None
        assert "no_such_table" not in str(body)  # 内部の詳細は出さない


def test_failures_handled_per_csv_are_shown_as_the_last_error(tmp_path):
    settings = _settings(tmp_path, csv_encoding="ascii")
    app = create_app(_config(), settings)
    with TestClient(app) as client:
        _wait(client, lambda tasks: tasks["ingest watcher"]["running"])
        (settings.csv_dir / "readings.csv").write_bytes(
            "id,ts,temp\n1,2026-10-21 08:00,温度\n".encode()
        )
        body = _wait(client, lambda tasks: tasks["ingest watcher"]["last_error"])
        assert body["tasks"]["ingest watcher"]["last_error"] == "CsvEncodingError"
        assert body["tasks"]["ingest watcher"]["running"] is True


@pytest.mark.parametrize("fails", [False, True])
def test_backup_setup_records_its_result(tmp_path, monkeypatch, fails):
    from monitor_app.services import backup_task

    if fails:

        def broken(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(backup_task, "backup_database", broken)
    app = create_app(_config(), _settings(tmp_path))
    task = next(t for t in app.state.services.background_tasks if t.name == "backup")
    task.setup()
    if fails:
        assert (task.last_tick, task.last_error) == (None, "OSError")
    else:
        assert task.last_tick is not None and task.last_error is None
    app.state.services.close()
