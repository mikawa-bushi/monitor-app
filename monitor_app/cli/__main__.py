"""``python -m monitor_app.cli`` で CLI を起動する(単一モジュール時代の起動方法)。

プログラム名は指定せず、従来どおり使い方の表示を ``python -m monitor_app.cli`` にする。
"""

from . import app

if __name__ == "__main__":  # pragma: no cover
    app()
