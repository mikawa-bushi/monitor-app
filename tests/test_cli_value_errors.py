"""実行時設定のよくある誤りを、秘密を漏らさずに 3 行以内のメッセージで知らせる。

- パスワードに ``@`` などの記号をそのまま書いた MONITOR_DATABASE_URL: SQLAlchemy は
  最初の ``@`` の後ろをホスト名として読むため、パスワードの残りが起動時のログや接続
  エラーに出ていた(MySQL の例では ValueError のトレースバックにも出た)。解釈できない
  URL として扱い、URL エンコードの案内を出す。
- 範囲外のポート番号(``MONITOR_PORT=99999`` / ``--port 70000``): 待ち受けの直前で
  OverflowError のトレースバックになっていた。
- 不正なログのレベル(``MONITOR_LOG_LEVEL=verbose``): ログの設定や uvicorn の起動で
  トレースバックになっていた(``check`` を含む全コマンド)。v2.3.1 で使えた名前
  (logging の別名 warn・fatal・notset、runserver の trace)は、すべてのコマンドで使える。
"""

from __future__ import annotations

import logging

import pytest
from contract import helpers
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from typer.testing import CliRunner

from monitor_app import main as main_mod
from monitor_app.cli import app
from monitor_app.db.engine import Database, describe_url, parse_url
from monitor_app.settings.runtime import AppSettings, settings_error_message

#: パスワードの一部(ログ・エラーに出てはいけない文字列)。
LEAK = "Secret"
#: パスワードの @ を URL エンコードしていない接続 URL。
UNENCODED = [
    f"postgresql+pg8000://monitor:Top@{LEAK}@127.0.0.1:1/monitor",
    f"mysql+pymysql://u:p@ss:{LEAK}@rd@db:3306/x",  # ポートとして読めず ValueError
    f"postgresql://admin@corp:pw@{LEAK}@db.example.com/app",
]


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    helpers.isolate_from_host(monkeypatch, tmp_path)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def project(tmp_path, monkeypatch) -> None:
    proj = helpers.start_project(tmp_path)
    monkeypatch.chdir(proj)


# ---------------------------------------------------------------------------
# 接続 URL
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "url",
    [
        "sqlite:///instances/database.db",
        "postgresql+psycopg://user:p%40ss%3Aw%2Frd@db.example.com:5432/app",
        "mysql+pymysql://u:p:a/s?s#x@h:3306/db",  # @ 以外の記号はそのまま読める
        "postgresql://myadmin@myserver:pw@myserver.example.com/db",  # Azure 形式
        "postgresql://u:pw@[::1]:5432/db",
        "sqlite:///instances/a@b.db",  # SQLite のパスの @ はホスト名ではない
        "sqlite:////srv/data@site/database.db",
    ],
)
def test_valid_urls_parse_as_before(url):
    assert parse_url(url) == make_url(url)


@pytest.mark.parametrize("url", UNENCODED)
def test_unencoded_at_signs_never_reach_logs_or_errors(url):
    with pytest.raises(ArgumentError) as info:
        parse_url(url)
    assert LEAK not in str(info.value) and info.value.__cause__ is None
    assert "URL エンコード" in str(info.value)
    with pytest.raises(ArgumentError) as info:
        Database(url)  # 接続を試す前に止める(接続エラーにもホスト名として出ない)
    assert LEAK not in str(info.value)
    assert LEAK not in describe_url(url)
    assert "解釈できない接続 URL" in describe_url(url)


def _load_like_uvicorn(target, **kwargs):
    """uvicorn.run の代わり: uvicorn と同じく、起動の前にアプリファクトリを呼ぶ。"""
    assert target == "monitor_app.main:app_factory" and kwargs.get("factory")
    main_mod.app_factory()
    pytest.fail("アプリを組み立てられてしまった")


@pytest.mark.parametrize("command", [["import-csv"], ["runserver"], ["migrate"]])
@pytest.mark.parametrize("url", UNENCODED)
def test_cli_explains_unencoded_passwords_briefly(
    project, runner, monkeypatch, url, command
):
    monkeypatch.setenv("MONITOR_DATABASE_URL", url)
    monkeypatch.setenv("MONITOR_CONFIG_PATH", "config.py")  # runserver が書き換える
    monkeypatch.setattr("uvicorn.run", _load_like_uvicorn)
    result = runner.invoke(app, command)
    assert result.exit_code == 1
    assert LEAK not in result.output
    assert "Traceback" not in result.output
    assert "MONITOR_DATABASE_URL の形式が正しくありません" in result.stderr
    assert "%40" in result.stderr  # 直し方(URL エンコード)


@pytest.mark.parametrize("url", UNENCODED)
def test_check_reports_unencoded_passwords(project, runner, monkeypatch, url):
    monkeypatch.setenv("MONITOR_DATABASE_URL", url)
    result = runner.invoke(app, ["check"])
    assert result.exit_code == 1
    assert LEAK not in result.output
    assert "MONITOR_DATABASE_URL を接続先として解釈できません" in result.stdout
    assert "%40" in result.stdout


# ---------------------------------------------------------------------------
# ポート番号
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("command", "env"),
    [
        (["runserver", "--port", "70000"], {}),
        (["runserver"], {"MONITOR_PORT": "99999"}),
        (["runserver", "--reload"], {"MONITOR_PORT": "99999"}),
        (["runserver", "--port", "-5"], {}),
    ],
)
def test_runserver_rejects_ports_out_of_range(
    project, runner, monkeypatch, command, env
):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr("uvicorn.run", lambda *a, **kw: pytest.fail("起動した"))
    result = runner.invoke(app, command)
    assert result.exit_code == 1
    assert "は使えません(0〜65535 の範囲で指定してください)" in result.stderr
    assert "Traceback" not in result.output


def test_serve_rejects_ports_out_of_range(tmp_path, runner, monkeypatch):
    (tmp_path / "data.csv").write_text("id,v\n1,2\n", encoding="utf-8")
    monkeypatch.setattr("uvicorn.run", lambda *a, **kw: pytest.fail("起動した"))
    result = runner.invoke(app, ["serve", str(tmp_path), "--port", "70000"])
    assert result.exit_code == 1
    assert "ポート番号 70000 は使えません" in result.stderr


@pytest.mark.usefixtures("real_port_probe")
def test_the_port_probe_itself_rejects_ports_out_of_range():
    import typer

    from monitor_app.cli import server

    with pytest.raises(typer.Exit):
        server.ensure_port_available("127.0.0.1", 65536)


def test_check_reports_ports_out_of_range(project, runner, monkeypatch):
    monkeypatch.setenv("MONITOR_PORT", "70000")
    result = runner.invoke(app, ["check"])
    assert result.exit_code == 1
    assert "MONITOR_PORT=70000 はポート番号として使えません" in result.stdout


# ---------------------------------------------------------------------------
# ログのレベル
# ---------------------------------------------------------------------------
#: v2.3.1 でも使えた名前(import-csv・sync-sources は logging の名前、runserver は trace も)。
ACCEPTED_LEVELS = ["DEBUG", "info", "Warning", "warn", "error", "fatal", "critical"]
ACCEPTED_LEVELS += ["trace", "NOTSET"]


@pytest.mark.parametrize("value", ["verbose", "", "10", "warnings", "all"])
def test_unknown_log_levels_are_a_short_settings_error(monkeypatch, value):
    monkeypatch.setenv("MONITOR_LOG_LEVEL", value)
    with pytest.raises(Exception) as info:
        AppSettings(_env_file=None)
    message = settings_error_message(info.value)
    assert message is not None and "\n" not in message
    assert "MONITOR_LOG_LEVEL" in message
    assert "critical, error, warning, info, debug, trace" in message


@pytest.mark.parametrize("value", [*ACCEPTED_LEVELS, " error "])
def test_log_levels_that_worked_before_are_kept(monkeypatch, value):
    import uvicorn.config

    from monitor_app import logging_conf

    monkeypatch.setenv("MONITOR_LOG_LEVEL", value)
    settings = AppSettings(_env_file=None)
    assert settings.log_level == value.strip()
    # uvicorn にもそのまま使える名前で渡す(warn → warning など)
    assert logging_conf.uvicorn_level(settings.log_level) in uvicorn.config.LOG_LEVELS


def test_runserver_passes_a_level_uvicorn_knows(project, runner, monkeypatch):
    calls = []
    monkeypatch.setenv("MONITOR_LOG_LEVEL", "WARN")
    monkeypatch.setenv("MONITOR_CONFIG_PATH", "config.py")  # runserver が書き換える
    monkeypatch.setattr("uvicorn.run", lambda *a, **kw: calls.append(kw))
    result = runner.invoke(app, ["runserver"])
    assert result.exit_code == 0, result.output
    (kwargs,) = calls
    assert kwargs["log_level"] == "warning"


@pytest.mark.parametrize("command", [["check"], ["import-csv"], ["runserver"]])
def test_cli_reports_unknown_log_levels_briefly(project, runner, monkeypatch, command):
    monkeypatch.setenv("MONITOR_LOG_LEVEL", "verbose")
    monkeypatch.setattr("uvicorn.run", lambda *a, **kw: pytest.fail("起動した"))
    result = runner.invoke(app, command)
    assert result.exit_code == 1
    assert "MONITOR_LOG_LEVEL='verbose'" in result.stderr
    assert "Traceback" not in result.output


def test_app_factory_reports_unknown_log_levels(monkeypatch, caplog):
    monkeypatch.setenv("MONITOR_LOG_LEVEL", "verbose")
    monkeypatch.setattr(logging.getLogger("monitor_app"), "propagate", True)
    with (
        caplog.at_level(logging.ERROR, logger="monitor_app.main"),
        pytest.raises(SystemExit) as info,
    ):
        main_mod.app_factory()
    assert info.value.code == 1
    assert any("MONITOR_LOG_LEVEL='verbose'" in r.getMessage() for r in caplog.records)


def test_real_cli_processes_with_log_levels(tmp_path):
    """インプロセスでは差し替えているログの設定も含めて、本物のプロセスで確かめる。"""
    created = helpers.run_cli(
        "startproject", "proj", cwd=tmp_path, env=helpers.child_env()
    )
    assert created.returncode == 0
    env = helpers.child_env(MONITOR_LOG_LEVEL="verbose")
    result = helpers.run_cli("check", cwd=tmp_path / "proj", env=env)
    stderr = result.stderr.decode("utf-8", "replace")
    assert result.returncode == 1, stderr
    assert "MONITOR_LOG_LEVEL='verbose'" in stderr
    assert "Traceback" not in stderr
    assert len([line for line in stderr.splitlines() if line.strip()]) <= 3
    # v2.3.1 で使えた名前は、ログの設定(dictConfig)を通る(trace は uvicorn の読み込み前でも)
    for value in ("warn", "fatal", "notset", "trace"):
        env = helpers.child_env(MONITOR_LOG_LEVEL=value)
        result = helpers.run_cli("check", cwd=tmp_path / "proj", env=env)
        assert result.returncode == 0, (value, result.stderr.decode("utf-8", "replace"))
