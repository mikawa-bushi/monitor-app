"""同梱の設定・README・設定ページが使うセルスタイルのクラスは、すべて app.css が定義している。

Bootstrap の CSS を同梱しなくなったので、app.css にないクラスを書くと何も起きない。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_CSS = ROOT / "monitor_app" / "web" / "static" / "css" / "app.css"
SETTINGS_JS = ROOT / "monitor_app" / "web" / "static" / "js" / "settings.js"
CLASS_VALUE = re.compile(r'(?:css_class=|"class":\s*)"([^"]+)"')


def used_classes() -> dict[str, str]:
    """クラス名 → 最初に見つけたファイル。"""
    sources = [
        p
        for p in (ROOT / "monitor_app").rglob("*.py")
        if "Threshold" in p.read_text(encoding="utf-8")
    ]
    sources += [ROOT / "README.md", ROOT / "README_ja.md"]
    used: dict[str, str] = {}
    for path in sources:
        for value in CLASS_VALUE.findall(path.read_text(encoding="utf-8")):
            for name in value.split():
                used.setdefault(name, str(path.relative_to(ROOT)))

    choices = (
        SETTINGS_JS.read_text(encoding="utf-8")
        .split("const STYLE_CLASSES = [", 1)[1]
        .split("];", 1)[0]
    )
    for value in re.findall(r'\["([^"]*)",', choices):
        for name in value.split():
            used.setdefault(name, "settings.js STYLE_CLASSES")
    return used


def test_the_scan_finds_the_classes_we_know_are_used():
    assert {
        "bg-danger",
        "text-white",
        "text-warning",
        "bg-info",
        "text-dark",
        "text-success",
    } <= used_classes().keys()


def test_every_shipped_cell_style_class_is_defined_in_app_css():
    defined = set(
        re.findall(r"\.([a-z][a-z0-9-]*)", APP_CSS.read_text(encoding="utf-8"))
    )
    missing = {
        name: where for name, where in used_classes().items() if name not in defined
    }
    assert missing == {}
