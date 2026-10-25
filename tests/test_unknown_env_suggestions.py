"""未知の MONITOR_* 変数への「〜の誤りではありませんか?」の提案が的外れにならないこと。

共通の接頭辞 ``MONITOR_`` まで含めて比べると、MONITOR_URL → MONITOR_PORT や
MONITOR_NAME → MONITOR_API_KEY のように無関係な変数にも提案し、後者は ``check`` で
「認証が有効になりません」のエラー(終了コード 1)になっていた。
"""

from __future__ import annotations

import pytest

from monitor_app.checks import run_checks
from monitor_app.settings.declarative import MonitorConfig, TableDef
from monitor_app.settings.runtime import AppSettings, find_unknown_env_keys


@pytest.mark.parametrize(
    ("key", "suggestion"),
    [
        ("MONITOR_DATABSE_URL", "MONITOR_DATABASE_URL"),
        ("MONITOR_API_KYE", "MONITOR_API_KEY"),
        ("MONITOR_APIKEY", "MONITOR_API_KEY"),
        ("MONITOR_LOGLEVEL", "MONITOR_LOG_LEVEL"),
        ("MONITOR_DB_URL", "MONITOR_DATABASE_URL"),
        ("MONITOR_PROTECT_READ", "MONITOR_PROTECT_READS"),
        ("MONITOR_CSVDIR", "MONITOR_CSV_DIR"),
        ("MONITOR_PROT", "MONITOR_PORT"),
    ],
)
def test_real_typos_are_still_suggested(key, suggestion):
    found = find_unknown_env_keys(environ={key: "x"}, env_files=[])
    assert [(f.key, f.suggestion) for f in found] == [(key, suggestion)]


@pytest.mark.parametrize(
    "key",
    ["MONITOR_URL", "MONITOR_TOKEN", "MONITOR_SECRET", "MONITOR_ENV", "MONITOR_NAME"],
)
def test_unrelated_variables_get_no_suggestion(key):
    found = find_unknown_env_keys(environ={key: "x"}, env_files=[])
    assert [(f.key, f.suggestion) for f in found] == [(key, None)]


def test_an_unrelated_variable_is_not_an_authentication_error(monkeypatch, tmp_path):
    monkeypatch.setenv("MONITOR_NAME", "line-3")
    config = MonitorConfig(tables={"t": TableDef(columns=["id"], primary_key="id")})
    issues = [
        issue
        for issue in run_checks(config, AppSettings(_env_file=None))
        if issue.code == "env-unknown-key"
    ]
    assert [issue.level for issue in issues] == ["warning"]
    assert "認証" not in issues[0].message
