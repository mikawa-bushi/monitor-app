"""NUL で埋まった CSV の末尾・CSV として読めない行(D3-09)。

電源断の後の SD カード・FAT では、ファイルの末尾が NUL(0x00)で埋まることがある。
Python 3.10 の csv は NUL で ``csv.Error`` を送出し、追記監視が以後ずっと止まって
いた(3.11 以降は NUL がセルに入り、その行が型変換で落ちていた)。NUL は取り除き、
それでも CSV として読めない行(長すぎるセルなど)はその行だけを読み飛ばす。
"""

from __future__ import annotations

import csv
import logging

import pytest
from contract import helpers
from sqlalchemy import text
from typer.testing import CliRunner

from monitor_app import MonitorConfig, TableDef
from monitor_app.cli import app as cli_app
from monitor_app.db.engine import Database
from monitor_app.db.registry import TableRegistry
from monitor_app.exceptions import CsvFormatError
from monitor_app.services.importer import CsvImporter, TailState

COLUMNS = {"id": "int", "v": "float"}


@pytest.fixture(autouse=True)
def _propagate(monkeypatch):
    monkeypatch.setattr(logging.getLogger("monitor_app"), "propagate", True)


@pytest.fixture
def env(tmp_path):
    config = MonitorConfig(tables={"m": TableDef(columns=COLUMNS)})
    db = Database(f"sqlite:///{(tmp_path / 'app.db').as_posix()}")
    registry = TableRegistry(config)
    registry.create_all(db)
    importer = CsvImporter(config, registry, db, tmp_path)
    yield importer, tmp_path / "m.csv", db
    db.dispose()


def _ids(db) -> list[int]:
    with db.connect() as conn:
        return [r[0] for r in conn.execute(text("SELECT id FROM m ORDER BY id"))]


def test_nul_padding_does_not_stop_the_tailer(env):
    importer, path, db = env
    path.write_bytes(b"id,v\n1,1.0\n")
    state = importer.init_tail("m", path)
    with path.open("ab") as fh:
        fh.write(b"\x00" * 512)  # 電源断で末尾が 0 埋め
        fh.write(b"3,3.0\n")  # 再起動後もロガーは続きに書く
    assert importer.tail_append("m", path, state) == 1
    with path.open("ab") as fh:
        fh.write(b"4,4.0\n")
    assert importer.tail_append("m", path, state) == 1
    assert _ids(db) == [3, 4]


def test_nul_padding_in_the_middle_is_ignored_by_the_full_import(env):
    importer, path, db = env
    path.write_bytes(b"id,v\n1,1.0\n" + b"\x00" * 300 + b"2,2.0\n3,3.0\n")
    result = importer.import_table("m", path)
    assert (result.inserted, result.skipped, result.warnings) == (3, 0, [])
    assert _ids(db) == [1, 2, 3]


def test_init_tail_survives_nul_inside_quotes(env):
    importer, path, _db = env
    path.write_bytes(b'id,v\n1,"1.0"\n' + b"\x00" * 64 + b'2,"2.0"\n')
    state = importer.init_tail("m", path)
    assert state.offset == path.stat().st_size and state.records == 2


def test_an_oversized_field_skips_only_that_row(env, caplog):
    importer, path, db = env
    huge = "x" * (csv.field_size_limit() + 10)
    path.write_text(f'id,v\n1,1.0\n2,"{huge}"\n3,3.0\n')
    result = importer.import_table("m", path)
    assert (result.inserted, result.skipped) == (2, 1)
    assert result.warnings[0].startswith("m.csv 3行目: CSV として読めないため")
    assert _ids(db) == [1, 3]
    # 追記監視も止まらない
    db_rows = _ids(db)
    state = TailState()
    path.write_text(f'id,v\n4,4.0\n5,"{huge}"\n6,6.0\n')
    importer.tail_append("m", path, state)
    assert _ids(db) == sorted({*db_rows, 4, 6})
    assert any("m.csv 3行目: CSV として読めないため" in m for m in caplog.messages)


def test_long_values_are_shortened_in_warnings(env):
    importer, path, _db = env
    path.write_text("id,v\n1," + "9x" * 2000 + "\n")
    result = importer.import_table("m", path, keep=True)
    (warning,) = result.warnings
    assert len(warning) < 300
    assert warning.startswith("m.csv 2行目: '9x9x") and warning.endswith(
        "を float に変換できません"
    )


def test_an_unreadable_header_is_a_short_cli_error(tmp_path, monkeypatch):
    project = helpers.start_project(tmp_path)
    monkeypatch.chdir(project)
    huge = "x" * (csv.field_size_limit() + 10)
    (project / "csv" / "users.csv").write_text(f'"{huge}",name\n1,a\n')
    result = CliRunner().invoke(cli_app, ["import-csv"])
    assert result.exit_code == 1
    assert "users.csv の見出し(1 行目)を CSV として読めません" in result.output
    assert "Traceback" not in result.output


def test_an_unreadable_header_raises_csv_format_error(env):
    importer, path, _db = env
    path.write_text('"' + "x" * (csv.field_size_limit() + 10) + '",v\n1,1.0\n')
    with pytest.raises(CsvFormatError) as info:
        importer.import_table("m", path)
    assert info.value.hint and info.value.code == "CSV_FORMAT_ERROR"
