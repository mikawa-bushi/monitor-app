"""追記監視の読み取り位置(バイト位置)からの取り込み(PERF-05, PERF-06)。

- 完全な行(引用符の外の改行で終わる行)だけを取り込み、書きかけの行は持ち越す
- BOM はファイルの先頭にしかない(utf-8-sig)/ cp932 / auto の判別
- 引用符の中の改行が書き込みの途中で切れても、閉じるまで待つ
- ファイルの縮小・差し替え(ローテーション)では先頭から読み直してヘッダも読み直す。
  エディタが別ファイルとして保存し直しても、内容が続いていれば続きから読む
- 監視の開始時は既存の行を取り込まない(末尾の完全な行の直後から)
"""

from __future__ import annotations

import logging
import os

import pytest
from sqlalchemy import text

import monitor_app.services.importer as importer_mod
from monitor_app import MonitorConfig, TableDef
from monitor_app.db.engine import Database
from monitor_app.db.registry import TableRegistry
from monitor_app.services.importer import CsvImporter, TailState
from monitor_app.services.ingest_watcher import IngestWatcher

COLUMNS = {"id": "int", "line": "str", "note": "str", "temp": "float"}


@pytest.fixture(autouse=True)
def _propagate(monkeypatch):
    monkeypatch.setattr(logging.getLogger("monitor_app"), "propagate", True)


@pytest.fixture
def env(tmp_path):
    config = MonitorConfig(tables={"r": TableDef(columns=COLUMNS)})
    db = Database(f"sqlite:///{(tmp_path / 'app.db').as_posix()}")
    registry = TableRegistry(config)
    registry.create_all(db)
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()
    yield config, registry, db, csv_dir
    db.dispose()


def _importer(env, **kwargs) -> CsvImporter:
    config, registry, db, csv_dir = env
    return CsvImporter(config, registry, db, csv_dir, **kwargs)


def _append(path, data: bytes) -> None:
    with path.open("ab") as fh:
        fh.write(data)


def _rows(env):
    db = env[2]
    with db.connect() as conn:
        return [tuple(r) for r in conn.execute(text("SELECT * FROM r ORDER BY id"))]


@pytest.mark.parametrize("encoding", ["utf-8-sig", "cp932", "auto"])
def test_partial_records_and_quoted_newlines_are_held_back(env, encoding):
    # utf-8-sig の CSV は先頭にだけ BOM がある(追記分は BOM なしの UTF-8)
    file_encoding = "utf-8" if encoding == "utf-8-sig" else "cp932"
    bom = b"\xef\xbb\xbf" if encoding == "utf-8-sig" else b""
    importer = _importer(env, encoding=encoding)
    path = env[3] / "r.csv"
    path.write_bytes(
        bom + "id,line,note,temp\r\n1,第1ライン,ok,1.5\r\n".encode(file_encoding)
    )
    state = TailState()
    assert importer.tail_append("r", path, state) == 1
    assert state.header == ["id", "line", "note", "temp"]  # BOM は除く

    _append(path, '2,第2ライン,"警告: 温度上昇\n(要確認・ソ表能)'.encode(file_encoding))
    assert importer.tail_append("r", path, state) == 0  # 引用符が閉じていない
    _append(path, '",98'.encode(file_encoding))
    assert importer.tail_append("r", path, state) == 0  # 改行がまだ無い
    _append(path, ".7\r\n3,第3ライン,停止".encode(file_encoding))
    assert importer.tail_append("r", path, state) == 1  # 3 行目は書きかけ
    _append(path, "中,30\r\n".encode(file_encoding))
    assert importer.tail_append("r", path, state) == 1
    assert importer.tail_append("r", path, state) == 0  # 変化なし
    assert _rows(env) == [
        (1, "第1ライン", "ok", 1.5),
        (2, "第2ライン", "警告: 温度上昇\n(要確認・ソ表能)", 98.7),
        (3, "第3ライン", "停止中", 30.0),
    ]
    assert state.records == 3
    assert state.offset == path.stat().st_size


def test_crlf_split_between_writes(env):
    importer = _importer(env)
    path = env[3] / "r.csv"
    path.write_bytes(b"id,line,note,temp\r\n1,a,b,1\r")
    state = TailState()
    assert importer.tail_append("r", path, state) == 1
    _append(path, b"\n2,a,b,2\r\n")
    assert importer.tail_append("r", path, state) == 1
    assert [r[0] for r in _rows(env)] == [1, 2]


def test_bom_only_at_the_start_and_utf8_continuation(env):
    importer = _importer(env, encoding="utf-8-sig")
    path = env[3] / "r.csv"
    path.write_bytes("\ufeffid,line,note,temp\n".encode("utf-8"))
    state = TailState()
    assert importer.tail_append("r", path, state) == 0
    assert state.header == ["id", "line", "note", "temp"]
    # 途中のデータ行の先頭の U+FEFF はデータとして残る(BOM として捨てない)
    _append(path, "1,\ufeffx,y,1\n".encode("utf-8"))
    assert importer.tail_append("r", path, state) == 1
    assert _rows(env) == [(1, "\ufeffx", "y", 1.0)]


def test_auto_switches_to_cp932_when_appended_rows_need_it(env, caplog):
    importer = _importer(env)
    path = env[3] / "r.csv"
    path.write_bytes(b"id,line,note,temp\n1,a,b,1\n")
    state = TailState()
    assert importer.tail_append("r", path, state) == 1
    _append(path, "2,ライン,注,2\n".encode("cp932"))
    assert importer.tail_append("r", path, state) == 1
    _append(path, "3,ライン3,x,3\n".encode("cp932"))
    assert importer.tail_append("r", path, state) == 1
    assert state.encoding == "cp932"
    assert [r[1] for r in _rows(env)] == ["a", "ライン", "ライン3"]
    assert sum("cp932 で読み込みました" in m for m in caplog.messages) == 1


def test_init_starts_after_the_last_complete_record(env):
    importer = _importer(env)
    path = env[3] / "r.csv"
    path.write_bytes(b'id,line,note,temp\n1,a,b,1\n2,a,"multi\nline",2\n3,a,"open')
    state = importer.init_tail("r", path)
    assert state.records == 2  # 完全なデータ行の数(参考値)
    assert state.offset == len(b'id,line,note,temp\n1,a,b,1\n2,a,"multi\nline",2\n')
    assert state.header == ["id", "line", "note", "temp"]
    _append(path, b' quote",3\n4,a,b,4\n')
    assert importer.tail_append("r", path, state) == 2
    assert [(r[0], r[2]) for r in _rows(env)] == [(3, "open quote"), (4, "b")]


def test_init_with_a_quoted_header_and_an_empty_file(env):
    importer = _importer(env)
    path = env[3] / "r.csv"
    path.write_bytes(b"")
    state = importer.init_tail("r", path)
    assert (state.offset, state.header) == (0, None)
    path.write_bytes(b'"id","line"\n"1","a"\n')
    state = importer.init_tail("r", path)
    assert state.header == ["id", "line"] and state.offset == path.stat().st_size
    _append(path, b'"2","b"\n')
    assert importer.tail_append("r", path, state) == 1
    assert _rows(env) == [(2, "b", None, None)]


def test_truncation_restarts_and_rereads_a_changed_header(env, caplog):
    importer = _importer(env)
    path = env[3] / "r.csv"
    path.write_bytes(b"id,line,note,temp\n1,a,b,1\n2,a,b,2\n")
    state = TailState()
    assert importer.tail_append("r", path, state) == 2
    path.write_bytes(b"temp,id\n9.5,9\n")  # 小さくなった・列の並びも違う
    assert importer.tail_append("r", path, state) == 1
    assert state.header == ["temp", "id"]
    assert _rows(env)[-1] == (9, None, None, 9.5)
    assert any("ファイルが小さくなったため先頭から" in m for m in caplog.messages)


def test_rotation_restarts_and_skips_existing_primary_keys(env, caplog):
    importer = _importer(env)
    path = env[3] / "r.csv"
    path.write_bytes(b"id,line,note,temp\n1,a,b,1\n2,a,b,2\n")
    state = TailState()
    importer.tail_append("r", path, state)
    rotated = path.with_suffix(".new")
    rotated.write_bytes(
        b"id,line,note,temp\n10,new,x,1\n11,new,x,2\n12,new,x,3\n13,new,x,4\n"
    )
    os.replace(rotated, path)
    assert importer.tail_append("r", path, state) == 4
    assert [r[0] for r in _rows(env)] == [1, 2, 10, 11, 12, 13]
    assert any("ファイルが差し替えられたため先頭から" in m for m in caplog.messages)
    # 先頭から読み直しても、主キーが既にある行は重複しない
    rotated.write_bytes(path.read_bytes().replace(b"13,new,x,4\n", b"") + b"14,n,x,5\n")
    os.replace(rotated, path)
    assert importer.tail_append("r", path, state) == 1
    assert [r[0] for r in _rows(env)] == [1, 2, 10, 11, 12, 13, 14]


def test_an_editor_saving_a_new_file_with_the_same_content_continues(env, caplog):
    """エディタ(Excel など)は保存のたびに別ファイルへ書いて置き換える。内容が続いて
    いれば読み直さない(主キーの無い CSV でも行が重複しない)。"""
    config = MonitorConfig(
        tables={"s": TableDef(columns={"id": "int", "ts": "str", "v": "float"})}
    )
    db, csv_dir = env[2], env[3]
    registry = TableRegistry(config)
    registry.create_all(db)
    importer = CsvImporter(config, registry, db, csv_dir)
    path = csv_dir / "s.csv"
    path.write_bytes(b"ts,v\n08:00,1\n")  # id は CSV に無い(採番される)
    state = TailState()
    assert importer.tail_append("s", path, state) == 1
    saved = path.with_suffix(".tmp")
    saved.write_bytes(path.read_bytes() + b"08:01,2\n")
    os.replace(saved, path)
    caplog.set_level(logging.INFO, logger="monitor_app.importer")
    assert importer.tail_append("s", path, state) == 1
    assert (
        "s.csv: ファイルが置き換わりましたが内容が続いているため、続きから読みます"
        in caplog.messages
    )
    with db.connect() as conn:
        assert conn.execute(text("SELECT ts FROM s ORDER BY id")).scalars().all() == [
            "08:00",
            "08:01",
        ]


def test_in_place_rewrite_of_earlier_rows_restarts(env, caplog):
    importer = _importer(env)
    path = env[3] / "r.csv"
    header = b"id,line,note,temp\n"
    body = b"".join(b"%d,line,note,%d\n" % (i, i) for i in range(1, 501))
    path.write_bytes(header + body)
    assert len(header + body) > 2 * 4096  # 先頭の 4 KB は変わらない
    state = TailState()
    assert importer.tail_append("r", path, state) == 500
    # 終わり近くの行を書き換え(長さが変わり、読み取り位置の直前が変わる)+ 追記
    path.write_bytes(
        header + body.replace(b"499,line,", b"499,LINE-LONGER,") + b"501,a,b,3\n"
    )
    assert importer.tail_append("r", path, state) == 1  # ほかは主キーが既にある
    assert len(_rows(env)) == 501
    assert any("書き換えられたため先頭から" in m for m in caplog.messages)


def test_small_files_detect_changes_in_the_head(env, caplog):
    importer = _importer(env)
    path = env[3] / "r.csv"
    path.write_bytes(b"id,line,note,temp\n1,a,b,1\n2,a,b,2\n")
    state = TailState()
    importer.tail_append("r", path, state)
    path.write_bytes(b"id,line,note,temp\n1,a,b,1\n2,aaaa,b,2\n3,a,b,3\n")
    assert importer.tail_append("r", path, state) == 1
    assert [r[0] for r in _rows(env)] == [1, 2, 3]
    assert any("差し替えられたため先頭から" in m for m in caplog.messages)


def test_state_is_not_advanced_when_the_insert_fails(env, monkeypatch):
    importer = _importer(env)
    path = env[3] / "r.csv"
    path.write_bytes(b"id,line,note,temp\n1,a,b,1\n")
    state = TailState()
    real = importer_mod.insert_in_chunks
    calls = []

    def flaky(conn, table, rows, **kw):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("DB が一時的に使えない")
        return real(conn, table, rows, **kw)

    monkeypatch.setattr(importer_mod, "insert_in_chunks", flaky)
    with pytest.raises(RuntimeError):
        importer.tail_append("r", path, state)
    assert (state.offset, state.header) == (0, None)
    assert importer.tail_append("r", path, state) == 1
    assert [r[0] for r in _rows(env)] == [1]


def test_coercion_failures_are_logged_with_record_numbers(env, caplog):
    importer = _importer(env)
    path = env[3] / "r.csv"
    path.write_bytes(b"id,line,note,temp\n1,a,b,1\n")
    state = importer.init_tail("r", path)
    _append(path, b"2,a,b,bad\n\n3,a,b,3\n,,,\n")
    assert importer.tail_append("r", path, state) == 1
    assert "r.csv 3行目: 'bad' を float に変換できません" in caplog.messages
    assert "r.csv: 値がすべて空の行を 1 行スキップしました(5 行目)" in caplog.messages


def test_encodings_that_cannot_be_tailed_fall_back_to_full_reads(env):
    importer = _importer(env, encoding="utf-16")
    path = env[3] / "r.csv"
    path.write_text("id,line,note,temp\n1,a,b,1\n", encoding="utf-16")
    state = importer.init_tail("r", path)
    assert state.full_read and state.records == 1
    # 全体を読み直し、既読の行数より後ろだけを取り込む(従来の方式)
    path.write_text("id,line,note,temp\n1,a,b,1\n2,a,b,2\n", encoding="utf-16")
    assert importer.tail_append("r", path, state) == 1
    assert state.records == 2
    assert [r[0] for r in _rows(env)] == [2]


# ---------------------------------------------------------------------------
# IngestWatcher(追記モード)
# ---------------------------------------------------------------------------
def _touch_later(path) -> None:
    st = path.stat()
    later = st.st_mtime_ns + 2_000_000_000
    os.utime(path, ns=(later, later))


def test_watcher_setup_does_not_reimport_existing_rows(env):
    importer = _importer(env)
    path = env[3] / "r.csv"
    path.write_bytes(b"id,line,note,temp\n1,a,b,1\n2,a,b,2\n3,a,b,3")  # 末尾は書きかけ
    watcher = IngestWatcher(importer, interval=1.0, keep=True)
    watcher.setup()
    assert watcher._row_counts == {"r": 2}
    _append(path, b"\n4,a,b,4\n")
    _touch_later(path)
    assert watcher.poll_once() == ["r"]
    assert [r[0] for r in _rows(env)] == [3, 4]
    assert watcher._row_counts == {"r": 4}


def test_watcher_poll_cost_does_not_depend_on_the_file_size(env, monkeypatch):
    """読むのは前回の位置から後ろだけ(全体を解析し直さない)。"""
    importer = _importer(env)
    path = env[3] / "r.csv"
    path.write_bytes(
        b"id,line,note,temp\n"
        + b"".join(b"%d,line,note,%d.5\n" % (i, i) for i in range(1, 20001))
    )
    watcher = IngestWatcher(importer, interval=1.0, keep=True)
    watcher.setup()
    parsed = []
    real = importer_mod._parse_complete
    monkeypatch.setattr(
        importer_mod,
        "_parse_complete",
        lambda data, cut, text_: parsed.append(len(data)) or real(data, cut, text_),
    )
    _append(path, b"20001,line,note,1\n")
    _touch_later(path)
    watcher.poll_once()
    assert parsed == [len(b"20001,line,note,1\n")]
    assert [r[0] for r in _rows(env)] == [20001]
