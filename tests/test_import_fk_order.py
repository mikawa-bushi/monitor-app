"""CSV の一括取り込み(import_all)の外部キー順と 1 トランザクション化(CRIT-01a)、
置き換えられない親の案内、置換の監視で同時に変わった CSV のまとめての置き換え、
リポジトリの行の組み立て(PERF-10)。

PostgreSQL / MySQL は外部キーを強制するため、子の行が参照している親のテーブルを
先に削除すると 2 回目の取り込みが失敗していた。SQLite でも ``PRAGMA foreign_keys``
を有効にすると同じ条件になるので、それで確かめる(実サーバーでは tests/server_db)。
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError

import monitor_app.services.importer as importer_mod
from monitor_app import MonitorConfig, TableDef
from monitor_app.db.engine import Database
from monitor_app.db.registry import TableRegistry
from monitor_app.db.repository import TableRepository
from monitor_app.exceptions import ConflictError
from monitor_app.services.importer import CsvImporter
from monitor_app.services.ingest_watcher import IngestWatcher

CSV = {
    "users": "id,name\n1,a\n2,b\n",
    "products": "id,name,price\n1,apple,100\n2,banana,50\n",
    "orders": "id,user_id,product_id,amount\n1,1,1,2\n2,2,2,1\n3,1,2,5\n",
}


def _config(order=("orders", "users", "products")) -> MonitorConfig:
    tables = {
        "users": TableDef(columns=["id", "name"]),
        "products": TableDef(columns=["id", "name", "price"]),
        "orders": TableDef(
            columns=["id", "user_id", "product_id", "amount"],
            foreign_keys={"user_id": "users.id", "product_id": "products.id"},
        ),
    }
    # 子のテーブルを先に宣言しても、親から入れる
    return MonitorConfig(tables={name: tables[name] for name in order})


@pytest.fixture
def env(tmp_path):
    config = _config()
    db = Database(f"sqlite:///{(tmp_path / 'app.db').as_posix()}")

    @event.listens_for(db.engine, "connect")
    def _fk_on(dbapi_conn, _record):  # PostgreSQL / MySQL と同じく外部キーを強制する
        dbapi_conn.execute("PRAGMA foreign_keys = ON")

    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()
    for name, body in CSV.items():
        (csv_dir / f"{name}.csv").write_text(body, encoding="utf-8")
    registry = TableRegistry(config)
    yield config, registry, db, csv_dir
    db.dispose()


def _counts(db) -> dict:
    with db.connect() as conn:
        return {
            t: conn.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar()
            for t in ("users", "products", "orders")
        }


def test_replace_import_can_run_twice_with_enforced_foreign_keys(env):
    config, registry, db, csv_dir = env
    importer = CsvImporter(config, registry, db, csv_dir)
    importer.import_all()
    report = importer.import_all()  # 以前はここで FOREIGN KEY constraint failed
    assert _counts(db) == {"users": 2, "products": 2, "orders": 3}
    # 結果の並びは従来どおり設定の宣言順
    assert [r.table for r in report.results] == ["orders", "users", "products"]
    assert report.total_inserted == 7


def test_keep_import_inserts_parents_first(env):
    config, registry, db, csv_dir = env
    report = CsvImporter(config, registry, db, csv_dir).import_all(keep=True)
    assert report.total_inserted == 7


def test_import_is_all_or_nothing(env, monkeypatch):
    config, registry, db, csv_dir = env
    importer = CsvImporter(config, registry, db, csv_dir)
    importer.import_all()
    (csv_dir / "users.csv").write_text("id,name\n1,A\n2,B\n", encoding="utf-8")
    (csv_dir / "orders.csv").write_text(
        "id,user_id,product_id,amount\n1,1,1,9\n", encoding="utf-8"
    )
    real = importer_mod.insert_in_chunks

    def fail_on_orders(conn, table, rows):
        if table.name == "orders":
            raise RuntimeError("途中で失敗")
        return real(conn, table, rows)

    monkeypatch.setattr(importer_mod, "insert_in_chunks", fail_on_orders)
    with pytest.raises(RuntimeError):
        importer.import_all()
    # users も置き換わっていない(全テーブルが 1 トランザクション)
    with db.connect() as conn:
        assert conn.execute(text("SELECT name FROM users ORDER BY id")).all() == [
            ("a",),
            ("b",),
        ]
    assert _counts(db) == {"users": 2, "products": 2, "orders": 3}


def test_replacing_a_parent_still_referenced_by_kept_children_fails_cleanly(env):
    """子の CSV が無いのに親だけ置き換えると、外部キーを強制する DB では失敗する
    (参照されている行を消せない)。何も変えずに、参照しているテーブルと対処を示す。"""
    config, registry, db, csv_dir = env
    importer = CsvImporter(config, registry, db, csv_dir)
    importer.import_all()
    (csv_dir / "orders.csv").unlink()
    with pytest.raises(ConflictError) as info:
        importer.import_all()
    assert isinstance(info.value.__cause__, IntegrityError)
    message = info.value.message
    assert "テーブル 'products' を置き換えられません" in message  # 子から順に削除する
    assert "orders(今回は置き換えていません)" in message
    assert "import-csv --keep" in message and "データは変更していません" in message
    assert "DELETE" not in message  # ドライバの文言(SQL)は出さない
    assert _counts(db) == {"users": 2, "products": 2, "orders": 3}


@pytest.mark.parametrize(
    ("orders_csv", "reason"),
    [
        ("id,user_id,product_id,amount\n", "有効行が 0 件のため置き換えませんでした"),
        ("id,user_id,amount\n1,1,2\n", "ヘッダに列が足りないため置き換えませんでした"),
    ],
    ids=["no-valid-rows", "refused-header"],
)
def test_the_reason_a_referencing_child_was_kept_is_shown(env, orders_csv, reason):
    config, registry, db, csv_dir = env
    importer = CsvImporter(config, registry, db, csv_dir)
    importer.import_all()
    (csv_dir / "orders.csv").write_text(orders_csv, encoding="utf-8")
    with pytest.raises(ConflictError, match=f"orders\\({reason}\\)"):
        importer.import_all()
    assert _counts(db) == {"users": 2, "products": 2, "orders": 3}


def test_watcher_replace_of_a_referenced_parent_logs_the_reason(
    env, caplog, monkeypatch
):
    """追記でない監視は変わった CSV のテーブルだけを置き換える。子の行が参照している
    親は外部キーを強制する DB では置き換えられないため、対処を警告に出す。"""
    # CLI のテストが monitor_app を propagate=False にしていても caplog で拾う
    monkeypatch.setattr(logging.getLogger("monitor_app"), "propagate", True)
    config, registry, db, csv_dir = env
    importer = CsvImporter(config, registry, db, csv_dir)
    importer.import_all()
    watcher = IngestWatcher(importer, 1.0, keep=False)
    watcher.setup()
    path = csv_dir / "users.csv"
    path.write_text("id,name\n1,a2\n2,b2\n3,c\n", encoding="utf-8")
    st = path.stat()
    os.utime(path, ns=(st.st_mtime_ns + 2_000_000_000,) * 2)
    with caplog.at_level(logging.WARNING, logger="monitor_app.ingest"):
        assert watcher.poll_once() == ["users"]
    assert "users.csv: テーブル 'users' を置き換えられません" in caplog.text
    assert "orders(今回は置き換えていません)" in caplog.text
    assert _counts(db) == {"users": 2, "products": 2, "orders": 3}


def test_empty_csv_keeps_existing_rows_and_warns(env):
    config, registry, db, csv_dir = env
    importer = CsvImporter(config, registry, db, csv_dir)
    importer.import_all()
    (csv_dir / "products.csv").write_text("id,name,price\n", encoding="utf-8")
    report = importer.import_all()
    products = next(r for r in report.results if r.table == "products")
    assert products.inserted == 0
    assert products.warnings == [
        "products.csv: 有効行 0 件のため置換を中止(既存データを保持)"
    ]
    assert _counts(db) == {"users": 2, "products": 2, "orders": 3}


def test_dependency_order_handles_self_references_and_cycles(tmp_path):
    config = MonitorConfig(
        tables={
            "a": TableDef(columns=["id", "b_id"], foreign_keys={"b_id": "b.id"}),
            "b": TableDef(columns=["id", "a_id"], foreign_keys={"a_id": "a.id"}),
            "emp": TableDef(
                columns=["id", "manager_id"], foreign_keys={"manager_id": "emp.id"}
            ),
            "root": TableDef(columns=["id"]),
        }
    )
    importer = CsvImporter(
        config, TableRegistry(config), Database("sqlite://"), tmp_path
    )
    # 自己参照は依存に数えず、循環(a ↔ b)は宣言順で 1 つずつほどく(止まらない)
    assert importer.dependency_order() == ["emp", "root", "a", "b"]


def test_repository_list_rows_match_the_mapping_based_rows():
    config = MonitorConfig(
        tables={
            "t": TableDef(
                columns={
                    "id": "int",
                    "s": "str",
                    "f": "float",
                    "b": "bool",
                    "d": "date",
                    "dt": "datetime",
                }
            )
        }
    )
    db = Database("sqlite://")
    registry = TableRegistry(config)
    registry.create_all(db)
    rows = [
        {
            "id": 1,
            "s": "日本語",
            "f": 1.5,
            "b": True,
            "d": date(2026, 10, 21),
            "dt": datetime(2026, 10, 21, 8, 0, 1),
        },
        {"id": 2, "s": None, "f": None, "b": False, "d": None, "dt": None},
    ]
    with db.connect() as conn:
        conn.execute(registry.get("t").insert(), rows)
    repo = TableRepository(db, registry)
    with db.readonly() as conn:
        expected = [
            dict(r) for r in conn.execute(registry.get("t").select()).mappings()
        ]
    assert repo.list("t") == expected == rows
    assert repo.list("t", limit=1, offset=1) == [rows[1]]
    db.dispose()


def test_import_if_empty_inserts_parents_first(tmp_path, monkeypatch):
    """``runserver --import-csv-if-empty`` の取り込みも親から入れる(子を先に宣言しても)。"""
    from sqlalchemy.engine import Engine

    from monitor_app.cli.data import run_csv_import
    from monitor_app.settings.runtime import AppSettings

    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()
    for name, body in CSV.items():
        (csv_dir / f"{name}.csv").write_text(body, encoding="utf-8")
    settings = AppSettings(
        _env_file=None,
        database_url=f"sqlite:///{(tmp_path / 'app.db').as_posix()}",
        csv_dir=csv_dir,
    )

    def fk_on(dbapi_conn, _record):  # PostgreSQL / MySQL と同じく外部キーを強制する
        dbapi_conn.execute("PRAGMA foreign_keys = ON")

    event.listen(Engine, "connect", fk_on)
    try:
        report = run_csv_import(settings, _config(), only_empty=True)
        again = run_csv_import(settings, _config(), only_empty=True)
    finally:
        event.remove(Engine, "connect", fk_on)
    assert [r.table for r in report.results] == ["orders", "users", "products"]
    assert report.total_inserted == 7
    assert again.results == []  # 行のあるテーブルには取り込まない


def _touch_later(path) -> None:
    st = path.stat()
    os.utime(path, ns=(st.st_mtime_ns + 2_000_000_000,) * 2)


def test_watcher_replaces_csvs_changed_together_in_one_transaction(env):
    """置換の監視で、参照し合うテーブルの CSV を揃えて差し替えたら一緒に置き換える。

    1 つずつ置き換えると、外部キーを強制する DB では途中の状態(新しい orders が
    まだ無い users を参照する・古い orders が消える users を参照する)で失敗する。
    """
    config, registry, db, csv_dir = env
    importer = CsvImporter(config, registry, db, csv_dir)
    importer.import_all()
    watcher = IngestWatcher(importer, 1.0, keep=False)
    watcher.setup()
    (csv_dir / "users.csv").write_text("id,name\n7,new\n", encoding="utf-8")
    (csv_dir / "orders.csv").write_text(
        "id,user_id,product_id,amount\n9,7,1,1\n", encoding="utf-8"
    )
    for name in ("users", "orders"):
        _touch_later(csv_dir / f"{name}.csv")
    assert sorted(watcher.poll_once()) == ["orders", "users"]
    assert _counts(db) == {"users": 1, "products": 2, "orders": 1}
    with db.connect() as conn:
        assert conn.execute(text("SELECT id, user_id FROM orders")).all() == [(9, 7)]


def test_watcher_falls_back_to_one_csv_at_a_time_when_one_cannot_be_read(
    env, caplog, monkeypatch
):
    """まとめて取り込めない(1 つの CSV が読めない)ときは 1 つずつ取り込み、読める
    CSV は従来どおり取り込む(1 ファイルの失敗でほかを巻き込まない)。"""
    monkeypatch.setattr(logging.getLogger("monitor_app"), "propagate", True)
    config, registry, db, csv_dir = env
    importer = CsvImporter(config, registry, db, csv_dir, encoding="utf-8")
    importer.import_all()
    watcher = IngestWatcher(importer, 1.0, keep=False)
    watcher.setup()
    (csv_dir / "orders.csv").write_text(
        "id,user_id,product_id,amount\n1,1,1,9\n", encoding="utf-8"
    )
    (csv_dir / "products.csv").write_bytes(
        "id,name,price\n1,りんご,1\n".encode("cp932")
    )
    for name in ("orders", "products"):
        _touch_later(csv_dir / f"{name}.csv")
    with caplog.at_level(logging.INFO, logger="monitor_app.ingest"):
        assert sorted(watcher.poll_once()) == ["orders", "products"]
    assert "まとめて取り込めないため 1 つずつ取り込みます" in caplog.text
    assert "CSV 取り込み失敗 'products'" in caplog.text
    assert _counts(db) == {"users": 2, "products": 2, "orders": 1}
