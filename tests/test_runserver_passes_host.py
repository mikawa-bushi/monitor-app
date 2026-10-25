"""``runserver --host`` の値がアプリ(app_factory)の設定にも届く(D6-03 の続き)。

app_factory は環境変数から設定を読み直すため、``--host 0.0.0.0`` だけを渡すと
アプリ側は既定の 127.0.0.1 で待ち受けていると誤り、同じ PC のリバースプロキシが
元の名前のまま転送した要求を Host ヘッダの確認(DNS リバインディング対策)で断って
しまう。runserver は MONITOR_CONFIG_PATH と同じく MONITOR_HOST も渡す。
"""

from __future__ import annotations

import os

import pytest
from typer.testing import CliRunner

from monitor_app.cli import app
from monitor_app.cli import server as server_mod
from monitor_app.security import host_policy
from monitor_app.settings.runtime import AppSettings


def _policy(settings: AppSettings):
    return host_policy(
        bound_host=settings.host,
        allowed_hosts=settings.allowed_hosts,
        root_path=settings.root_path,
        forwarded_allow_ips=settings.forwarded_allow_ips,
    )


@pytest.fixture
def project(tmp_path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["startproject", "proj"]).exit_code == 0
    monkeypatch.chdir(tmp_path / "proj")
    # runserver が書き換える値の後始末(monkeypatch が元に戻す)
    monkeypatch.setenv("MONITOR_CONFIG_PATH", "")
    monkeypatch.setenv("MONITOR_HOST", "127.0.0.1")
    monkeypatch.setattr(server_mod, "_in_container", lambda: False)
    return tmp_path / "proj"


@pytest.fixture
def started(monkeypatch) -> list[dict]:
    seen: list[dict] = []

    def fake_run(target, **kw):
        # uvicorn が app_factory を呼ぶ時点の設定
        seen.append({"kw": kw, "settings": AppSettings()})

    monkeypatch.setattr("uvicorn.run", fake_run)
    return seen


def test_cli_host_reaches_the_app_settings(project, started):
    result = CliRunner().invoke(app, ["runserver", "--host", "0.0.0.0"])
    assert result.exit_code == 0, result.output
    [call] = started
    assert call["kw"]["host"] == "0.0.0.0"
    assert os.environ["MONITOR_HOST"] == "0.0.0.0"
    assert call["settings"].host == "0.0.0.0"
    # LAN に公開しているので、Host ヘッダの確認は既定では行わない
    assert _policy(call["settings"]) is None


def test_default_host_stays_loopback(project, started):
    result = CliRunner().invoke(app, ["runserver"])
    assert result.exit_code == 0, result.output
    [call] = started
    assert call["settings"].host == "127.0.0.1"
    assert _policy(call["settings"]) is not None
