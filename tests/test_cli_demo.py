"""``monitor-app demo`` と ``startproject --template warehouse``。

uvicorn.run を差し替え、渡されたアプリを TestClient で動かして確かめる(demo は
uvicorn.run から戻ると一時フォルダを消すため、確認は差し替えた run の中で行う)。
"""

from __future__ import annotations

import datetime as dt
import signal
import socket
import tempfile
import time
from pathlib import Path

import pytest
from contract import helpers
from fastapi.testclient import TestClient
from sqlalchemy.engine import make_url
from typer.testing import CliRunner

from monitor_app.cli import app as cli_app
from monitor_app.cli import demo as demo_mod
from monitor_app.cli._common import StopRequested
from monitor_app.demo import SOURCE
from monitor_app.demo.simulation import WarehouseSimulator

PAGES = ("/", "/kiosk", "/form/movements", "/table/stock_list")


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    helpers.isolate_from_host(monkeypatch, tmp_path)


@pytest.fixture
def temp_root(tmp_path, monkeypatch) -> Path:
    """一時フォルダの置き場所(後片付けを確かめる)。"""
    root = tmp_path / "tmp"
    root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(root))
    return root


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


class FakeUvicorn:
    """uvicorn.run の代わり。アプリを TestClient で動かし、結果を記録する。"""

    def __init__(self, check=None) -> None:
        self.calls: list[dict] = []
        self.check = check
        self.results: dict = {}

    def __call__(self, app, **kwargs) -> None:
        settings = app.state.settings
        self.calls.append({"app": app, "settings": settings, **kwargs})
        self.workdir = Path(make_url(settings.database_url).database).parent
        if self.check is not None:
            with TestClient(app) as client:
                self.check(client, app, self.results)


@pytest.fixture
def fake_uvicorn(monkeypatch):
    def install(check=None) -> FakeUvicorn:
        fake = FakeUvicorn(check)
        monkeypatch.setattr("uvicorn.run", fake)
        return fake

    return install


def _view_rows(client: TestClient, name: str) -> int:
    response = client.get(f"/api/views/{name}")
    assert response.status_code == 200, name
    return len(response.json()["data"])


def _alerts_once_evaluated(client: TestClient, timeout: float = 10.0) -> list[dict]:
    """アラートの評価(バックグラウンドで画面の更新間隔ごと)が出した一覧。"""
    deadline = time.monotonic() + timeout
    while True:
        alerts = client.get("/api/alerts").json()["alerts"]
        if len(alerts) >= 2 or time.monotonic() > deadline:
            return alerts
        time.sleep(0.1)


def test_demo_serves_a_warehouse_screen_with_both_alerts(
    tmp_path, runner, fake_uvicorn, temp_root
):
    def check(client, app, results):
        config = app.state.services.config
        results["rows"] = {name: _view_rows(client, name) for name in config.views}
        results["kpis"] = client.get("/api/kpis").json()["kpis"]
        results["alerts"] = _alerts_once_evaluated(client)
        results["pages"] = {path: client.get(path) for path in PAGES}
        results["tasks"] = client.get("/api/health").json()["tasks"]

    before = sorted(tmp_path.iterdir())
    fake = fake_uvicorn(check)
    result = runner.invoke(cli_app, ["demo"])
    assert result.exit_code == 0, result.output

    (call,) = fake.calls
    assert (call["host"], call["port"]) == ("127.0.0.1", 9990)
    rows = fake.results["rows"]
    just_after_midnight = dt.datetime.now().time() < dt.time(0, 2)
    for name, count in rows.items():
        if name == "hourly_today" and just_after_midnight:
            continue
        assert count > 0, name
    kpis = {k["key"]: k for k in fake.results["kpis"]}
    assert all(k["value"] is not None for k in kpis.values())
    assert (
        kpis["below_safety"]["value"] >= 1 and kpis["below_safety"]["status"] == "bad"
    )
    levels = {a["level"] for a in fake.results["alerts"]}
    assert levels == {"warning", "critical"}
    pages = fake.results["pages"]
    assert {page.status_code for page in pages.values()} == {200}
    assert "<h1>入出庫の入力</h1>" in pages["/form/movements"].text
    assert '<a href="/form/movements">📝 入出庫の入力</a>' in pages["/"].text
    assert fake.results["tasks"]["warehouse demo"]["running"] is True
    tasks = call["app"].state.services.background_tasks
    assert any(isinstance(task, WarehouseSimulator) for task in tasks)

    assert fake.workdir.parent == temp_root and fake.workdir.name.startswith(
        demo_mod.TEMP_PREFIX
    )
    assert list(temp_root.iterdir()) == []
    assert sorted(tmp_path.iterdir()) == before
    assert "データは架空" in result.output and "終了すると消えます" in result.output
    assert str(SOURCE / "config.py") in result.output
    assert "startproject my-warehouse --template warehouse" in result.output


def test_demo_ignores_dotenv_and_never_notifies_or_backs_up(
    tmp_path, runner, fake_uvicorn, temp_root, monkeypatch
):
    (tmp_path / ".env").write_text(
        "MONITOR_PORT=1234\nMONITOR_API_KEY=from-dotenv\n", encoding="utf-8"
    )
    for key, value in {
        "MONITOR_WEBHOOK_URL": "https://hooks.example.com/x",
        "MONITOR_SMTP_HOST": "smtp.example.com",
        "MONITOR_ALERT_EMAIL_TO": "ops@example.com",
        "MONITOR_BACKUP_DIR": str(tmp_path / "backups"),
        "MONITOR_INGEST_WATCH": "true",
        "MONITOR_DATABASE_URL": f"sqlite:///{(tmp_path / 'mine.db').as_posix()}",
        "MONITOR_CSV_DIR": str(tmp_path / "mine"),
    }.items():
        monkeypatch.setenv(key, value)
    fake = fake_uvicorn()
    result = runner.invoke(cli_app, ["demo"])
    assert result.exit_code == 0, result.output

    settings = fake.calls[0]["settings"]
    assert settings.port == 9990 and settings.api_key is None
    assert settings.webhook_url is None and settings.smtp_host is None
    assert settings.alert_email_to is None and settings.backup_dir is None
    assert settings.ingest_watch is False
    assert Path(make_url(settings.database_url).database).parent == fake.workdir
    assert settings.csv_dir == fake.workdir / "csv"
    from monitor_app.services.notifiers import build_notifiers

    assert set(build_notifiers(settings)) == {"console"}
    assert not (tmp_path / "backups").exists() and not (tmp_path / "mine.db").exists()


def test_demo_uses_the_host_and_port_from_the_environment(
    runner, fake_uvicorn, temp_root, monkeypatch
):
    monkeypatch.setenv("MONITOR_HOST", "0.0.0.0")
    monkeypatch.setenv("MONITOR_PORT", "8000")
    fake = fake_uvicorn()
    result = runner.invoke(cli_app, ["demo"])
    assert result.exit_code == 0, result.output
    assert (fake.calls[0]["host"], fake.calls[0]["port"]) == ("0.0.0.0", 8000)
    assert "API キーが未設定" in result.output
    result = runner.invoke(cli_app, ["demo", "--host", "127.0.0.1", "--port", "9123"])
    assert (fake.calls[1]["host"], fake.calls[1]["port"]) == ("127.0.0.1", 9123)


def test_demo_busy_port_fails_before_creating_anything(
    runner, fake_uvicorn, temp_root, real_port_probe
):
    fake = fake_uvicorn()
    with socket.create_server(("127.0.0.1", 0)) as sock:
        port = sock.getsockname()[1]
        result = runner.invoke(cli_app, ["demo", "--port", str(port)])
    assert result.exit_code == 1
    assert f"ポート {port} は使用中です" in result.output
    assert fake.calls == [] and list(temp_root.iterdir()) == []


def test_demo_removes_the_temp_folder_when_the_server_fails(
    runner, temp_root, monkeypatch
):
    def boom(app, **kwargs):
        raise OSError("bind failed")

    monkeypatch.setattr("uvicorn.run", boom)
    result = runner.invoke(cli_app, ["demo"])
    assert result.exit_code != 0
    assert list(temp_root.iterdir()) == []


def test_sigterm_and_ctrl_c_resent_by_uvicorn_are_normal_exits(
    runner, temp_root, monkeypatch
):
    previous = signal.getsignal(signal.SIGTERM)
    monkeypatch.setattr(
        "uvicorn.run", lambda app, **kw: signal.raise_signal(signal.SIGTERM)
    )
    result = runner.invoke(cli_app, ["demo"])
    assert result.exit_code == 0, result.output
    assert list(temp_root.iterdir()) == []
    assert signal.getsignal(signal.SIGTERM) is previous

    def interrupted(app, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr("uvicorn.run", interrupted)
    result = runner.invoke(cli_app, ["demo"])
    assert result.exit_code == 0, result.output
    assert list(temp_root.iterdir()) == []


def test_interrupt_before_serving_aborts_and_cleans_up(runner, temp_root, monkeypatch):
    calls: list = []
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: calls.append(app))

    def terminated(*args, **kwargs):
        raise StopRequested

    monkeypatch.setattr(demo_mod, "run_csv_import", terminated)
    result = runner.invoke(cli_app, ["demo"])
    assert result.exit_code == 143 and calls == []
    assert list(temp_root.iterdir()) == []

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(demo_mod, "run_csv_import", interrupted)
    result = runner.invoke(cli_app, ["demo"])
    assert result.exit_code not in (0, 143) and calls == []  # click の中断(Aborted!)
    assert list(temp_root.iterdir()) == []


def test_startproject_warehouse_passes_check_and_serves(tmp_path, runner, monkeypatch):
    result = runner.invoke(cli_app, ["startproject", "wh", "--template", "warehouse"])
    assert result.exit_code == 0, result.output
    proj = tmp_path / "wh"
    names = {p.relative_to(proj).as_posix() for p in proj.rglob("*") if p.is_file()}
    assert {
        "config.py",
        "README.md",
        ".env.example",
        ".gitignore",
        "instances/.gitkeep",
        "csv/items.csv",
        "csv/locations.csv",
        "csv/lots.csv",
        "csv/movements.csv",
    } == names
    assert (proj / "config.py").read_bytes() == (SOURCE / "config.py").read_bytes()

    monkeypatch.chdir(proj)
    result = runner.invoke(cli_app, ["check", "--strict", "--data"])
    assert result.exit_code == 0, result.output

    served: dict = {}

    def fake_run(target, **kwargs):
        from monitor_app.main import app_factory

        with TestClient(app_factory()) as client:
            served["rows"] = _view_rows(client, "stock_list")
            served["kpis"] = client.get("/api/kpis").status_code

    monkeypatch.setenv("MONITOR_CONFIG_PATH", "")
    monkeypatch.setattr("uvicorn.run", fake_run)
    result = runner.invoke(cli_app, ["runserver", "--import-csv-if-empty"])
    assert result.exit_code == 0, result.output
    assert served == {"rows": served["rows"], "kpis": 200} and served["rows"] > 0


def test_startproject_warehouse_rejects_from_csv_before_creating_anything(
    tmp_path, runner
):
    result = runner.invoke(
        cli_app,
        ["startproject", "wh", "--template", "warehouse", "--from-csv", "."],
    )
    assert result.exit_code == 1
    assert "--from-csv" in result.output
    assert not (tmp_path / "wh").exists()
