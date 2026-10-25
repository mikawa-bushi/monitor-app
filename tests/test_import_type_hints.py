"""列名から str と推論した列が数値ばかりなら 1 度だけ警告する(DX-08)。

文字列として保存した数値は SQLite で MAX/MIN/ORDER BY/WHERE が文字列の比較になり、
集計が黙って誤る。型を明示する書き方(例: ``{"temp": "float"}``)を案内する。
"""

from __future__ import annotations

import logging

import pytest

from monitor_app import MonitorConfig, TableDef
from monitor_app.db.engine import Database
from monitor_app.db.registry import TableRegistry
from monitor_app.services.importer import CsvImporter, TailState

#: テストで作った DB(終わったらコネクションを閉じる)。
_DATABASES: list = []


@pytest.fixture(autouse=True)
def _propagate(monkeypatch):
    monkeypatch.setattr(logging.getLogger("monitor_app"), "propagate", True)
    yield
    while _DATABASES:
        _DATABASES.pop().dispose()


def _importer(tmp_path, columns):
    config = MonitorConfig(tables={"m": TableDef(columns=columns)})
    db = Database(f"sqlite:///{(tmp_path / 'app.db').as_posix()}")
    _DATABASES.append(db)
    registry = TableRegistry(config)
    registry.create_all(db)
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir(exist_ok=True)
    return CsvImporter(config, registry, db, csv_dir), csv_dir / "m.csv"


def _hints(messages):
    return [m for m in messages if "列名から str(文字列)と推論しましたが" in m]


def test_numeric_name_inferred_columns_are_reported_once(tmp_path, caplog):
    importer, path = _importer(tmp_path, ["id", "temp", "qty", "line", "amount"])
    path.write_text(
        "id,temp,qty,line,amount\n1,9.5,3,A,1\n2,10.2,100,B,2\n3,,7,C,3\n",
        encoding="utf-8",
    )
    result = importer.import_table("m", path)
    expected = [
        "m.temp: 列名から str(文字列)と推論しましたが、CSV の値はすべて数値です。"
        "数値として集計・並べ替え・比較するには config.py の columns で型を明示して"
        'ください(例: {"temp": "float"}。作成済みのテーブルの列の型は変わりません)',
        "m.qty: 列名から str(文字列)と推論しましたが、CSV の値はすべて数値です。"
        "数値として集計・並べ替え・比較するには config.py の columns で型を明示して"
        'ください(例: {"qty": "int"}。作成済みのテーブルの列の型は変わりません)',
    ]
    assert result.warnings == expected
    assert _hints(caplog.messages) == expected
    importer.import_table("m", path)  # 2 回目は出さない
    assert len(_hints(caplog.messages)) == 2


@pytest.mark.parametrize(
    "values",
    [
        ["007", "008"],  # 先頭ゼロ(コード)は数値とみなさない
        ["1", "x"],  # 数値でない値がある
        ["", " "],  # 空だけ
        ["1,000", "2,000"],  # 桁区切り
    ],
)
def test_text_like_columns_are_not_reported(tmp_path, caplog, values):
    importer, path = _importer(tmp_path, ["id", "code"])
    body = "".join(f'{i},"{v}"\n' for i, v in enumerate(values, start=1))
    path.write_text("id,code\n" + body, encoding="utf-8")
    assert _hints(importer.import_table("m", path).warnings) == []
    assert _hints(caplog.messages) == []


def test_explicit_types_are_not_reported(tmp_path, caplog):
    importer, path = _importer(tmp_path, {"id": "int", "temp": "str"})
    path.write_text("id,temp\n1,9.5\n2,10\n", encoding="utf-8")
    importer.import_table("m", path)
    assert _hints(caplog.messages) == []


def test_the_append_tailer_reports_too(tmp_path, caplog):
    importer, path = _importer(tmp_path, ["id", "temp"])
    path.write_text("id,temp\n1,9.5\n", encoding="utf-8")
    state = TailState()
    importer.tail_append("m", path, state)
    with path.open("a", encoding="utf-8") as fh:
        fh.write("2,10\n")
    importer.tail_append("m", path, state)
    assert len(_hints(caplog.messages)) == 1


def test_the_hint_survives_a_retried_replace(tmp_path, caplog):
    """有効行 0 件のテーブルを残すために取り消してやり直しても、ほかのテーブルの
    結果に型の推奨が残る。"""
    config = MonitorConfig(
        tables={
            "a": TableDef(columns=["id", "temp"]),
            "b": TableDef(columns=["id", "name"]),
        }
    )
    db = Database(f"sqlite:///{(tmp_path / 'app.db').as_posix()}")
    _DATABASES.append(db)
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()
    (csv_dir / "a.csv").write_text("id,temp\n1,9.5\n", encoding="utf-8")
    (csv_dir / "b.csv").write_text("id,name\n", encoding="utf-8")  # 有効行 0 件
    report = CsvImporter(config, TableRegistry(config), db, csv_dir).import_all()
    a = next(r for r in report.results if r.table == "a")
    assert a.inserted == 1 and len(_hints(a.warnings)) == 1
    assert len(_hints(caplog.messages)) == 1
