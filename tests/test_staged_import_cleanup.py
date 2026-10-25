"""SQLite の置換の一時テーブル(``temp.<表>``)を、打ち切り・失敗の後にも残さない。

一時テーブルは本体と同じ名前のため、プールに戻った接続に残ると、名前を修飾しない SQL
(画面・API・次の取り込み)が本体の代わりに空の一時テーブルを読み書きする(停止の指示で
取り込みを打ち切ると、元の行が消えたように見えていた — 取り込みの一時テーブル(D4-06)と
停止の打ち切り(D4-01)の組み合わせ)。
"""

from __future__ import annotations

import threading

import pytest
from sqlalchemy import event, func, select, text

from monitor_app import MonitorConfig, TableDef
from monitor_app.cancel import TaskStopped, stop_scope
from monitor_app.db.engine import Database
from monitor_app.db.registry import TableRegistry
from monitor_app.services import importer as importer_mod
from monitor_app.services.importer import CsvImporter

CONFIG = MonitorConfig(
    tables={"items": TableDef(columns={"id": "int", "name": "str"}, primary_key="id")}
)


@pytest.fixture
def setup(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'app.db'}")
    registry = TableRegistry(CONFIG)
    registry.create_all(database)
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()
    path = csv_dir / "items.csv"
    path.write_text("id,name\n1,old\n2,old\n", encoding="utf-8")
    importer = CsvImporter(CONFIG, registry, database, csv_dir)
    importer.import_table("items", path)
    path.write_text(
        "id,name\n" + "".join(f"{i},new\n" for i in range(20_000)), encoding="utf-8"
    )
    yield database, registry, importer, path
    database.dispose()


def _state(database: Database, registry: TableRegistry) -> tuple[list, list[str]]:
    """プールの接続から見える、一時テーブルと(修飾しない名前の)表の中身。"""
    with database.connect() as conn:
        temps = conn.execute(text("SELECT name FROM temp.sqlite_master")).all()
        names = conn.execute(select(registry.get("items").c.name)).scalars().all()
    return temps, names


def test_a_stopped_replace_leaves_no_temp_table_behind(setup):
    database, registry, importer, path = setup
    stop = threading.Event()

    def stop_while_staging(_conn, _cursor, statement, *_args) -> None:
        if statement.startswith('INSERT INTO "temp"'):
            stop.set()  # 一時テーブルへの読み込みの途中で停止の指示

    event.listen(database.engine, "before_cursor_execute", stop_while_staging)
    try:
        with stop_scope(stop), pytest.raises(TaskStopped):
            importer.import_table("items", path)
    finally:
        event.remove(database.engine, "before_cursor_execute", stop_while_staging)
    assert stop.is_set()
    assert _state(database, registry) == ([], ["old", "old"])
    # 次の取り込みも本体へ書く
    importer.import_table("items", path)
    with database.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM main.items")).scalar() == 20_000


def test_a_temp_table_that_cannot_be_dropped_takes_the_connection_with_it(
    setup, monkeypatch
):
    database, registry, importer, path = setup
    calls = {"n": 0}
    original = importer_mod._drop_stage

    def fail_after_staging(conn, stage) -> None:
        calls["n"] += 1
        if calls["n"] > 1:  # 作る前の掃除は通し、後片付けだけ失敗させる
            raise OSError("disk I/O error")
        original(conn, stage)

    def boom(*_args, **_kwargs):
        raise RuntimeError("入れ替えの前に失敗")

    monkeypatch.setattr(importer_mod, "_drop_stage", fail_after_staging)
    monkeypatch.setattr(importer_mod, "_main_table", boom)
    with pytest.raises(RuntimeError, match="入れ替えの前に失敗"):
        importer.import_table("items", path)
    monkeypatch.setattr(importer_mod, "_drop_stage", original)
    # 消せなかった一時テーブルは接続ごと捨てられ、本体の行が見える
    assert _state(database, registry) == ([], ["old", "old"])
    with database.connect() as conn:
        assert (
            conn.execute(
                select(func.count()).select_from(registry.get("items"))
            ).scalar()
            == 2
        )
