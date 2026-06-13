"""取り込み堅牢化(#9 E1 / #10 E2 / #15 E3 / #16 E4)の回帰テスト。"""

from __future__ import annotations

import os
import time

from monitor_app import MonitorConfig, TableDef
from monitor_app.db.engine import Database
from monitor_app.db.registry import TableRegistry
from monitor_app.services.importer import CsvImporter
from monitor_app.services.ingest_watcher import IngestWatcher


def _env(tmp_path):
    config = MonitorConfig(
        tables={
            "events": TableDef(
                columns={"id": "int", "name": "str"}, primary_key="id"
            ),
        },
    )
    db = Database(f"sqlite:///{tmp_path / 'app.db'}")
    registry = TableRegistry(config)
    registry.create_all(db)
    return config, db, registry


def _count(db, registry) -> int:
    with db.connect() as conn:
        return len(conn.execute(registry.get("events").select()).fetchall())


# --- #9 E1: append 監視で全行重複しない -----------------------------------
def test_csv_append_incremental_no_duplication(tmp_path):
    config, db, registry = _env(tmp_path)
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()
    csv_path = csv_dir / "events.csv"
    csv_path.write_text("id,name\n1,a\n2,b\n")

    importer = CsvImporter(config, registry, db, csv_dir)
    watcher = IngestWatcher(importer, interval=1.0, keep=True)

    # 初回: 2 行
    watcher.poll_once()
    assert _count(db, registry) == 2

    # 変化なし: 2 行のまま
    watcher.poll_once()
    assert _count(db, registry) == 2

    # 1 行追記 → 増分のみ(全行再取込なら 5 行になる)
    time.sleep(0.01)
    with csv_path.open("a") as fh:
        fh.write("3,c\n")
    os.utime(csv_path, None)
    watcher.poll_once()
    assert _count(db, registry) == 3


# --- #10 E2: 1 ファイルの取り込み失敗で poll_once が落ちない ----------------
def test_poll_once_swallows_csv_import_error(tmp_path):
    config, db, registry = _env(tmp_path)
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()
    (csv_dir / "events.csv").write_text("id,name\n1,a\n")

    importer = CsvImporter(config, registry, db, csv_dir)

    def boom(*_a, **_k):
        raise RuntimeError("意図的な失敗")

    importer.import_table = boom  # keep=False 経路を失敗させる

    watcher = IngestWatcher(importer, interval=1.0, keep=False)
    # 例外が外に漏れない(変更検出済みなので events は結果に含まれる)
    result = watcher.poll_once()
    assert result == ["events"]


# --- #16 E4: 空 CSV の置換で既存データを消さない ----------------------------
def test_replace_skips_wipe_on_empty_csv(tmp_path):
    config, db, registry = _env(tmp_path)
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()
    csv_path = csv_dir / "events.csv"
    csv_path.write_text("id,name\n1,a\n2,b\n")

    importer = CsvImporter(config, registry, db, csv_dir)
    importer.import_table("events", csv_path, keep=False)
    assert _count(db, registry) == 2

    # ヘッダのみ(有効行 0)で置換 → 全消去せず保持
    csv_path.write_text("id,name\n")
    res = importer.import_table("events", csv_path, keep=False)
    assert _count(db, registry) == 2
    assert res.inserted == 0


# --- #15 E3: CP932(Shift-JIS)CSV を読める -------------------------------
def test_csv_cp932_encoding(tmp_path):
    config, db, registry = _env(tmp_path)
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()
    csv_path = csv_dir / "events.csv"
    csv_path.write_bytes("id,name\n1,ライン\n".encode("cp932"))

    importer = CsvImporter(config, registry, db, csv_dir, encoding="cp932")
    importer.import_table("events", csv_path, keep=False)

    with db.connect() as conn:
        row = conn.execute(registry.get("events").select()).fetchone()
    assert row[1] == "ライン"
