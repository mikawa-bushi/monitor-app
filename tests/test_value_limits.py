"""サーバー DB の列の制限(services/value_limits — D3-06)と、DB のエラーの説明。

制限そのものは接続せずに確かめる(実際の PostgreSQL / MySQL での動作は
tests/server_db/test_import_server_limits.py)。
"""

from __future__ import annotations

import pytest
from sqlalchemy import BigInteger, Integer, SmallInteger, String, Text
from sqlalchemy.exc import DataError

import monitor_app.db.bulk as bulk_mod
import monitor_app.services.importer as importer_mod
from monitor_app import MonitorConfig, TableDef
from monitor_app.db.engine import Database
from monitor_app.db.registry import TableRegistry
from monitor_app.exceptions import InvalidPayloadError
from monitor_app.services.coercion import CoercionError
from monitor_app.services.importer import CsvImporter, _KeyTracker
from monitor_app.services.value_limits import declared_limits, value_limits


def test_limits_come_from_the_sql_types():
    limits = value_limits(
        {
            "s": String(255),
            "short": String(10),
            "t": Text(),
            "i": Integer(),
            "b": BigInteger(),
            "small": SmallInteger(),
        },
        "postgresql",
    )
    assert limits.lengths == {"s": 255, "short": 10}
    assert limits.ranges == {
        "i": (-(2**31), 2**31 - 1),
        "b": (-(2**63), 2**63 - 1),
        "small": (-(2**15), 2**15 - 1),
    }
    assert value_limits({"s": String(255)}, "sqlite") is None  # SQLite は強制しない
    assert value_limits({"t": Text()}, "mysql") is None


def test_rows_over_the_limits_raise_a_coercion_error():
    limits = value_limits({"note": String(5), "n": Integer()}, "mysql")
    assert limits({"note": "abc", "n": 7, "other": "x" * 999}) == {
        "note": "abc",
        "n": 7,
        "other": "x" * 999,
    }
    with pytest.raises(CoercionError, match="6 文字。MySQL のこの列は 5 文字まで"):
        limits({"note": "abcdef", "n": 1})
    with pytest.raises(CoercionError, match="値 2147483648 は MySQL"):
        limits({"note": None, "n": 2**31})
    assert limits({"n": True}) == {"n": True}  # bool は整数の範囲を調べない
    assert limits.violation("note", "x" * 100).count("x") == 40  # 長い値は省く


def test_declared_limits_need_no_connection():
    config = MonitorConfig(tables={"t": TableDef(columns={"id": "int", "s": "str"})})
    table = TableRegistry(config).get("t")
    pg = declared_limits(table, "postgresql+psycopg://u:p@127.0.0.1:1/db")
    assert pg.lengths == {"s": 255} and pg.ranges["id"] == (-(2**31), 2**31 - 1)
    assert declared_limits(table, "sqlite:///x.db") is None
    assert declared_limits(table, "not a url") is None


def test_key_tracker_finds_duplicates_in_any_order():
    tracker = _KeyTracker()
    seen = [tracker.first_line(k, n) for n, k in enumerate([1, 2, 5, 3, 7, 3], 2)]
    assert seen == [None, None, None, None, None, 5]
    tracker = _KeyTracker()
    assert [tracker.first_line(k, n) for n, k in enumerate(["b", "a", "b"], 2)] == [
        None,
        None,
        2,
    ]
    tracker = _KeyTracker()
    assert [tracker.first_line(k, n) for n, k in enumerate([1, 9, 2, 9], 2)] == [
        None,
        None,
        None,
        3,
    ]


class _PgLike(Exception):
    def __init__(self, message: str, sqlstate: str) -> None:
        super().__init__(message)
        self.sqlstate = sqlstate


def test_a_value_the_database_rejects_names_the_file_and_the_rows(
    tmp_path, monkeypatch
):
    """DB が受け付けない値(制限で見分けられなかったもの)は、ファイル名と書き込んでいた
    行の範囲を示し、何も変えない(D3-06)。"""
    config = MonitorConfig(tables={"t": TableDef(columns={"id": "int", "v": "str"})})
    db = Database(f"sqlite:///{tmp_path / 'x.db'}")
    registry = TableRegistry(config)
    registry.create_all(db)
    (tmp_path / "t.csv").write_text(
        "id,v\n" + "".join(f"{i},x\n" for i in range(1, 8)) + "\n8,x\n"
    )
    monkeypatch.setattr(importer_mod, "INSERT_CHUNK", 3)
    real = bulk_mod._Writer.insert_plain
    calls = []

    def reject_the_second_chunk(self, rows):
        calls.append(len(rows))
        if len(calls) == 2:  # 2 回目の書き込み(4〜6 件目の行)を DB が受け付けない
            raise DataError("INSERT", {}, _PgLike("value too long", "22001"))
        return real(self, rows)

    monkeypatch.setattr(bulk_mod._Writer, "insert_plain", reject_the_second_chunk)
    try:
        with pytest.raises(InvalidPayloadError) as info:
            CsvImporter(config, registry, db, tmp_path).import_all()
    finally:
        db.dispose()
    assert info.value.message == (
        "t.csv 5〜7行目のどれか: 文字列が列の最大長を超えています(str 列は 255 文字まで)"
        "。取り込みは取り消しました(データは変わっていません)"
    )
