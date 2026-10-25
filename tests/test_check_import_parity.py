"""``check --data`` は取り込みと同じ処理で CSV を調べる(D7-01, D7-02, D7-03)。

以前の点検は見出しの対応・値の読み方を別に実装していたため、BOM 付きの見出しを
「列が無い」と誤って警告したり、前後に空白のある見出し(`` qty ``)の列の値を調べずに
OK と言ったりしていた(取り込みは行を落としていた)。同じ CSV で点検と取り込みを
実行し、報告が一致することを確かめる。
"""

from __future__ import annotations

import re

import pytest

from monitor_app import MonitorConfig, TableDef
from monitor_app.checks import run_checks
from monitor_app.db.engine import Database
from monitor_app.db.registry import TableRegistry
from monitor_app.exceptions import ConflictError
from monitor_app.services.importer import CsvImporter
from monitor_app.settings.infer import infer_project
from monitor_app.settings.runtime import AppSettings


def _run(tmp_path, table: dict, body: bytes, **settings):
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()
    (csv_dir / "items.csv").write_bytes(body)
    config = MonitorConfig(tables={"items": TableDef(**table)})
    values = {
        "_env_file": None,
        "csv_dir": str(csv_dir),
        "database_url": f"sqlite:///{tmp_path / 'x.db'}",
        **settings,
    }
    s = AppSettings(**values)
    issues = [i for i in run_checks(config, s, data=True) if i.code.startswith("csv")]
    db = Database(f"sqlite:///{tmp_path / 'x.db'}")
    registry = TableRegistry(config)
    try:
        importer = CsvImporter(
            config,
            registry,
            db,
            csv_dir,
            s.csv_encoding,
            encoding_fallback=s.csv_encoding_fallback,
        )
        result = importer.import_all().results[0]
    finally:
        db.dispose()
    return issues, result


def _columns(issues, code: str) -> set[str]:
    found = set()
    for issue in issues:
        if issue.code == code:
            match = re.search(r"列 '([^']*)'", issue.message)
            if match:
                found.add(match.group(1))
    return found


CASES = {
    "bom-utf8-pk-first": (
        {"columns": {"id": "int", "name": "str"}, "primary_key": "id"},
        "﻿id,name\n1,a\n2,b\n".encode(),
        {"csv_encoding": "utf-8"},
    ),
    "bom-utf8-name-first": (
        {"columns": {"name": "str", "id": "int"}, "primary_key": "id"},
        "﻿name,id\na,1\nb,2\n".encode(),
        {"csv_encoding": "utf-8"},
    ),
    "padded-header-bad-value": (
        {"columns": {"id": "int", "qty": "int"}, "primary_key": "id"},
        b"id, qty \n1,10\n2,abc\n3,30\n",
        {},
    ),
    "case-and-width-variants": (
        {"columns": {"id": "int", "name": "str", "temp": "float"}},
        "ＩＤ,Name,TEMP\n1,a,1.5\n2,b,x\n".encode(),
        {},
    ),
    "duplicate-header-cells": (
        {"columns": {"id": "int", "name": "str"}},
        b"id,name,Name,extra\n1,a,b,c\n",
        {},
    ),
    "missing-and-unknown": (
        {"columns": {"id": "int", "name": "str", "email": "str"}},
        b"id,nmae,,other\n1,a,,x\n",
        {},
    ),
}


@pytest.mark.parametrize("case", list(CASES))
def test_check_reports_what_the_import_does(tmp_path, case):
    table, body, settings = CASES[case]
    issues, result = _run(tmp_path, table, body, **settings)
    pk = table.get("primary_key", "id")
    # 見出しの対応: 無い列・取り込まない列・表記ゆれの数が取り込みと同じ
    assert _columns(issues, "csv-header-missing") == set(result.missing)
    assert _columns(issues, "csv-header-unknown") == {
        re.sub(r"[﻿]", r"\\ufeff", u) for u in result.unknown if u.strip()
    }
    variants = [i for i in issues if i.code == "csv-header-variant"]
    assert len(variants) == len(result.renamed)
    # 取り込みで読み飛ばす行がある ⇔ 点検が変換できない値を報告する
    failing = [i for i in issues if i.code == "csv-type-mismatch"]
    skipped_lines = {
        int(m.group(1))
        for w in result.warnings
        if (m := re.match(r"items\.csv (\d+)行目", w))
    }
    assert bool(failing) == bool(skipped_lines)
    for issue in failing:
        line = int(re.search(r"例: (\d+) 行目", issue.message).group(1))
        assert line in skipped_lines
    # 誤った「列が無い(置き換えを中止)」警告を出さない
    if not result.refused:
        assert not [
            i
            for i in issues
            if i.code == "csv-header-missing"
            and i.level == "warning"
            and pk not in i.message
        ]


def test_bom_header_is_not_reported_as_missing(tmp_path):
    issues, result = _run(
        tmp_path, *CASES["bom-utf8-name-first"][:2], csv_encoding="utf-8"
    )
    assert result.inserted == 2 and not result.missing
    assert [i.code for i in issues] == ["csv-header-variant"]
    assert "'\\ufeffname' を列 'name'" in issues[0].message


def test_padded_header_values_are_checked_like_the_import(tmp_path):
    issues, result = _run(tmp_path, *CASES["padded-header-bad-value"][:2])
    assert (result.inserted, result.skipped) == (2, 1)
    (mismatch,) = [i for i in issues if i.code == "csv-type-mismatch"]
    assert "'qty'" in mismatch.message and "3 行目 'abc'" in mismatch.message


@pytest.mark.parametrize(
    ("values", "kind"),
    [
        (["007", "008"], None),  # 先頭が 0 の番号は数値にしない
        (["1.", "2."], "float"),
        (["1e3", "2"], "float"),
        (["9901234567894123", "9901234567894124"], None),  # 2**53 を超える番号
        (["12", "-3"], "int"),
    ],
)
def test_numeric_advice_agrees_between_import_check_and_serve(tmp_path, values, kind):
    body = (
        "id,code\n" + "".join(f"{i},{v}\n" for i, v in enumerate(values, 1))
    ).encode()
    issues, result = _run(
        tmp_path, {"columns": ["id", "code"], "primary_key": "id"}, body
    )
    # 取り込みの型の推奨(DX-08)
    hint = [w for w in result.warnings if w.startswith("items.code:")]
    import_kind = re.search(r'"code": "(\w+)"', hint[0]).group(1) if hint else None
    # check --data の csv-numeric-as-str
    numeric = [i for i in issues if i.code == "csv-numeric-as-str"]
    check_kind = (
        re.search(r'"code": "(\w+)"', numeric[0].hint).group(1) if numeric else None
    )
    # serve の推論
    inferred = infer_project([tmp_path / "csv" / "items.csv"]).files[0].columns["code"]
    serve_kind = inferred if inferred in ("int", "float") else None
    assert import_kind == check_kind == serve_kind == kind


def test_duplicate_primary_keys_are_an_error_in_check_and_named_by_the_import(tmp_path):
    body = b"id,name\n1,a\n2,b\n1,c\n"
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()
    (csv_dir / "items.csv").write_bytes(body)
    config = MonitorConfig(
        tables={"items": TableDef(columns={"id": "int", "name": "str"})}
    )
    settings = AppSettings(
        _env_file=None,
        csv_dir=str(csv_dir),
        database_url=f"sqlite:///{tmp_path / 'x.db'}",
    )
    (dup,) = [
        i
        for i in run_checks(config, settings, data=True)
        if i.code == "csv-duplicate-key"
    ]
    assert dup.level == "error"
    assert "items.csv の 4 行目の主キー id=1 が 2 行目と重複" in dup.message
    db = Database(settings.database_url)
    try:
        with pytest.raises(ConflictError) as info:
            CsvImporter(config, TableRegistry(config), db, csv_dir).import_all()
    finally:
        db.dispose()
    assert info.value.message.startswith(
        "items.csv 4行目: 主キー id=1 が 2行目と重複しています。取り込みは取り消しました"
    )


def test_check_reports_values_that_server_databases_reject(tmp_path):
    # n(主キー・外部キー以外の整数列)は新しく作る表では BIGINT のため入るが、主キーの
    # id は INTEGER(32 ビット)のまま
    body = (
        "id,note,n\n1,ok,1\n2,"
        + "x" * 300
        + ",2\n3,ok,1729500000000\n2147483648,ok,4\n"
    ).encode()
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()
    (csv_dir / "items.csv").write_bytes(body)
    config = MonitorConfig(
        tables={"items": TableDef(columns={"id": "int", "note": "str", "n": "int"})}
    )

    def limit_issues(url: str):
        settings = AppSettings(_env_file=None, csv_dir=str(csv_dir), database_url=url)
        return [
            i
            for i in run_checks(config, settings, data=True)
            if i.code == "csv-db-limit"
        ]

    assert limit_issues(f"sqlite:///{tmp_path / 'x.db'}") == []  # SQLite は入る
    found = limit_issues("postgresql+pg8000://u:p@127.0.0.1:1/db")  # 接続はしない
    messages = sorted(i.message for i in found)
    assert len(found) == 2
    assert messages[0].startswith("items.csv 3 行目: 列 note の値")
    assert "300 文字" in messages[0] and "PostgreSQL" in messages[0]
    assert messages[1].startswith("items.csv 5 行目: 列 id の値 2147483648")
