"""未知の MONITOR_* の警告文: 「環境変数の X」・「<.env のパス> の X」(D5-07 の続き)。

場所が「環境変数」のとき、以前は「環境変数 の MONITOR_NAME」と余計な空白が入っていた。
"""

from __future__ import annotations

from monitor_app.checks import run_checks
from monitor_app.settings.declarative import MonitorConfig, TableDef
from monitor_app.settings.runtime import AppSettings

CONFIG = MonitorConfig(tables={"t": TableDef(columns=["id"], primary_key="id")})


def _messages(settings: AppSettings, env_files=None) -> list[str]:
    issues = run_checks(CONFIG, settings, env_files=env_files)
    return [issue.message for issue in issues if issue.code == "env-unknown-key"]


def test_environment_variables_have_no_space_before_the_particle(monkeypatch):
    monkeypatch.setenv("MONITOR_NAME", "line-3")
    assert _messages(AppSettings(_env_file=None)) == [
        "環境変数の MONITOR_NAME は monitor-app の設定ではありません"
    ]


def test_env_file_paths_keep_the_space(tmp_path):
    env = tmp_path / ".env"
    env.write_text("MONITOR_NAME=line-3\n", encoding="utf-8")
    assert _messages(AppSettings(_env_file=None), env_files=[env]) == [
        f"{env} の MONITOR_NAME は monitor-app の設定ではありません"
    ]
