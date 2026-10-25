"""CSV の取り込みを 1 行ずつ流す(PERF-04)ことの確認。

- 結果(DB の値・スキップ数・警告文・行番号)が従来の取り込み(``csv.DictReader`` +
  ``coerce()``)と同じ: 不正な値・セルの過不足・空行・重複した列名・引用符の中の改行
- ファイル全体をリストにせず、``INSERT_CHUNK`` 行ずつ書き込む
- 値がすべて空の行(Excel が書き出すカンマだけの行)はスキップして警告する(従来は
  採番された NULL の行になっていた)
"""

from __future__ import annotations

import csv
import types

import pytest
from sqlalchemy import text

import monitor_app.services.importer as importer_mod
from monitor_app import MonitorConfig, TableDef
from monitor_app.db.engine import Database
from monitor_app.db.registry import TableRegistry
from monitor_app.services.coercion import CoercionError, coerce
from monitor_app.services.importer import CsvImporter

COLUMNS = {
    "id": "int",
    "name": "str",
    "temp": "float",
    "ok": "bool",
    "day": "date",
    "note": "str",
}
LIST_COLUMNS = ["id", "name", "price", "line_id", "note"]

CASES = {
    "bad_values": (
        COLUMNS,
        "id,name,temp,ok,day,note\n"
        "1,a,1.5,true,2024-01-02,x\n"
        "2,b,abc,true,2024-01-02,x\n"
        "x,c,2.5,false,2024-01-03,y\n"
        "4,d,3.5,maybe,2024-01-04,z\n"
        "5,e,4.5,1,2024-13-01,w\n"
        "6,f, 5.5 , YES ,2024-02-29, sp \n",
    ),
    "short_and_long_rows": (
        COLUMNS,
        "id,name,temp,ok,day,note\n1,a\n2,b,1.0,true,2024-01-01,n,EXTRA,MORE\n3\n",
    ),
    "blank_lines": (
        COLUMNS,
        "id,name,temp,ok,day,note\n\n1,a,1,t,2024-01-01,x\n\n\n"
        "2,b,oops,t,2024-01-01,x\n3,c,3,f,2024-01-01,x\n",
    ),
    "duplicate_header": (
        COLUMNS,
        "id,name,temp,name,ok,day,note,temp\n"
        "1,first,1.0,second,true,2024-01-01,n,9.5\n"
        "2,first,2.0\n"
        "3,f,bad,s,true,2024-01-01,n,8.5\n",
    ),
    "quoted_newlines": (
        COLUMNS,
        'id,name,temp,ok,day,note\n1,"multi\nline",1.0,true,2024-01-01,"a ""q"" x"\n'
        '2,"comma, inside",2.0,false,2024-01-02,"crlf\r\ninside"\n'
        "3,z,bad,true,2024-01-03,n\n",
    ),
    "crlf": (
        COLUMNS,
        "id,name,temp,ok,day,note\r\n1,a,1.0,true,2024-01-01,x\r\n"
        "2,b,2.0,false,2024-01-02,y\r\n",
    ),
    "unknown_columns": (
        COLUMNS,
        "id,extra1,name,temp,ok,day,note,extra2\n1,zz,a,1.0,true,2024-01-01,x,yy\n",
    ),
    "values_only_in_some_columns": (
        COLUMNS,
        "id,name,temp,ok,day,note\n7,,,,,\n8, , , , , \n,x,,,,\n",
    ),
    "list_style": (
        LIST_COLUMNS,
        "id,name,price,line_id,note\n1,a,100,5,x\n2,b,1e3,6,\n3,c,bad,7,y\n4,d,5,x,z\n",
    ),
}


def _reference(columns, path, encoding="utf-8-sig"):
    """v2.3.1 の取り込み(DictReader で読み、行ごとに coerce)の結果。"""
    tdef = TableDef(columns=columns)
    allowed = set(tdef.column_names)
    rows, skipped, warnings = [], 0, []
    with path.open(newline="", encoding=encoding) as fh:
        for lineno, raw in enumerate(csv.DictReader(fh), start=2):
            record, failed = {}, False
            for key, value in raw.items():
                if key not in allowed:
                    continue
                try:
                    record[key] = coerce(value, tdef.column_type(key))
                except CoercionError as exc:
                    skipped += 1
                    warnings.append(f"{path.name} {lineno}行目: {exc}")
                    failed = True
                    break
            if not failed and record:
                rows.append(record)
    return rows, skipped, warnings


def _env(tmp_path, columns):
    config = MonitorConfig(tables={"t": TableDef(columns=columns)})
    db = Database(f"sqlite:///{(tmp_path / 'app.db').as_posix()}")
    registry = TableRegistry(config)
    registry.create_all(db)
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir(exist_ok=True)
    return config, registry, db, csv_dir


def _table_rows(db, columns):
    names = list(columns)
    sel = ", ".join(f"{c}, typeof({c})" for c in names)
    with db.connect() as conn:
        return conn.execute(text(f"SELECT {sel} FROM t ORDER BY rowid")).all()


@pytest.mark.parametrize("case", list(CASES))
def test_results_match_the_dictreader_importer(tmp_path, case):
    columns, body = CASES[case]
    config, registry, db, csv_dir = _env(tmp_path, columns)
    path = csv_dir / "t.csv"
    path.write_text(body, encoding="utf-8", newline="")
    importer = CsvImporter(config, registry, db, csv_dir, encoding="utf-8-sig")

    expected = _reference(columns, path)
    assert importer.read_rows("t", path) == expected

    result = importer.import_table("t", path, keep=True)
    assert (result.inserted, result.skipped) == (len(expected[0]), expected[1])
    assert result.warnings == expected[2]

    # DB に入った値と SQLite の型も、従来どおり 1 行ずつ INSERT した場合と同じ
    ref_db = Database(f"sqlite:///{(tmp_path / 'ref.db').as_posix()}")
    registry.create_all(ref_db)
    with ref_db.connect() as conn:
        for row in expected[0]:
            conn.execute(registry.get("t").insert(), row)
    assert _table_rows(db, columns) == _table_rows(ref_db, columns)
    db.dispose()
    ref_db.dispose()


def test_all_empty_rows_are_skipped_with_one_warning(tmp_path):
    config, registry, db, csv_dir = _env(tmp_path, COLUMNS)
    path = csv_dir / "t.csv"
    path.write_text(
        "id,name,temp,ok,day,note\n1,a,1,t,2024-01-01,x\n,,,,,\n , ,, , ,\n"
        "2,b,2,f,2024-01-02,y\n,,,,,\n",
        encoding="utf-8",
    )
    importer = CsvImporter(config, registry, db, csv_dir)
    result = importer.import_table("t", path, keep=False)
    assert (result.inserted, result.skipped) == (2, 3)
    assert result.warnings == [
        "t.csv: 値がすべて空の行を 3 行スキップしました(3, 4, 6 行目)"
    ]
    with db.connect() as conn:
        assert conn.execute(text("SELECT id FROM t ORDER BY id")).scalars().all() == [
            1,
            2,
        ]
    rows, skipped, warnings = importer.read_rows("t", path)
    assert [r["id"] for r in rows] == [1, 2]
    assert skipped == 3 and len(warnings) == 1
    db.dispose()


def test_many_blank_rows_are_listed_briefly(tmp_path):
    config, registry, db, csv_dir = _env(tmp_path, COLUMNS)
    path = csv_dir / "t.csv"
    path.write_text("id,name,temp,ok,day,note\n1,a,1,t,2024-01-01,x\n" + ",,,,,\n" * 25)
    result = CsvImporter(config, registry, db, csv_dir).import_table("t", path)
    assert result.warnings == [
        "t.csv: 値がすべて空の行を 25 行スキップしました"
        "(3, 4, 5, 6, 7, 8, 9, 10, 11, 12 行目ほか 15 行)"
    ]
    db.dispose()


def test_rows_are_streamed_in_insert_chunks(tmp_path, monkeypatch):
    """ファイル全体をリストにせず、``INSERT_CHUNK`` 行ずつ書き込む。"""
    config, registry, db, csv_dir = _env(tmp_path, COLUMNS)
    path = csv_dir / "t.csv"
    path.write_text(
        "id,name,temp,ok,day,note\n"
        + "".join(f"{i},n{i},{i}.5,true,2024-01-01,\n" for i in range(1, 12)),
        encoding="utf-8",
    )
    monkeypatch.setattr(importer_mod, "INSERT_CHUNK", 4)
    seen = []
    real = importer_mod.insert_rows

    def spy(conn, table, rows, **kwargs):
        seen.append((type(rows), kwargs["chunk"]))
        return real(conn, table, rows, **kwargs)

    monkeypatch.setattr(importer_mod, "insert_rows", spy)
    report = CsvImporter(config, registry, db, csv_dir).import_all()
    assert report.total_inserted == 11
    assert seen == [(types.GeneratorType, 4)]
    db.dispose()


def test_insert_chunk_still_controls_insert_in_chunks(tmp_path, monkeypatch):
    _config, registry, db, _csv_dir = _env(tmp_path, COLUMNS)
    monkeypatch.setattr(importer_mod, "INSERT_CHUNK", 2)
    calls = []
    real = importer_mod.insert_rows
    monkeypatch.setattr(
        importer_mod,
        "insert_rows",
        lambda conn, table, rows, **kw: calls.append(kw)
        or real(conn, table, rows, **kw),
    )
    rows = [{"id": i, "name": f"r{i}"} for i in range(5)]
    with db.connect() as conn:
        assert importer_mod.insert_in_chunks(conn, registry.get("t"), rows) == 5
    assert calls == [{"on_conflict": "error", "chunk": 2}]
    db.dispose()


def test_warning_line_numbers_count_records_like_dictreader(tmp_path):
    """行番号は(空行を除く)レコードの番号 + 1。引用符の中の改行は 1 行に数える。"""
    config, registry, db, csv_dir = _env(tmp_path, COLUMNS)
    path = csv_dir / "t.csv"
    path.write_text(
        'id,name,temp,ok,day,note\n\n1,"a\nb",1,t,2024-01-01,x\n\nzz,b,2,t,2024-01-01,y\n',
        encoding="utf-8",
    )
    result = CsvImporter(config, registry, db, csv_dir).import_table("t", path)
    assert result.warnings == ["t.csv 3行目: 'zz' を int に変換できません"]
    db.dispose()


def test_count_rows_and_append_since_keep_their_semantics(tmp_path):
    config, registry, db, csv_dir = _env(tmp_path, COLUMNS)
    path = csv_dir / "t.csv"
    path.write_text(
        "id,name,temp,ok,day,note\n1,a,1,t,2024-01-01,x\n2,b,bad,t,2024-01-01,x\n"
        "3,c,3,t,2024-01-01,x\n",
        encoding="utf-8",
    )
    importer = CsvImporter(config, registry, db, csv_dir)
    assert importer.count_rows("t", path) == 2
    assert importer.append_since("t", path, start_row=0) == (2, 2)
    # 既読分を超えた行だけ。縮んだら先頭から(主キーが既にある行は読み飛ばす)
    with path.open("a", encoding="utf-8") as fh:
        fh.write("4,d,4,t,2024-01-01,x\n")
    assert importer.append_since("t", path, start_row=2) == (1, 3)
    assert importer.append_since("t", path, start_row=10) == (0, 3)
    with db.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM t")).scalar() == 3
    db.dispose()


def test_empty_file_and_header_only_file_keep_rows(tmp_path):
    config, registry, db, csv_dir = _env(tmp_path, COLUMNS)
    path = csv_dir / "t.csv"
    path.write_text(
        "id,name,temp,ok,day,note\n1,a,1,t,2024-01-01,x\n", encoding="utf-8"
    )
    importer = CsvImporter(config, registry, db, csv_dir)
    assert importer.import_table("t", path).inserted == 1
    for body in ("", "id,name,temp,ok,day,note\n", "id,name,temp,ok,day,note\n,,,,,\n"):
        path.write_text(body, encoding="utf-8")
        result = importer.import_table("t", path)
        assert result.inserted == 0
        assert (
            result.warnings[-1]
            == "t.csv: 有効行 0 件のため置換を中止(既存データを保持)"
        )
        with db.connect() as conn:
            assert conn.execute(text("SELECT COUNT(*) FROM t")).scalar() == 1
    db.dispose()
