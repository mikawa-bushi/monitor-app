"""ロギング設定(テキスト / JSON、uvicorn との書式の統一、アクセスログの切り替え)のテスト。

設定はプロセス全体のロガーを書き換えるため、各テストの後に元へ戻す。出力は、設定後に
ハンドラの出力先を StringIO へ向け直して集める(pytest は実行フェーズの開始時に
``sys.stderr`` を自分の捕捉用に戻すため、``sys.stderr`` の差し替えでは集められない)。
"""

from __future__ import annotations

import io
import json
import logging
import logging.config
import pickle
import re

import pytest
import uvicorn

from monitor_app import logging_conf
from monitor_app.logging_conf import (
    JsonFormatter,
    TextFormatter,
    build_logging_config,
    configure_logging,
    current_log_format,
    uvicorn_log_config,
)
from monitor_app.settings.runtime import AppSettings

_LOGGERS = ("", "monitor_app", "sqlalchemy.engine", "uvicorn", "uvicorn.error")
_LOGGERS += ("uvicorn.access", "uvicorn.asgi")
_TEXT_LINE = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d (INFO|WARNING|ERROR) \S+: ")


@pytest.fixture
def stderr():
    """ロガーの状態を保存して戻し、設定したハンドラの出力を集める StringIO を返す。"""
    saved = {}
    for name in _LOGGERS:
        logger = logging.getLogger(name)
        saved[name] = (list(logger.handlers), logger.level, logger.propagate)
    yield io.StringIO()
    for name, (handlers, level, propagate) in saved.items():
        logger = logging.getLogger(name)
        logger.handlers[:] = handlers
        logger.setLevel(level)
        logger.propagate = propagate


def _capture(buffer: io.StringIO) -> None:
    """設定済みのハンドラ(標準エラー向け)の出力先を ``buffer`` にする。"""
    for name in _LOGGERS:
        for handler in logging.getLogger(name).handlers:
            if isinstance(handler, logging.StreamHandler):
                handler.setStream(buffer)


def _apply(config: dict, buffer: io.StringIO) -> None:
    logging.config.dictConfig(config)
    _capture(buffer)


def _access(status: int = 200) -> None:
    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d', "10.0.0.5:51234", "GET", "/api/health", "1.1", status
    )


def _lines(buffer: io.StringIO) -> list[str]:
    return [line for line in buffer.getvalue().splitlines() if line]


def test_text_format_puts_timestamps_on_monitor_app_and_uvicorn_lines(stderr):
    _apply(build_logging_config("INFO", uvicorn=True), stderr)
    logging.getLogger("monitor_app.importer").info("imported users: 5 rows")
    logging.getLogger("uvicorn.error").info("Started server process [%d]", 42)
    _access()

    lines = _lines(stderr)
    assert len(lines) == 3
    assert all(_TEXT_LINE.match(line) for line in lines), lines
    assert lines[0].endswith("INFO monitor_app.importer: imported users: 5 rows")
    # "uvicorn.error" はエラーと誤解されないよう "uvicorn" と表示する
    assert lines[1].endswith("INFO uvicorn: Started server process [42]")
    assert lines[2].endswith(
        'INFO uvicorn.access: 10.0.0.5:51234 - "GET /api/health HTTP/1.1" 200'
    )


def test_json_format_emits_one_object_per_line(stderr):
    _apply(build_logging_config("INFO", log_format="json", uvicorn=True), stderr)
    logging.getLogger("monitor_app.cli").warning("ポート %s を開けません", 9990)
    logging.getLogger("uvicorn.error").info("Application startup complete.")
    _access(503)
    try:
        raise ValueError("boom")
    except ValueError:
        logging.getLogger("monitor_app.api").exception("失敗しました")

    records = [json.loads(line) for line in _lines(stderr)]
    assert [r["logger"] for r in records] == [
        "monitor_app.cli",
        "uvicorn",
        "uvicorn.access",
        "monitor_app.api",
    ]
    first = records[0]
    assert set(first) == {"time", "level", "logger", "message"}
    assert first["level"] == "WARNING"
    assert first["message"] == "ポート 9990 を開けません"  # 日本語はそのまま
    assert re.match(
        r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}[+-]\d\d:\d\d$", first["time"]
    )
    access = records[2]
    assert access["client"] == "10.0.0.5:51234"
    assert access["method"] == "GET"
    assert access["path"] == "/api/health"
    assert access["http_version"] == "1.1"
    assert access["status"] == 503
    assert "ValueError: boom" in records[3]["exc_info"]


def test_json_access_fields_are_skipped_for_unexpected_arguments(stderr):
    _apply(build_logging_config("INFO", log_format="json", uvicorn=True), stderr)
    logging.getLogger("uvicorn.access").info("plain message")
    (record,) = [json.loads(line) for line in _lines(stderr)]
    assert record["message"] == "plain message"
    assert "status" not in record


def test_access_log_can_be_turned_off(stderr):
    _apply(build_logging_config("INFO", uvicorn=True, access_log=False), stderr)
    _access()
    logging.getLogger("uvicorn.error").info("still logged")
    assert len(_lines(stderr)) == 1
    assert not logging.getLogger("uvicorn.access").hasHandlers()


def test_sql_echo_and_level(stderr):
    _apply(build_logging_config("warning", sql_echo=True), stderr)
    logging.getLogger("monitor_app.x").info("hidden")
    logging.getLogger("sqlalchemy.engine").info("SELECT 1")
    assert [line.split(": ", 1)[1] for line in _lines(stderr)] == ["SELECT 1"]


def test_configure_logging_keeps_the_current_format_unless_given(stderr):
    configure_logging("INFO")
    assert current_log_format() == "text"
    configure_logging("INFO", log_format="json")
    assert current_log_format() == "json"
    # アプリファクトリのように形式を指定せずに呼び直しても JSON のまま
    configure_logging("INFO")
    assert current_log_format() == "json"
    _capture(stderr)
    logging.getLogger("monitor_app").info("still json")
    assert json.loads(_lines(stderr)[-1])["message"] == "still json"
    configure_logging("INFO", log_format="text")
    assert current_log_format() == "text"


def test_unknown_format_is_rejected():
    with pytest.raises(ValueError, match="text / json"):
        build_logging_config(log_format="yaml")


def test_uvicorn_log_config_follows_settings():
    settings = AppSettings(
        _env_file=None, log_level="debug", log_format="json", access_log=False
    )
    config = uvicorn_log_config(settings)
    assert config["formatters"]["monitor"]["()"].endswith("JsonFormatter")
    assert config["loggers"]["monitor_app"]["level"] == "DEBUG"
    assert config["loggers"]["uvicorn.access"]["handlers"] == []
    assert config["disable_existing_loggers"] is False
    # --reload の子プロセスへ渡せる(uvicorn は Config を pickle する)
    assert pickle.loads(pickle.dumps(config)) == config


def test_uvicorn_applies_the_config(stderr):
    """uvicorn 自身が log_config を適用したあとも、同じ書式で出力される。"""
    settings = AppSettings(_env_file=None, log_format="text", access_log=False)
    uvicorn.Config(
        "monitor_app.main:app_factory",
        factory=True,
        log_config=uvicorn_log_config(settings),
        access_log=settings.access_log,
        log_level="info",
    )
    _capture(stderr)
    logging.getLogger("uvicorn.error").info("from uvicorn")
    _access()
    lines = _lines(stderr)
    assert len(lines) == 1
    assert lines[0].endswith("INFO uvicorn: from uvicorn")
    assert isinstance(logging.getLogger("uvicorn").handlers[0].formatter, TextFormatter)


def test_formatters_do_not_rename_the_record_for_other_handlers():
    record = logging.LogRecord(
        "uvicorn.error", logging.INFO, __file__, 1, "x", (), None
    )
    assert "uvicorn:" in TextFormatter().format(record)
    assert json.loads(JsonFormatter().format(record))["logger"] == "uvicorn"
    assert record.name == "uvicorn.error"


def test_module_exposes_the_supported_formats():
    assert logging_conf.LOG_FORMATS == ("text", "json")
