"""公開契約のベースライン(tests/contract/baseline/*.json)を現行コードから生成する。

使い方(リポジトリ直下で)::

    uv run python tests/contract/_make_baseline.py            # 全部
    uv run python tests/contract/_make_baseline.py openapi cli  # 一部だけ

ベースラインは「既存の利用者との約束」の記録で、tests/test_contract.py は現行コードが
これを満たし続けるか(追加は可、削除・変更は不可)を検証する。**再生成してよいのは、
互換性を壊す変更が明示的に合意されたときだけ。** リファクタの途中で「テストを通すため」
に再生成してはならない。再生成したら差分をレビューし、CHANGELOG に記載すること。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parents[1]
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))  # `contract` パッケージを解決できるようにする

from contract import snapshot  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "names",
        nargs="*",
        choices=snapshot.BASELINE_NAMES,
        help="再生成するベースライン(省略時は全部)",
    )
    args = parser.parse_args(argv)
    snapshot.BASELINE_DIR.mkdir(parents=True, exist_ok=True)
    for name in args.names or snapshot.BASELINE_NAMES:
        path = snapshot.BASELINE_DIR / f"{name}.json"
        path.write_text(
            snapshot.dump_json(snapshot.build_baseline(name)),
            encoding="utf-8",
            newline="\n",
        )
        print(f"書き出しました: {path.relative_to(_TESTS_DIR.parent)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
