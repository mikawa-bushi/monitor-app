"""外部キーを強制する実サーバーで、参照されている親だけを置き換えようとしたときの扱い。

子の行が参照している親のテーブルは削除できない。何も変えずに、参照しているテーブルと
対処(子も一緒に置き換える・追記で取り込む)を示す。置換の監視で、参照し合うテーブルの
CSV を揃えて差し替えたら一緒に置き換える。実行方法は conftest.py を参照。
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text

from monitor_app import MonitorConfig, TableDef
from monitor_app.db.engine import Database
from monitor_app.db.registry import TableRegistry
from monitor_app.exceptions import ConflictError
from monitor_app.services.importer import CsvImporter
from monitor_app.services.ingest_watcher import IngestWatcher

pytestmark = pytest.mark.server_db

CONFIG = MonitorConfig(
    tables={
        "users": TableDef(columns={"id": "int", "name": "str"}, primary_key="id"),
        "orders": TableDef(
            columns={"id": "int", "user_id": "int"},
            primary_key="id",
            foreign_keys={"user_id": "users.id"},
        ),
    }
)


def _counts(url: str) -> dict:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return {
                t: conn.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar()
                for t in ("users", "orders")
            }
    finally:
        engine.dispose()


def test_replacing_only_a_referenced_parent_explains_what_to_do(server_url, tmp_path):
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()
    (csv_dir / "users.csv").write_text("id,name\n1,a\n2,b\n", encoding="utf-8")
    (csv_dir / "orders.csv").write_text("id,user_id\n1,1\n", encoding="utf-8")
    db = Database(server_url)
    try:
        importer = CsvImporter(CONFIG, TableRegistry(CONFIG), db, csv_dir)
        importer.import_all()
        # 監視(置換)が users.csv の変更だけを取り込む場合
        (csv_dir / "users.csv").write_text("id,name\n1,a2\n3,c\n", encoding="utf-8")
        with pytest.raises(ConflictError) as info:
            importer.import_table("users", csv_dir / "users.csv")
        assert "テーブル 'users' を置き換えられません" in info.value.message
        assert "orders(今回は置き換えていません)" in info.value.message
        # 子の CSV が有効行 0 件(置き換えない)のときは理由を示す
        (csv_dir / "orders.csv").write_text("id,user_id\n", encoding="utf-8")
        with pytest.raises(
            ConflictError, match="有効行が 0 件のため置き換えませんでした"
        ):
            importer.import_all()
        # 子も揃えれば置き換えられる
        (csv_dir / "orders.csv").write_text("id,user_id\n1,3\n", encoding="utf-8")
        importer.import_all()
    finally:
        db.dispose()
    assert _counts(server_url) == {"users": 2, "orders": 1}


def test_watcher_replaces_related_csvs_changed_together(server_url, tmp_path):
    """置換の監視で users.csv と orders.csv を揃えて差し替えたら、一緒に置き換える。"""
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()
    (csv_dir / "users.csv").write_text("id,name\n1,a\n2,b\n", encoding="utf-8")
    (csv_dir / "orders.csv").write_text("id,user_id\n1,1\n2,2\n", encoding="utf-8")
    db = Database(server_url)
    try:
        importer = CsvImporter(CONFIG, TableRegistry(CONFIG), db, csv_dir)
        importer.import_all()
        watcher = IngestWatcher(importer, 1.0, keep=False)
        watcher.setup()
        (csv_dir / "users.csv").write_text("id,name\n5,e\n", encoding="utf-8")
        (csv_dir / "orders.csv").write_text("id,user_id\n8,5\n", encoding="utf-8")
        for name in ("users", "orders"):
            st = (csv_dir / f"{name}.csv").stat()
            os.utime(csv_dir / f"{name}.csv", ns=(st.st_mtime_ns + 2_000_000_000,) * 2)
        assert sorted(watcher.poll_once()) == ["orders", "users"]
    finally:
        db.dispose()
    assert _counts(server_url) == {"users": 1, "orders": 1}
