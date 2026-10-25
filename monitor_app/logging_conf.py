"""ロギング設定。``print()`` を使わず、時刻付きの 1 つの書式で出力する。

monitor_app のロガーと uvicorn のロガー(起動メッセージ・アクセスログ)を同じ書式・
同じ出力先(標準エラー)にそろえる。リダイレクトしたログファイルでも行の順序が
入れ替わらない(標準出力はファイルへのリダイレクト時にバッファされるため)。

- ``log_format="text"`` … ``2026-10-21 08:00:00 INFO monitor_app.importer: ...``(従来の形式)
- ``log_format="json"`` … 1 行 1 JSON(ログ収集基盤向け。標準ライブラリだけで出力する)
"""

from __future__ import annotations

import contextlib
import copy
import json
import logging
from datetime import datetime
from logging.config import dictConfig
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .settings.runtime import AppSettings

#: 指定できるログの形式。
LOG_FORMATS = ("text", "json")
#: テキスト形式の書式(monitor_app と uvicorn で共通)。
TEXT_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

#: uvicorn の最も詳しいログのレベル(uvicorn と同じ値・名前で登録する。uvicorn を
#: 読み込む前でも MONITOR_LOG_LEVEL=trace でログを設定できるように)。
TRACE = 5
logging.addLevelName(TRACE, "TRACE")
#: uvicorn が知らないログのレベル名 → uvicorn の名前(Python の logging の別名)。
_UVICORN_LEVEL_ALIASES = {"warn": "warning", "fatal": "critical", "notset": "trace"}

# uvicorn の主ロガー名は "uvicorn.error" だが、起動メッセージなど通常のログも流れる。
# 利用者がエラーと誤解しないよう、表示名を "uvicorn" にする。
_DISPLAY_NAMES = {"uvicorn.error": "uvicorn"}


def uvicorn_level(level: str) -> str:
    """``uvicorn.run(log_level=...)`` に渡すレベル名(warn・fatal などの別名を直す)。"""
    name = level.strip().lower()
    return _UVICORN_LEVEL_ALIASES.get(name, name)


#: uvicorn のアクセスログの引数(client_addr, method, path, http_version, status)。
_ACCESS_FIELDS = ("client", "method", "path", "http_version", "status")


class TextFormatter(logging.Formatter):
    """時刻・レベル・ロガー名・メッセージの 1 行(uvicorn のロガー名は短く表示する)。"""

    def __init__(self) -> None:
        super().__init__(TEXT_FORMAT, DATE_FORMAT)

    def format(self, record: logging.LogRecord) -> str:
        name = _DISPLAY_NAMES.get(record.name)
        if name is not None:
            # 同じレコードはほかのハンドラにも渡るため、写しの名前だけを変える。
            record = copy.copy(record)
            record.name = name
        return super().format(record)


class JsonFormatter(logging.Formatter):
    """1 レコードを 1 行の JSON にする。

    キーは ``time``(オフセット付き ISO 8601)・``level``・``logger``・``message``。
    uvicorn のアクセスログには ``client``・``method``・``path``・``http_version``・
    ``status`` を、例外があれば ``exc_info`` を加える。
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "time": datetime.fromtimestamp(record.created)
            .astimezone()
            .isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": _DISPLAY_NAMES.get(record.name, record.name),
            "message": record.getMessage(),
        }
        if record.name == "uvicorn.access":
            payload.update(_access_fields(record))
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack_info"] = self.formatStack(record.stack_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def _access_fields(record: logging.LogRecord) -> dict[str, Any]:
    """uvicorn のアクセスログの引数を名前付きの項目にする(形が違えば何も足さない)。"""
    args = record.args
    if not isinstance(args, tuple) or len(args) != len(_ACCESS_FIELDS):
        return {}
    fields = dict(zip(_ACCESS_FIELDS, args))
    with contextlib.suppress(TypeError, ValueError):
        fields["status"] = int(fields["status"])
    return fields


_FORMATTER_CLASSES = {"text": TextFormatter, "json": JsonFormatter}


def current_log_format() -> str:
    """``monitor_app`` ロガーに今設定されている形式(未設定なら ``"text"``)。"""
    for handler in logging.getLogger("monitor_app").handlers:
        if isinstance(handler.formatter, JsonFormatter):
            return "json"
    return "text"


def build_logging_config(
    level: str = "INFO",
    *,
    sql_echo: bool = False,
    log_format: str = "text",
    uvicorn: bool = False,
    access_log: bool = True,
) -> dict[str, Any]:
    """``logging.config.dictConfig`` に渡す設定を組み立てる。

    ``uvicorn=True`` なら uvicorn のロガー(``uvicorn``・``uvicorn.error``・
    ``uvicorn.access``)も同じハンドラへ向ける。``access_log=False`` でアクセスログを
    出さない。値はすべて文字列・数値で、uvicorn の ``--reload`` の子プロセスへも
    そのまま渡せる(pickle 可能)。
    """
    if log_format not in _FORMATTER_CLASSES:
        raise ValueError(
            f"ログの形式は {' / '.join(LOG_FORMATS)} のいずれかです: {log_format!r}"
        )
    formatter = _FORMATTER_CLASSES[log_format]
    level = level.upper()
    loggers: dict[str, Any] = {
        "monitor_app": {"handlers": ["console"], "level": level, "propagate": False},
        "sqlalchemy.engine": {
            "handlers": ["console"],
            "level": "INFO" if sql_echo else "WARNING",
            "propagate": False,
        },
    }
    if uvicorn:
        # レベルは uvicorn.run(log_level=...) が設定後に上書きする。
        loggers["uvicorn"] = {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        }
        loggers["uvicorn.error"] = {"level": "INFO"}  # "uvicorn" のハンドラへ伝播
        loggers["uvicorn.access"] = {
            "handlers": ["console"] if access_log else [],
            "level": "INFO",
            "propagate": False,
        }
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "monitor": {"()": f"{formatter.__module__}.{formatter.__qualname__}"}
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "formatter": "monitor",
                "stream": "ext://sys.stderr",
            }
        },
        "loggers": loggers,
        "root": {"handlers": ["console"], "level": level},
    }


def configure_logging(
    level: str = "INFO", sql_echo: bool = False, *, log_format: str | None = None
) -> None:
    """monitor_app・SQLAlchemy・ルートのロガーを設定する。

    ``log_format`` を省略すると、今の形式(未設定なら text)を引き継ぐ。
    ``runserver`` が JSON にしたあと、uvicorn が読み込むアプリファクトリ
    (``main.app_factory``)が形式を指定せずに呼び直しても JSON のままにするため。
    """
    log_format = log_format or current_log_format()
    dictConfig(build_logging_config(level, sql_echo=sql_echo, log_format=log_format))
    logging.getLogger("monitor_app").debug(
        "logging configured (level=%s, format=%s)", level, log_format
    )


def uvicorn_log_config(settings: AppSettings) -> dict[str, Any]:
    """``uvicorn.run(log_config=...)`` に渡す設定(uvicorn と monitor_app を同じ書式に)。

    ``MONITOR_LOG_FORMAT``(text / json)・``MONITOR_LOG_LEVEL``・
    ``MONITOR_ACCESS_LOG`` を反映する。
    """
    return build_logging_config(
        settings.log_level,
        log_format=settings.log_format,
        uvicorn=True,
        access_log=settings.access_log,
    )
