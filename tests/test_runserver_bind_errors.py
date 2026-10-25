"""待ち受けできないポート・ホストを、取り込みや起動より前に短く案内すること。

1024 未満のポート(管理者の権限が必要)・Windows の予約済みのポート・名前を引けない
ホスト・この PC のものでないアドレスは、uvicorn の ERROR(終了コード 3)ではなく、
1〜2 行の案内と終了コード 1 で止める。CSV の取り込みも始めない。
"""

from __future__ import annotations

import errno
import socket

import pytest
from typer.testing import CliRunner

from monitor_app.cli import app
from monitor_app.cli import data as data_mod
from monitor_app.cli import server as server_mod


@pytest.fixture
def project(tmp_path, monkeypatch):
    runner = CliRunner()
    result = runner.invoke(app, ["startproject", str(tmp_path / "proj")])
    assert result.exit_code == 0, result.output
    monkeypatch.chdir(tmp_path / "proj")
    monkeypatch.setattr(
        "monitor_app.logging_conf.configure_logging", lambda *a, **k: None
    )
    return tmp_path / "proj"


@pytest.fixture
def no_server(monkeypatch):
    calls: list = []
    monkeypatch.setattr(server_mod, "run_uvicorn", lambda *a, **k: calls.append(a))
    imports: list = []
    monkeypatch.setattr(server_mod, "run_csv_import", lambda *a, **k: imports.append(a))
    return calls, imports


def _refuse_with(monkeypatch, exc: OSError) -> None:
    def refuse(address, *, family):
        raise exc

    monkeypatch.setattr(socket, "create_server", refuse)


@pytest.mark.parametrize(
    "exc",
    [
        PermissionError(errno.EACCES, "Permission denied"),
        OSError(10013, "An attempt was made to access a socket in a way forbidden"),
    ],
    ids=["eacces", "windows-10013"],
)
def test_a_port_without_permission_stops_before_importing(
    project, no_server, monkeypatch, real_port_probe, exc
):
    _refuse_with(monkeypatch, exc)
    result = CliRunner().invoke(
        app, ["runserver", "--port", "80", "--import-csv-if-empty"]
    )
    assert result.exit_code == 1, result.output
    assert "ポート 80 で待ち受けする権限がありません" in result.output
    assert "--port 9990" in result.output
    calls, imports = no_server
    assert calls == [] and imports == []  # 取り込みも起動もしない


def test_an_unresolvable_host_stops_with_a_hint(
    project, no_server, monkeypatch, real_port_probe
):
    def unknown(*_args, **_kwargs):
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", unknown)
    result = CliRunner().invoke(app, ["runserver", "--host", "no-such-host.invalid"])
    assert result.exit_code == 1, result.output
    assert "ホスト no-such-host.invalid で待ち受けできません" in result.output
    assert "127.0.0.1" in result.output and "0.0.0.0" in result.output
    # LAN の URL の案内(ほかの PC からは …)は出さない
    assert "ほかの PC からは" not in result.output


def test_an_address_of_another_machine_is_refused_for_real(
    project, no_server, real_port_probe
):
    # 192.0.2.1(TEST-NET-1)はどの PC のアドレスでもない
    result = CliRunner().invoke(
        app, ["runserver", "--host", "192.0.2.1", "--port", "0"]
    )
    assert result.exit_code == 1, result.output
    assert "この PC のアドレスではありません" in result.output


def test_serve_uses_the_same_checks(tmp_path, monkeypatch, real_port_probe):
    (tmp_path / "data.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "monitor_app.logging_conf.configure_logging", lambda *a, **k: None
    )
    monkeypatch.setattr(data_mod, "run_csv_import", lambda *a, **k: pytest.fail())
    _refuse_with(monkeypatch, PermissionError(errno.EACCES, "Permission denied"))
    result = CliRunner().invoke(app, ["serve", "data.csv", "--port", "443"])
    assert result.exit_code == 1, result.output
    assert "ポート 443 で待ち受けする権限がありません" in result.output
