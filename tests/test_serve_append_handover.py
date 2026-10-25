"""``serve --append`` の起動時の引き継ぎ(取り込み → フォルダ監視)。

起動前の取り込みのあと、フォルダ監視が始まるまで(アプリの組み立て・lifespan の開始)
にも CSV へ追記され得る。監視が「起動時の CSV の末尾」から読み始めると、その間の行は
取り込まれない。:meth:`IngestWatcher.import_existing` の読み取り位置を監視が引き継ぎ、
取りこぼしも二重の取り込みも起きないことを確かめる。
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path

import pytest
from contract import helpers
from fastapi.testclient import TestClient
from sqlalchemy.engine import make_url
from typer.testing import CliRunner

from monitor_app import MonitorConfig, TableDef
from monitor_app.cli import app as cli_app
from monitor_app.db.engine import Database
from monitor_app.db.registry import TableRegistry
from monitor_app.services.importer import CsvImporter
from monitor_app.services.ingest_watcher import IngestWatcher


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    helpers.isolate_from_host(monkeypatch, tmp_path)


def _rows(path: Path, table: str) -> tuple[int, int]:
    """(行数, ts の種類の数)。主キーの無い CSV なので、二重に取り込むと前者が増える。"""
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute(
            f"SELECT COUNT(*), COUNT(DISTINCT ts) FROM {table}"
        ).fetchone()


def _bump_mtime(path: Path) -> None:
    """mtime の分解能が粗いファイルシステムでも変更として検出させる。"""
    later = path.stat().st_mtime_ns + 2_000_000_000
    os.utime(path, ns=(later, later))


@pytest.fixture
def env(tmp_path):
    """主キーを CSV に持たない(自動採番の)表と、その CSV。"""
    config = MonitorConfig(
        tables={
            "log": TableDef(
                columns={"rid": "int", "ts": "str", "v": "float"}, primary_key="rid"
            )
        }
    )
    db_path = tmp_path / "w.db"
    db = Database(f"sqlite:///{db_path.as_posix()}")
    registry = TableRegistry(config)
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()
    importer = CsvImporter(config, registry, db, csv_dir)
    yield importer, csv_dir / "log.csv", db_path
    db.dispose()


def test_rows_appended_before_the_watcher_starts_are_imported_once(env):
    importer, csv, db_path = env
    # 3 行と、書きかけの 4 行目(改行がまだ無い)
    csv.write_bytes(b"ts,v\nt1,1\nt2,2\nt3,3\nt4,")
    watcher = IngestWatcher(importer, interval=1.0, keep=True)

    assert watcher.import_existing() == {"log": 3}  # 書きかけの行はまだ取り込まない
    assert _rows(db_path, "log") == (3, 3)

    # serve の取り込みのあと、監視が始まるまでに追記される(書きかけの行も完成する)
    with csv.open("ab") as fh:
        fh.write(b"4\nt5,5\nt6,6\n")
    watcher.setup()  # lifespan の開始時に監視がすぐ続きを読む
    assert _rows(db_path, "log") == (6, 6)

    with csv.open("ab") as fh:
        fh.write(b"t7,7\n")
    _bump_mtime(csv)
    watcher.poll_once()
    assert _rows(db_path, "log") == (7, 7)  # 取りこぼしも重複もない
    watcher.poll_once()
    assert _rows(db_path, "log") == (7, 7)


def test_tables_that_already_have_rows_continue_from_the_end_of_the_csv(env):
    """再利用した DB(--db)の行のあるテーブルは取り込み済みとみなし、続きだけを読む。"""
    importer, csv, db_path = env
    csv.write_bytes(b"ts,v\nt1,1\nt2,2\n")
    importer.import_all()
    watcher = IngestWatcher(importer, interval=1.0, keep=True)
    assert watcher.import_existing() == {"log": None}
    with csv.open("ab") as fh:
        fh.write(b"t3,3\n")
    watcher.setup()
    assert _rows(db_path, "log") == (3, 3)


def test_import_existing_is_only_for_the_append_watcher(env):
    importer, _csv, _db_path = env
    with pytest.raises(ValueError, match="追記"):
        IngestWatcher(importer, interval=1.0, keep=False).import_existing()


def test_without_the_handover_setup_starts_at_the_end_as_before(env):
    """import_existing を使わない従来の起動(runserver)では、監視は CSV の末尾から読む。"""
    importer, csv, db_path = env
    csv.write_bytes(b"ts,v\nt1,1\n")
    importer.import_all()
    watcher = IngestWatcher(importer, interval=1.0, keep=True)
    watcher.setup()
    assert _rows(db_path, "log") == (1, 1)
    with csv.open("ab") as fh:
        fh.write(b"t2,2\n")
    _bump_mtime(csv)
    watcher.poll_once()
    assert _rows(db_path, "log") == (2, 2)


def test_serve_append_keeps_every_row_written_during_startup(tmp_path):
    """起動の間ずっと追記し続ける書き手がいても、すべての行が 1 回ずつ取り込まれる。"""
    csv = tmp_path / "csv" / "log.csv"
    csv.parent.mkdir()
    csv.write_text("ts,v\n" + "".join(f"s{i},{i}\n" for i in range(50)))
    written = [50]
    stop = threading.Event()

    def writer():
        with csv.open("a", encoding="utf-8") as fh:
            while not stop.is_set():
                fh.write(f"w{written[0]},{written[0]}\n")
                fh.flush()
                written[0] += 1
                time.sleep(0.002)

    results: dict = {}

    def fake_run(app, **kwargs):
        db_file = Path(make_url(app.state.settings.database_url).database)
        with TestClient(app):  # lifespan(フォルダ監視)を動かす
            time.sleep(0.3)  # 監視の開始後も少し書き続ける
            stop.set()
            thread.join(5)
            total = written[0]
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                results["rows"] = _rows(db_file, "log")
                if results["rows"][0] >= total:
                    break
                time.sleep(0.1)
            time.sleep(1.5)  # 追加の取り込み(重複)が起きないことも確かめる
            results["rows"] = _rows(db_file, "log")
            results["total"] = total

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("uvicorn.run", fake_run)
            result = CliRunner().invoke(cli_app, ["serve", str(csv.parent), "--append"])
    finally:
        stop.set()
        thread.join(5)
    assert result.exit_code == 0, result.output
    assert "取り込み log:" in result.output
    total = results["total"]
    assert total > 50
    # すべての行が 1 回ずつ(主キーの無い CSV なので、二重に取り込めば行数が増える)
    assert results["rows"] == (total, total)
