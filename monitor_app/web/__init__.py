"""同梱 Web UI(テンプレート・静的ファイル)と、その配信部品(:mod:`.serving`)。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path, PurePosixPath

#: 同梱の静的ファイル(``/static/...`` で配信する)のフォルダ。
STATIC_DIR = Path(__file__).with_name("static")


@lru_cache(maxsize=64)
def static_asset_path(path: str) -> str:
    """設定の静的ファイルのパスを、実際に同梱されているファイルのパスにする(表示用)。

    ``static/<path>`` が無く、``static/img/<ファイル名>`` があればそちらを返す。v1 の
    ``FAVICON_PATH`` の既定値 ``'favicon.ico'`` は、v2 では ``img/favicon.ico`` にある
    ため(読み込んだ設定値そのものは変えない)。どちらも無ければ ``path`` のまま。
    """
    try:
        if (STATIC_DIR / path).is_file():
            return path
        fallback = PurePosixPath("img", PurePosixPath(path.replace("\\", "/")).name)
        if fallback.as_posix() != path and (STATIC_DIR / fallback).is_file():
            return fallback.as_posix()
    except (OSError, ValueError):  # 不正な文字を含むパスなど
        pass
    return path
