"""実行時設定(MONITOR_*)の値の誤りを、トレースバックではなく短いメッセージで知らせる。

``MONITOR_PORT=abc`` や ``MONITOR_LOG_FORMAT=xml`` で、pydantic の長いエラーや rich の
トレースバックの代わりに、変数名と指定できる値を 3 行以内で示す(CLI)。
``main.app_factory`` は 1 行のログを出して終了コード 1 で終える。
"""

from __future__ import annotations

import logging

import pytest
from contract import helpers
from pydantic import BaseModel, ValidationError
from typer.testing import CliRunner

from monitor_app import main as main_mod
from monitor_app.cli import app as cli_app
from monitor_app.settings.runtime import AppSettings, settings_error_message


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    helpers.isolate_from_host(monkeypatch, tmp_path)


def _error(monkeypatch, **env: str) -> BaseException:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(Exception) as info:
        AppSettings(_env_file=None)
    return info.value


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"MONITOR_PORT": "abc"}, "MONITOR_PORT='abc'(整数を指定してください)"),
        (
            {"MONITOR_LOG_FORMAT": "xml"},
            "MONITOR_LOG_FORMAT='xml'(text / json のいずれかを指定してください)",
        ),
        (
            {"MONITOR_SMTP_SECURITY": "tls"},
            "MONITOR_SMTP_SECURITY='tls'(starttls / ssl / none のいずれかを指定してください)",
        ),
        (
            {"MONITOR_PROTECT_READS": "maybe"},
            "MONITOR_PROTECT_READS='maybe'(true / false のいずれかを指定してください)",
        ),
        ({"MONITOR_INGEST_INTERVAL": "fast"}, "MONITOR_INGEST_INTERVAL='fast'(数値"),
        ({"MONITOR_SQLITE_JOURNAL_MODE": "bogus"}, "wal, delete, truncate"),
        ({"MONITOR_CORS_ORIGINS": "http://a,http://b"}, "MONITOR_CORS_ORIGINS"),
    ],
)
def test_settings_errors_name_the_variable_and_the_allowed_values(
    monkeypatch, env, expected
):
    message = settings_error_message(_error(monkeypatch, **env))
    assert message is not None
    assert message.startswith("実行時設定(環境変数・.env)の値") and "\n" not in message
    assert expected in message
    assert "pydantic" not in message and "https://" not in message


def test_list_settings_explain_the_json_format(monkeypatch):
    message = settings_error_message(
        _error(monkeypatch, MONITOR_CORS_ORIGINS="http://a,http://b")
    )
    assert "JSON 形式" in message


def test_several_errors_are_listed_on_one_line(monkeypatch):
    message = settings_error_message(
        _error(
            monkeypatch,
            MONITOR_PORT="x",
            MONITOR_LOG_FORMAT="y",
            MONITOR_INGEST_INTERVAL="z",
            MONITOR_NOTIFY_RETRIES="w",
        )
    )
    assert message.count("MONITOR_") == 3 and "ほか 1 件" in message


def test_secret_values_are_not_echoed():
    from monitor_app.settings import runtime

    text = runtime._describe_validation_error(
        {"loc": ("smtp_password",), "type": "string_type", "input": "hunter2"}
    )
    assert text.startswith("MONITOR_SMTP_PASSWORD(") and "hunter2" not in text
    long_value = runtime._describe_validation_error(
        {"loc": ("port",), "type": "int_parsing", "input": "9" * 100}
    )
    assert "..." in long_value and len(long_value) < 100


def test_other_validation_errors_are_left_alone():
    class Other(BaseModel):
        port: int

    with pytest.raises(ValidationError) as info:
        Other(port="abc")
    assert settings_error_message(info.value) is None
    assert settings_error_message(ValueError("x")) is None


@pytest.mark.parametrize(
    "command", [["runserver"], ["runserver", "--import-csv"], ["import-csv"]]
)
def test_cli_shows_at_most_three_lines_without_a_traceback(
    tmp_path, monkeypatch, command
):
    (tmp_path / "config.py").write_text(
        "from monitor_app import MonitorConfig\nconfig = MonitorConfig()\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MONITOR_PORT", "abc")
    monkeypatch.setattr("uvicorn.run", lambda *a, **kw: pytest.fail("起動した"))
    result = CliRunner().invoke(cli_app, command)
    assert result.exit_code == 1
    lines = [line for line in result.output.splitlines() if line.strip()]
    assert 1 <= len(lines) <= 3, result.output
    assert "MONITOR_PORT='abc'" in lines[0] and "整数" in lines[0]
    assert ".env" in result.output
    assert "Traceback" not in result.output and "ValidationError" not in result.output


def test_serve_reports_settings_errors_too(tmp_path, monkeypatch):
    (tmp_path / "a.csv").write_text("id,v\n1,2\n", encoding="utf-8")
    monkeypatch.setenv("MONITOR_LOG_FORMAT", "xml")
    result = CliRunner().invoke(cli_app, ["serve", str(tmp_path)])
    assert result.exit_code == 1
    assert "MONITOR_LOG_FORMAT='xml'" in result.output
    assert "Traceback" not in result.output


def test_app_factory_logs_one_line_and_exits_1(monkeypatch, caplog):
    monkeypatch.setenv("MONITOR_LOG_FORMAT", "xml")
    monkeypatch.setattr(logging.getLogger("monitor_app"), "propagate", True)
    with (
        caplog.at_level(logging.ERROR, logger="monitor_app.main"),
        pytest.raises(SystemExit) as info,
    ):
        main_mod.app_factory()
    assert info.value.code == 1
    (record,) = [r for r in caplog.records if r.name == "monitor_app.main"]
    assert "MONITOR_LOG_FORMAT='xml'" in record.getMessage()
    assert "起動を中止" in record.getMessage()


def test_real_cli_process_prints_a_short_message(tmp_path):
    created = helpers.run_cli(
        "startproject", "proj", cwd=tmp_path, env=helpers.child_env()
    )
    assert created.returncode == 0
    env = helpers.child_env(MONITOR_PORT="abc")
    result = helpers.run_cli("import-csv", cwd=tmp_path / "proj", env=env)
    stderr = result.stderr.decode("utf-8", "replace")
    assert result.returncode == 1
    assert "MONITOR_PORT='abc'" in stderr
    assert "Traceback" not in stderr
    assert len([line for line in stderr.splitlines() if line.strip()]) <= 3
