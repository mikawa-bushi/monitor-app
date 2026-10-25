"""CSV からの設定の推論(``monitor_app.settings.infer``、#DX-09 / #DX-08 / #CRIT-12)。"""

from __future__ import annotations

import datetime as dt
import os
import warnings
from pathlib import Path

import pytest
from contract import helpers

from monitor_app.checks import run_checks
from monitor_app.settings import ConfigWarning
from monitor_app.settings import infer as infer_mod
from monitor_app.settings.infer import (
    CsvReadError,
    InferError,
    classify_value,
    infer_project,
    quote_identifier,
    render_config_py,
    scan_csv,
)
from monitor_app.settings.loader import load_config_with_warnings
from monitor_app.settings.runtime import AppSettings


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    helpers.isolate_from_host(monkeypatch, tmp_path)


def _csv(path: Path, text: str, encoding: str = "utf-8") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode(encoding))
    return path


def _types(path: Path, **kwargs) -> dict[str, str]:
    return {name: col.type for name, col in scan_csv(path, **kwargs).columns.items()}


def _tolerant(value: str, logical: str) -> bool:
    """日時の取り込み(B1 の寛容な変換)を想定した「取り込める」判定。"""
    return True


# ---------------------------------------------------------------------------
# 値・列の型
# ---------------------------------------------------------------------------
def test_column_types_come_from_the_data(tmp_path):
    path = _csv(
        tmp_path / "m.csv",
        "id,code,qty,temp,ok,flag,day,ts,name,mixed,empty\n"
        "1,0001,3,9.5,true,0,2026-10-21,2026-10-21T08:00:00,a,1,\n"
        "2,0002,100,10.25,FALSE,1,2026-10-22,2026-10-21 08:05,b,x,\n"
        "3,0010,-4,1e3,yes,1,2026-10-23,2026-10-21T08:10:00Z,c,2,\n",
    )
    assert _types(path) == {
        "id": "int",
        "code": "str",  # 先頭の 0 を残す(品番・郵便番号)
        "qty": "int",
        "temp": "float",
        "ok": "bool",  # true / false / yes / no(大文字小文字を問わない)
        "flag": "int",  # 0/1 は int のまま
        "day": "date",
        "ts": "datetime" if _importable_datetime("2026-10-21T08:10:00Z") else "str",
        "name": "str",
        "mixed": "str",
        "empty": "str",  # 値が 1 つも無い列は str
    }


def _importable_datetime(value: str) -> bool:
    from monitor_app.services.coercion import CoercionError, coerce

    try:
        coerce(value, "datetime")
    except CoercionError:
        return False
    return True


@pytest.mark.parametrize(
    "value, expected",
    [
        ("0", {"int", "float", "boolish"}),
        ("007", set()),
        ("-0.5", {"float"}),
        ("00.5", set()),
        (".5", {"float"}),
        ("1.", {"float"}),
        ("1e400", set()),  # 無限大になる値は数値にしない
        ("9007199254740991", {"int", "float"}),
        ("9007199254740993", set()),  # JavaScript で桁が落ちる長い番号は str
        ("１２３", set()),  # 全角数字は str のまま
        ("TRUE", {"bool", "boolish"}),
        ("on", {"boolish"}),
        ("2026/2/29", set()),  # 存在しない日付
        ("2026/2/28", {"date", "datetime"}),
        ("2026-10-21 24:00", set()),
        ("2026/10/21 8:05", {"datetime"}),
        ("2026-10-21T08:05:06.123+09:00", {"datetime"}),
        ("21.10.2026", set()),
    ],
)
def test_classify_value(value, expected):
    names = {
        infer_mod._INT: "int",
        infer_mod._FLOAT: "float",
        infer_mod._BOOL: "bool",
        infer_mod._BOOLISH: "boolish",
        infer_mod._DATE: "date",
        infer_mod._DATETIME: "datetime",
    }
    bits = classify_value(value)
    assert {name for bit, name in names.items() if bits & bit} == expected


def test_one_bad_value_anywhere_in_a_big_file_makes_the_column_str(tmp_path):
    rows = [f"{i},{i * 1.5},{i}" for i in range(1, 20_001)]
    rows[15_000] = "15001,n/a,0015001"  # まとめて判定するチャンクの途中に混ぜる
    path = _csv(tmp_path / "big.csv", "id,value,code\n" + "\n".join(rows) + "\n")
    scan = scan_csv(path)
    assert {n: c.type for n, c in scan.columns.items()} == {
        "id": "int",
        "value": "str",
        "code": "str",
    }
    assert scan.columns["value"].counterexample("float") == (15002, "n/a")
    assert scan.columns["code"].counterexample("int") == (15002, "0015001")
    assert scan.rows == 20_000 and scan.id_problem is None


def test_dates_need_the_importer_to_accept_the_format(tmp_path):
    """取り込みで変換できない日時の形式は str にする(行を捨てさせない)。"""
    path = _csv(
        tmp_path / "jp.csv",
        "日時,値\n2026/10/21 8:00,1\n2026/10/21 10:00,2\n",
    )
    assert _types(path, importable=_tolerant)["日時"] == "datetime"
    assert _types(path, importable=lambda v, t: False)["日時"] == "str"


def test_datetime_column_accepts_date_only_values(tmp_path):
    path = _csv(
        tmp_path / "d.csv",
        "ts\n2026-10-21\n2026-10-21 08:00\n2026/10/22\n2026/10/22 9:30:15\n",
    )
    assert _types(path, importable=_tolerant) == {"ts": "datetime"}


def test_excel_jp_cp932_with_japanese_headers(tmp_path):
    lines = ["日時,ライン,生産数,不良数,温度(℃),品番"]
    for i in range(30):
        t = dt.datetime(2026, 10, 21, 8) + dt.timedelta(minutes=5 * i)
        lines.append(
            f"{t.year}/{t.month}/{t.day} {t.hour}:{t.minute:02d},第{1 + i % 3}ライン,"
            f"{90 + i},{i % 4},{20 + i / 10:.1f},{i % 3 + 1:05d}"
        )
    path = _csv(tmp_path / "生産実績.csv", "\r\n".join(lines) + "\r\n", "cp932")
    project = infer_project([path], importable=_tolerant)
    (item,) = project.files
    assert (item.table, item.encoding, item.rows) == ("生産実績", "cp932", 30)
    assert item.columns == {
        "_row_id": "int",
        "日時": "datetime",
        "ライン": "str",
        "生産数": "int",
        "不良数": "int",
        "温度(℃)": "float",
        "品番": "str",
    }
    assert (item.primary_key, item.synthetic_pk) == ("_row_id", True)
    assert (item.chart_x, item.chart_y) == ("日時", ["生産数", "不良数", "温度(℃)"])
    view = project.config.views["生産実績_view"]
    assert view.query == 'SELECT * FROM "生産実績"'
    assert view.chart.x == "日時"


def test_utf8_with_bom_and_encoding_fallback_order(tmp_path):
    path = _csv(tmp_path / "s.csv", "\ufeffid,名前\n1,山田\n")
    scan = scan_csv(path)
    assert scan.encoding == "utf-8-sig" and list(scan.columns) == ["id", "名前"]
    cp = _csv(tmp_path / "c.csv", "id,名前\n1,山田\n", "cp932")
    assert scan_csv(cp).encoding == "cp932"
    with pytest.raises(CsvReadError, match="文字コードを判別できません"):
        scan_csv(cp, ("utf-8-sig",))
    utf16 = tmp_path / "u.csv"
    utf16.write_bytes("id\n1\n".encode("utf-16"))
    with pytest.raises(CsvReadError, match="UTF-16"):
        scan_csv(utf16)


# ---------------------------------------------------------------------------
# 主キー
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "body, pk, problem",
    [
        ("ID,v\n1,a\n2,b\n", "ID", None),
        ("id,v\n1,a\n1,b\n", "_row_id", "重複"),
        ("id,v\n1,a\n,b\n", "_row_id", "空欄"),
        ("id,v\nA1,a\nA2,b\n", "_row_id", "整数ではない"),
        ("v,w\na,1\n", "_row_id", None),
        ("_row_id,v\nx,1\n", "_row_id_2", None),  # 名前が使われていれば別名
    ],
)
def test_primary_key_rules(tmp_path, body, pk, problem):
    (item,) = infer_project([_csv(tmp_path / "t.csv", body)]).files
    assert item.primary_key == pk
    assert item.synthetic_pk == (pk != "ID")
    if problem:
        assert any(problem in note for note in item.notes), item.notes
    if item.synthetic_pk:
        assert next(iter(item.columns)) == pk and item.columns[pk] == "int"


def test_large_files_are_sampled_and_do_not_trust_the_id(tmp_path, monkeypatch):
    monkeypatch.setattr(infer_mod, "FULL_SCAN_MAX_BYTES", 100)
    monkeypatch.setattr(infer_mod, "SAMPLE_ROWS", 50)
    rows = "\n".join(f"{i},{i}" for i in range(1, 200)) + "\n200,later-text\n"
    path = _csv(tmp_path / "big.csv", "id,v\n" + rows)
    scan = scan_csv(path)
    assert scan.sampled and scan.rows == 50
    assert scan.columns["v"].type == "int"  # 先頭 50 行だけで判定した
    assert "先頭 50 行" in " ".join(scan.notes)
    (item,) = infer_project([path]).files
    assert item.synthetic_pk  # 全行で重複を確かめていない id は主キーにしない


# ---------------------------------------------------------------------------
# 見出し・行
# ---------------------------------------------------------------------------
def test_header_cleanup_and_dictreader_semantics(tmp_path):
    path = _csv(
        tmp_path / "h.csv",
        " a ,b,,a,B,c\n"
        "1,x,ignored,10,X,2026-10-21\n"
        "\n"  # 空行は飛ばす
        ",,,,,\n"  # すべて空欄の行は数えない
        "2,y\n",  # 足りない値は空欄
    )
    scan = scan_csv(path)
    assert list(scan.columns) == ["a", "b", "c"]
    assert scan.columns["a"].index == 3  # 重複した見出しは後ろの列(DictReader と同じ)
    assert scan.rows == 2
    notes = " ".join(scan.notes)
    assert "見出し 'a' が重複" in notes
    assert "'B' は 'b' と大文字・小文字だけが違う" in notes
    assert "見出しが空の列(1 列)" in notes
    assert scan.columns["a"].type == "int"


def test_header_that_looks_like_data_is_noted(tmp_path):
    scan = scan_csv(_csv(tmp_path / "n.csv", "1,2\n3,4\n"))
    assert any("見出しではなくデータ" in note for note in scan.notes)


@pytest.mark.parametrize("body", ["", "\n", ",,\n1,2\n"])
def test_files_without_a_header_are_rejected(tmp_path, body):
    with pytest.raises(CsvReadError):
        scan_csv(_csv(tmp_path / "e.csv", body))


# ---------------------------------------------------------------------------
# ファイル・テーブル名
# ---------------------------------------------------------------------------
def test_unsafe_and_duplicate_names_are_skipped_with_a_note(tmp_path):
    d = tmp_path / "csv"
    for name in (
        "ok.csv",
        "it's.csv",
        "a,b.csv",
        " lead.csv",
        "sqlite_x.csv",
        "_audit.csv",
    ):
        _csv(d / name, "v\n1\n")
    _csv(d / "notes.txt", "v\n1\n")
    project = infer_project([d])
    assert [f.table for f in project.files] == ["ok"]
    assert len(project.notes) == 5, project.notes


def test_uppercase_extension_needs_a_rename_on_case_sensitive_fs(tmp_path):
    d = tmp_path / "csv"
    path = _csv(d / "DATA.CSV", "v\n1\n")
    if (d / "DATA.csv").exists():
        pytest.skip("大文字小文字を区別しないファイルシステム")
    _csv(d / "ok.csv", "v\n1\n")
    project = infer_project([path.parent])
    assert [f.table for f in project.files] == ["ok"]
    assert "DATA.csv に名前を変えてください" in " ".join(project.notes)


def test_paths_must_share_one_folder_and_exist(tmp_path):
    a = _csv(tmp_path / "a" / "x.csv", "v\n1\n")
    b = _csv(tmp_path / "b" / "y.csv", "v\n1\n")
    with pytest.raises(InferError, match="1 つのフォルダ"):
        infer_project([a, b])
    with pytest.raises(InferError, match="見つかりません"):
        infer_project([tmp_path / "missing.csv"])
    (tmp_path / "empty").mkdir()
    with pytest.raises(InferError, match="CSV ファイルが見つかりません"):
        infer_project([tmp_path / "empty"])
    _csv(tmp_path / "bad" / "x y.csv", "")  # 見出しの無い CSV しかない
    with pytest.raises(InferError, match="取り込める CSV がありません"):
        infer_project([tmp_path / "bad"])


def test_files_from_one_folder_select_only_those_tables(tmp_path):
    d = tmp_path / "data"
    a = _csv(d / "a.csv", "v\n1\n")
    _csv(d / "b.csv", "v\n1\n")
    project = infer_project([a])
    assert list(project.config.tables) == ["a"]
    assert project.csv_dir == d.resolve()


# ---------------------------------------------------------------------------
# 生成する設定
# ---------------------------------------------------------------------------
def test_config_shape_views_charts_kpis_and_title(tmp_path):
    d = tmp_path / "line1" / "csv"
    _csv(d / "orders.csv", "id,user_id,qty,price\n1,10,2,9.5\n2,11,3,8.5\n")
    _csv(d / "order.csv", "label,n1,n2,n3,n4,n5\na,1,2,3,4,5\n")
    config = infer_project([d]).config
    assert config.app_title == "line1"  # ./csv ならその親フォルダの名前
    assert config.dashboard.columns == 2
    orders = config.views["orders_view"]
    assert orders.chart.x == "id" and orders.chart.y == ["qty", "price"]  # *_id は除く
    order = config.views["order_view"]
    assert order.query == 'SELECT * FROM "order"'  # 予約語は引用符で囲む
    assert order.chart.y == ["n1", "n2", "n3", "n4"]  # 系列は 4 本まで
    assert config.kpis["orders_count"].query == "SELECT COUNT(*) FROM orders"
    assert config.kpis["orders_count"].link_view == "orders_view"


def test_large_tables_get_a_bounded_tail_view(tmp_path):
    body = "id,v\n" + "\n".join(f"{i},{i}" for i in range(1, 12)) + "\n"
    path = _csv(tmp_path / "t.csv", body)
    assert infer_project([path], tail_rows=20).config.views["t_view"].query == (
        "SELECT * FROM t"
    )
    query = infer_project([path], tail_rows=5).config.views["t_view"].query
    assert query == (
        "SELECT * FROM (SELECT * FROM t ORDER BY id DESC LIMIT 5) AS _tail ORDER BY id"
    )
    assert (
        "LIMIT 20"
        in infer_project([path], tail_rows=20, bound_all_views=True)
        .config.views["t_view"]
        .query
    )


def test_quote_identifier():
    assert quote_identifier("users") == "users"
    assert quote_identifier("Users") == '"Users"'
    assert quote_identifier('a"b') == '"a""b"'
    assert quote_identifier("温度(℃)") == '"温度(℃)"'


def test_render_round_trip_passes_check(tmp_path, monkeypatch):
    d = tmp_path / "csv"
    _csv(
        d / "生産実績.csv",
        "日時,ライン,温度(℃)\r\n2026/10/21 8:00,第1ライン,20.5\r\n",
        "cp932",
    )
    _csv(d / "quotes.csv", 'id,say"hi",it\'s\n1,a,b\n')
    _csv(d / "big.csv", "id,v\n" + "\n".join(f"{i},{i}.5" for i in range(1, 30)))
    project = infer_project([d], tail_rows=10)
    source = render_config_py(project)
    compile(source, "config.py", "exec")
    path = tmp_path / "config.py"
    path.write_text(source, encoding="utf-8")
    with warnings.catch_warnings():
        warnings.simplefilter("error", ConfigWarning)
        loaded, found = load_config_with_warnings(path)
    assert found == []
    assert loaded.model_dump() == project.config.model_dump()
    assert all(isinstance(t.columns, dict) for t in loaded.tables.values())

    monkeypatch.chdir(tmp_path)
    settings = AppSettings(
        _env_file=None, database_url="sqlite://", csv_dir=d, csv_encoding="auto"
    )
    issues = run_checks(loaded, settings, data=True)
    assert [i for i in issues if i.level != "info"] == [], issues


def test_render_handles_windows_paths_in_comments(tmp_path):
    path = _csv(tmp_path / "t.csv", "v\n1\n")
    project = infer_project([path])
    project.csv_dir = Path("C:\\Users\\taro\\new\\data")  # \U \n を含む
    compile(render_config_py(project), "config.py", "exec")


# ---------------------------------------------------------------------------
# まとめて判定(チャンク)と 1 値ずつの判定が同じ結果になる
# ---------------------------------------------------------------------------
_VALUE_POOL = [
    "0",
    "1",
    "12",
    "-7",
    "+3",
    "007",
    "1.5",
    ".5",
    "1e3",
    "1e400",
    "abc",
    "",
    " 42 ",
    "TRUE",
    "no",
    "y",
    "off",
    "2026-10-21",
    "2026/1/5",
    "2026-02-30",
    "2026-10/21",
    "2026-10-21 08:00",
    "2026/10/21 8:05:09",
    "2026-10-21T08:00:00Z",
    "2026-10-21 24:00",
    "0000-01-01",
    "9007199254740993",
    "１２",
]


def _reference_mask(values: list[str], importable) -> int:
    """1 値ずつ classify_value を畳み込む素朴な実装(チャンク判定の検証用)。"""
    mask, shapes = infer_mod._ALL, {}
    for raw in values:
        value = raw.strip()
        if not value:
            continue
        bits = classify_value(value)
        if bits & (infer_mod._DATE | infer_mod._DATETIME):
            bits &= infer_mod._shape_bits(value, bits, shapes, importable)
        mask &= bits
    return mask


@pytest.mark.parametrize("chunk", [1, 2, 3, 7, 4096])
@pytest.mark.parametrize("importable", [_tolerant, None], ids=["tolerant", "coerce"])
def test_chunked_scan_matches_value_by_value(tmp_path, monkeypatch, chunk, importable):
    import csv as csv_mod
    import random

    monkeypatch.setattr(infer_mod, "_CHUNK_ROWS", chunk)
    importable = importable or infer_mod._default_importable()
    rng = random.Random(chunk)
    families = [
        ["0", "1", "12", "-7", "+3"],
        ["1.5", ".5", "12", "0"],
        ["TRUE", "no", "false", "yes"],
        ["y", "off", "1", "0"],
        ["2026-10-21", "2026/1/5", "2026-1-5"],
        ["2026-10-21", "2026-10-21 08:00", "2026/10/21 8:05:09"],
        ["2026-10-21T08:00:00Z", "2026-10-21 08:00"],
    ]
    columns = []
    for family in families:
        for _variant in range(3):
            values = [rng.choice([*family, ""]) for _ in range(40)]
            if rng.random() < 0.6:  # 途中に 1 つだけ外れ値を混ぜる
                values[rng.randrange(len(values))] = rng.choice(_VALUE_POOL)
            columns.append(values)
    path = tmp_path / "eq.csv"
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv_mod.writer(fh)
        writer.writerow([f"c{i}" for i in range(len(columns))])
        for row in zip(*columns):
            writer.writerow(row)
    scan = scan_csv(path, importable=importable)
    for i, values in enumerate(columns):
        col = scan.columns[f"c{i}"]
        assert col.mask == _reference_mask(values, importable), (i, values)
        for bit, (line, value) in col.examples.items():
            assert not classify_value(value) & bit or not importable(value, "datetime")
            assert columns[i][line - 2].strip() == value  # 行番号が正しい


def test_later_chunks_use_the_fast_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(infer_mod, "_CHUNK_ROWS", 2)
    path = _csv(
        tmp_path / "f.csv",
        "d,dt,b,f\n"
        "2026-10-21,2026-10-21 08:00,true,1.5\n"
        "2026-10-22,2026-10-21 08:05,no,2\n"
        "2026/10/23,2026/10/21 8:10,TRUE,3.25\n"
        "2026/10/24,2026/10/21 8:15:30,yes,4\n"
        "2026-02-30,2026-10-21T09:00:00,false,5\n",
    )
    scan = scan_csv(path, importable=_tolerant)
    types = {name: col.type for name, col in scan.columns.items()}
    assert types == {"d": "str", "dt": "datetime", "b": "bool", "f": "float"}
    assert scan.columns["d"].counterexample("date") == (6, "2026-02-30")


def test_private_use_characters_are_allowed_in_names(tmp_path):
    """外字(私用領域の文字)を含む見出し・ファイル名も使える(制御文字だけを弾く)。"""
    gaiji = "\ue000"
    path = _csv(tmp_path / f"品{gaiji}.csv", f"名前{gaiji},値\nA,1\n")
    (item,) = infer_project([path]).files
    assert item.table == f"品{gaiji}" and f"名前{gaiji}" in item.columns
    scan = scan_csv(_csv(tmp_path / "c.csv", '"a\nb",c\n1,2\n'))
    assert list(scan.columns) == ["c"]  # 改行を含む見出しは取り込まない
    assert any("制御文字" in note for note in scan.notes)


@pytest.mark.parametrize("chunk", [1, 2, 4096])
@pytest.mark.parametrize(
    "ids, problem",
    [
        ([1, 2, 3, 10], None),
        ([3, 1, 2, 10], None),  # 順不同でも重複が無ければ主キーにする
        ([1, 2, 3, 2], "id 列の値 2 が重複しているため"),
        ([5, 1, 5], "id 列の値 5 が重複しているため"),
        ([1, 1], "id 列の値 1 が重複しているため"),
    ],
)
def test_id_uniqueness_across_chunks(tmp_path, monkeypatch, chunk, ids, problem):
    monkeypatch.setattr(infer_mod, "_CHUNK_ROWS", chunk)
    body = "id,v\n" + "".join(f"{i},x\n" for i in ids)
    scan = scan_csv(_csv(tmp_path / "t.csv", body))
    assert scan.id_problem == problem


def test_id_values_too_large_for_int64_are_not_a_key(tmp_path):
    scan = scan_csv(_csv(tmp_path / "t.csv", "id\n1\n1234567890123456789012\n"))
    assert scan.id_problem is not None
    (item,) = infer_project([tmp_path / "t.csv"]).files
    assert item.synthetic_pk


# ---------------------------------------------------------------------------
# レビューで見つかった問題の回帰テスト
# ---------------------------------------------------------------------------
def test_names_that_break_sql_parameters_or_links_are_skipped(tmp_path):
    """``x :y`` は SQL の :y がパラメータになりビューが動かない。# ? % は画面のリンクを壊す。

    Excel が開いている間の ``~$`` ファイルと隠しファイルも表にしない。
    """
    d = tmp_path / "csv"
    names = ["ok.csv", "a#b.csv", "a%b.csv", "~$ok.csv", ".hidden.csv"]
    if os.name != "nt":
        # Windows ではファイル名に使えない(: は代替データストリームの区切りになり、
        # "x :y.csv" と書くとファイル "x " のストリームになる)
        names += ["x :y.csv", "a?b.csv"]
    for name in names:
        _csv(d / name, "id,v\n1,2\n")
    project = infer_project([d])
    assert [f.table for f in project.files] == ["ok"]
    notes = "\n".join(project.notes)
    assert len(project.notes) == len(names) - 1, notes
    assert "使えない文字(:)" in notes and "使えない文字(#)" in notes
    assert "~$ok.csv: Excel などがファイルを開いている間" in notes
    assert ".hidden.csv: 隠しファイル" in notes


def test_symlinked_csv_keeps_the_name_shown_in_the_folder(tmp_path):
    """取り込みは <フォルダ>/<テーブル名>.csv を読むため、リンク先の名前を使わない。"""
    from monitor_app.cli.data import run_csv_import

    target = _csv(tmp_path / "share" / "plc_export_2026.csv", "id,temp\n1,20.5\n")
    d = tmp_path / "csv"
    d.mkdir()
    try:
        os.symlink(target, d / "line1.csv")
    except (OSError, NotImplementedError):
        pytest.skip("シンボリックリンクを作れない環境")
    for args in ([d], [d / "line1.csv"]):
        project = infer_project(args)
        assert project.csv_dir == d
        assert [(f.table, f.path) for f in project.files] == [
            ("line1", d / "line1.csv")
        ]
    settings = AppSettings(
        _env_file=None, database_url=f"sqlite:///{tmp_path / 'x.db'}", csv_dir=d
    )
    report = run_csv_import(settings, project.config)
    assert [(r.table, r.inserted) for r in report.results] == [("line1", 1)]


@pytest.mark.parametrize(
    "body, label",
    [("id\tname\n1\ta\n", "タブ"), ("id;name\n1;a\n", "セミコロン")],
)
def test_non_comma_delimiters_are_rejected_with_a_hint(tmp_path, body, label):
    with pytest.raises(CsvReadError, match=f"区切り文字が{label}"):
        scan_csv(_csv(tmp_path / "x.csv", body))


def test_header_only_file_keeps_id_as_the_integer_key(tmp_path):
    project = infer_project([_csv(tmp_path / "csv" / "log.csv", "id,temp,line\n")])
    (item,) = project.files
    assert item.columns == {"id": "int", "temp": "str", "line": "str"}
    assert (item.primary_key, item.synthetic_pk, item.rows) == ("id", False, 0)
    assert any("データ行が無い" in note for note in item.notes)
    assert not any("整数ではない" in note for note in item.notes)


def test_sampled_files_show_the_row_count_as_a_lower_bound(tmp_path, monkeypatch):
    monkeypatch.setattr(infer_mod, "FULL_SCAN_MAX_BYTES", 100)
    monkeypatch.setattr(infer_mod, "SAMPLE_ROWS", 50)
    rows = "\n".join(f"{i},{i}" for i in range(1, 200)) + "\n"
    project = infer_project([_csv(tmp_path / "big.csv", "id,v\n" + rows)])
    (item,) = project.files
    assert item.rows_label == "50 行以上"
    assert "UTF-8・50 行以上" in render_config_py(project)
    small = infer_project([_csv(tmp_path / "s" / "small.csv", "id,v\n1,2\n")])
    assert small.files[0].rows_label == "1 行"


def test_dashboard_shows_only_the_first_charts_of_a_big_folder(tmp_path):
    """トップページは全グラフのデータをまとめて取得するため、件数を絞る。"""
    d = tmp_path / "csv"
    for i in range(infer_mod.MAX_DASHBOARD_CHARTS + 2):
        _csv(d / f"t{i:02d}.csv", "id,v\n1,2\n2,3\n")
    project = infer_project([d])
    names = [f"t{i:02d}_view" for i in range(infer_mod.MAX_DASHBOARD_CHARTS)]
    assert project.config.dashboard.views == names
    assert any("トップページには最初の" in note for note in project.notes)
    path = tmp_path / "config.py"
    path.write_text(render_config_py(project), encoding="utf-8")
    config, found = load_config_with_warnings(path)
    assert found == [] and config.model_dump() == project.config.model_dump()
    few = infer_project([_csv(tmp_path / "few" / "a.csv", "id,v\n1,2\n")])
    assert few.config.dashboard.views == []  # 少なければグラフ付きの全ビュー
