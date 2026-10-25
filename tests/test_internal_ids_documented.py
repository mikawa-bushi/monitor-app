"""コメントに書いた内部 ID(PERF-06・D3-01 など)が一覧の文書にあること(D7-06)。

ID の元になった点検の記録はリポジトリに無いため、docs/design/refactor_2_4_ids.md で
意味を引けるようにしている。コメントに新しい ID を書いたら、そこにも 1 行足す。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
IDS_DOC = ROOT / "docs" / "design" / "refactor_2_4_ids.md"
ID_RE = re.compile(r"\b((?:PERF|DX|CRIT|ARCH|DEPLOY|FE)-\d+|D[1-8]-\d+)\b")
#: 内部 ID を探すファイルの拡張子(コード・テンプレート・スクリプト)。
SUFFIXES = {".py", ".js", ".mjs", ".html", ".css", ".sh", ".ps1", ".bat", ".service"}


def _cited() -> dict[str, Path]:
    found: dict[str, Path] = {}
    for folder in ("monitor_app", "tests", "docker"):
        for path in sorted((ROOT / folder).rglob("*")):
            if path.suffix not in SUFFIXES or not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for found_id in ID_RE.findall(text):
                found.setdefault(found_id, path)
    return found


@pytest.mark.skipif(not IDS_DOC.is_file(), reason="docs/ の無い配布物(sdist)")
def test_every_internal_id_in_comments_is_listed():
    listed = set(re.findall(r"^\| (\S+) \|", IDS_DOC.read_text(encoding="utf-8"), re.M))
    missing = {
        found_id: str(path.relative_to(ROOT))
        for found_id, path in _cited().items()
        if found_id not in listed
    }
    assert missing == {}, f"docs/design/refactor_2_4_ids.md に無い ID: {missing}"
