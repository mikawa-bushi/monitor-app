"""``python -m monitor_app`` で CLI を起動する(PATH に monitor-app が無い環境向け)。"""

from .cli import app

if __name__ == "__main__":  # pragma: no cover
    app(prog_name="monitor-app")
