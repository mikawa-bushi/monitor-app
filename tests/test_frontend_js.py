"""同梱 UI の JavaScript の単体テスト(Node.js の組み込みテストランナーで実行)。

``tests/js/*.test.mjs`` を ``node --test`` で走らせる。Node.js(18 以上)が無い環境では
スキップする(GitHub Actions の ubuntu / windows / macos ランナーには入っている)。
``MONITOR_TEST_NODE`` で node 実行ファイルを指定できる。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

JS_TESTS = Path(__file__).parent / "js"
STATIC_JS = Path(__file__).parent.parent / "monitor_app" / "web" / "static" / "js"


def _node() -> str | None:
    node = os.environ.get("MONITOR_TEST_NODE") or shutil.which("node")
    if not node:
        return None
    try:
        out = subprocess.run(
            [node, "--version"], capture_output=True, text=True, timeout=10
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    major = int(out.strip().lstrip("v").split(".")[0] or 0) if out else 0
    return node if major >= 18 else None


NODE = _node()
needs_node = pytest.mark.skipif(NODE is None, reason="Node.js 18+ が見つかりません")


@needs_node
def test_core_js_unit_tests():
    files = sorted(str(p) for p in JS_TESTS.glob("*.test.mjs"))
    assert files
    result = subprocess.run(
        [NODE, "--test", *files], capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stdout + result.stderr


@needs_node
@pytest.mark.parametrize(
    "script", sorted(p.name for p in STATIC_JS.glob("*.js") if ".min." not in p.name)
)
def test_static_js_has_no_syntax_errors(script):
    result = subprocess.run(
        [NODE, "--check", str(STATIC_JS / script)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
