"""ロギング設定。``print()`` を使わず、シンプルな時刻付きフォーマットで出力する。"""

from __future__ import annotations

import logging
from logging.config import dictConfig


def configure_logging(level: str = "INFO", sql_echo: bool = False) -> None:
    dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "simple": {
                    "format": "%(asctime)s %(levelname)s %(name)s: %(message)s",
                    "datefmt": "%Y-%m-%d %H:%M:%S",
                }
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "simple",
                }
            },
            "loggers": {
                "monitor_app": {
                    "handlers": ["console"],
                    "level": level.upper(),
                    "propagate": False,
                },
                "sqlalchemy.engine": {
                    "handlers": ["console"],
                    "level": "INFO" if sql_echo else "WARNING",
                    "propagate": False,
                },
            },
            "root": {"handlers": ["console"], "level": level.upper()},
        }
    )
    logging.getLogger("monitor_app").debug("logging configured (level=%s)", level)
