"""cli/_common.py を _errors.py(エラーの表示)・_signals.py(停止のシグナル)に分けても、
従来の import 先(``monitor_app.cli._common``)から同じものを使えること。"""

from __future__ import annotations

import subprocess
import sys

import pytest

from monitor_app.cli import _common, _errors, _signals

MOVED_TO_ERRORS = [
    "DEBUG_HINT",
    "MonitorGroup",
    "debug_enabled",
    "explain_error",
    "is_address_in_use",
    "make_streams_safe",
    "_echo_error",
    "_db_hint",
    "_db_detail",
    "_is_utf8",
    "_DB_DRIVER_HINT",
    "_DB_URL_HINT",
    "_SETTINGS_HINT",
]


@pytest.mark.parametrize("name", MOVED_TO_ERRORS)
def test_error_helpers_are_still_importable_from_common(name):
    assert getattr(_common, name) is getattr(_errors, name)


@pytest.mark.parametrize("name", ["StopRequested", "stop_signals"])
def test_signal_helpers_are_still_importable_from_common(name):
    assert getattr(_common, name) is getattr(_signals, name)


def test_the_app_still_uses_the_monitor_group():
    from monitor_app.cli import app

    assert app.info.cls is _common.MonitorGroup


def test_help_does_not_import_sqlalchemy():
    # エラー表示を分けても、--help で重いモジュールを読み込まない(起動を軽く保つ)
    code = (
        "import sys\n"
        "from monitor_app.cli import app\n"
        "print('sqlalchemy' in sys.modules)\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "False"
