"""組み込みのデモ(物流センターの在庫画面)。CLI の ``demo`` と ``startproject --template
warehouse`` だけが使う内部パッケージ(``monitor_app`` からは再公開しない)。

- :func:`write_project` … デモのプロジェクト(config.py・README.md・csv/)をフォルダに
  書く。マスタの CSV はパッケージの固定ファイル、ロットと台帳(過去 7 日分)はその場で
  生成する(日付は ``now`` からの相対なので、いつ書いても「今日」が埋まっている)。
- :class:`WarehouseSimulator` … 起動後に入出庫を発生させ続けるバックグラウンドタスク。
"""

from __future__ import annotations

import importlib.resources as resources
import shutil
from datetime import datetime
from pathlib import Path

from .simulation import WarehouseSimulator, write_history_csv

__all__ = ["SOURCE", "WarehouseSimulator", "write_project"]

#: パッケージに入っているデモのファイル(config.py・README.md・csv/ のマスタ)。
SOURCE = Path(str(resources.files("monitor_app") / "demo" / "warehouse"))


def write_project(dest: Path, *, now: datetime | None = None) -> None:
    """``dest`` にデモのプロジェクトを書く(既存の同名のファイルは上書きする)。

    書くもの: config.py・README.md・csv/items.csv・csv/locations.csv(コピー)と
    csv/lots.csv・csv/movements.csv(``now`` までの 7 日分を生成)。
    """
    (dest / "csv").mkdir(parents=True, exist_ok=True)
    for name in ("config.py", "README.md"):
        shutil.copyfile(SOURCE / name, dest / name)
    for csv_file in (SOURCE / "csv").glob("*.csv"):
        shutil.copyfile(csv_file, dest / "csv" / csv_file.name)
    write_history_csv(dest / "csv", now or datetime.now())
