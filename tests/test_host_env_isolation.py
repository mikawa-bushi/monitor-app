"""テストが開発者の環境(シェルの MONITOR_*・作業フォルダの .env)に左右されないこと。

配備の手順どおり ``MONITOR_API_KEY`` を export したシェルや、リポジトリ直下に .env を
置いたままの状態で ``pytest`` を実行しても、テストが失敗しないことを確かめる
(conftest の ``_no_host_monitor_env``)。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

TESTS = Path(__file__).parent
#: API キーや文字コードの設定に左右されやすいテスト(認証なしの書き込み・ETag など)。
_SENSITIVE = ("test_api.py", "test_view_payload.py", "test_ingest_on_conflict.py")


def test_suite_ignores_host_monitor_variables_and_dotenv(tmp_path):
    (tmp_path / ".env").write_text(
        "MONITOR_API_KEY=from-dotenv\nMONITOR_PROTECT_READS=true\n", encoding="utf-8"
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("MONITOR_")}
    env.update(
        MONITOR_API_KEY="from-shell",
        MONITOR_CSV_ENCODING="cp932",
        MONITOR_LOG_LEVEL="DEBUG",
        PYTHONUTF8="1",
    )
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-p",
            "no:pytest_cov",
            *(str(TESTS / name) for name in _SENSITIVE),
        ],
        cwd=tmp_path,  # 作業フォルダの .env を読ませようとする
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-2000:]
    assert " passed" in proc.stdout


def test_each_test_starts_without_monitor_variables_in_an_empty_folder():
    # MONITOR_TEST_*(サーバー DB・ブラウザのテストを有効にする変数)だけは残す。
    assert not [
        key
        for key in os.environ
        if key.upper().startswith("MONITOR_")
        and not key.upper().startswith("MONITOR_TEST_")
    ]
    assert list(Path.cwd().iterdir()) == []
