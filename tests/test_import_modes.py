"""取り込みモード(replace / append / if-empty)・1 トランザクション・外部キーの順
(DX-01, DEPLOY-02, CRIT-01a)と、ヘッダの検査(CRIT-03)。

外部キーの強制は SQLite の ``PRAGMA foreign_keys`` で再現する(PostgreSQL / MySQL と
同じ条件。実サーバーでは tests/server_db)。
"""

from __future__ import annotations

import logging

import pytest
from sqlalchemy import event, text
from typer.testing import CliRunner

import monitor_app.services.importer as importer_mod
from monitor_app import MonitorConfig, TableDef
from monitor_app.cli import app as cli_app
from monitor_app.cli.data import run_csv_import
from monitor_app.db.engine import Database
from monitor_app.db.registry import TableRegistry
from monitor_app.exceptions import ConflictError
from monitor_app.services.importer import CsvImporter, UnmatchedFile
from monitor_app.settings.runtime import AppSettings

CSV = {
    "users": "id,name,email\n1,a,a@x\n2,b,b@x\n",
    "products": "id,name,price\n1,apple,100\n2,banana,50\n",
    "orders": "id,user_id,product_id,amount\n1,1,1,2\n2,2,2,1\n3,1,2,5\n",
}


@pytest.fixture(autouse=True)
def _propagate(monkeypatch):
    # CLI のテストが monitor_app を propagate=False にしていても caplog で拾う
    monkeypatch.setattr(logging.getLogger("monitor_app"), "propagate", True)


def _config() -> MonitorConfig:
    return MonitorConfig(
        tables={
            "orders": TableDef(
                columns=["id", "user_id", "product_id", "amount"],
                foreign_keys={"user_id": "users.id", "product_id": "products.id"},
            ),
            "users": TableDef(columns=["id", "name", "email"]),
            "products": TableDef(columns=["id", "name", "price"]),
        }
    )


@pytest.fixture
def env(tmp_path):
    config = _config()
    db = Database(f"sqlite:///{(tmp_path / 'app.db').as_posix()}")

    @event.listens_for(db.engine, "connect")
    def _fk_on(dbapi_conn, _record):
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


def _rows(db, sql):
    with db.connect() as conn:
        return [tuple(r) for r in conn.execute(text(sql))]


# ---------------------------------------------------------------------------
# モード
# ---------------------------------------------------------------------------
def test_append_twice_skips_existing_primary_keys(env):
    config, registry, db, csv_dir = env
    importer = CsvImporter(config, registry, db, csv_dir)
    first = importer.import_all(keep=True)
    assert first.total_inserted == 7 and first.total_duplicates == 0
    (csv_dir / "users.csv").write_text(CSV["users"] + "3,c,c@x\n", encoding="utf-8")
    second = importer.import_all(mode="append")
    by_table = {r.table: r for r in second.results}
    assert {t: (r.inserted, r.duplicates) for t, r in by_table.items()} == {
        "orders": (0, 3),
        "users": (1, 2),
        "products": (0, 2),
    }
    assert second.total_duplicates == 7
    assert by_table["users"].warnings == [
        "users.csv: 主キーが既にある 2 行を読み飛ばしました(追記)"
    ]
    assert _counts(db) == {"users": 3, "products": 2, "orders": 3}


def test_if_empty_imports_only_into_empty_tables(env):
    config, registry, db, csv_dir = env
    registry.create_all(db)
    with db.connect() as conn:
        conn.execute(text("INSERT INTO users (id, name, email) VALUES (9, 'z', 'z@x')"))
    (csv_dir / "orders.csv").write_text(
        "id,user_id,product_id,amount\n1,9,1,2\n", encoding="utf-8"
    )
    report = CsvImporter(config, registry, db, csv_dir).import_all(mode="if-empty")
    # 行のある users は取り込まない(画面から入れた行を残す)。結果には載せない
    assert [r.table for r in report.results] == ["orders", "products"]
    assert report.skipped_tables == ["users"]
    assert report.total_inserted == 3  # products 2 + orders 1(親を先に入れる)
    assert _counts(db) == {"users": 1, "products": 2, "orders": 1}
    again = CsvImporter(config, registry, db, csv_dir).import_all(mode="if-empty")
    assert again.results == []
    assert again.skipped_tables == ["orders", "users", "products"]


def test_replace_twice_and_mixed_modes_with_enforced_foreign_keys(env):
    config, registry, db, csv_dir = env
    importer = CsvImporter(config, registry, db, csv_dir)
    for kwargs in ({}, {}, {"keep": True}, {"mode": "replace"}, {"mode": "if-empty"}):
        importer.import_all(**kwargs)
        assert _counts(db) == {"users": 2, "products": 2, "orders": 3}


def test_mode_arguments_are_validated(env):
    config, registry, db, csv_dir = env
    importer = CsvImporter(config, registry, db, csv_dir)
    with pytest.raises(ValueError, match="mode"):
        importer.import_all(mode="upsert")
    with pytest.raises(ValueError, match="keep=True"):
        importer.import_all(keep=True, mode="replace")
    assert importer.import_all(keep=True, mode="append").total_inserted == 7


def test_zero_valid_rows_keeps_that_table_and_still_replaces_the_others(env):
    """有効行 0 件の CSV のテーブルは残す。削除済みの行を戻すため取り消して
    やり直しても、ほかのテーブルは正しく置き換わる(#16 + 1 トランザクション)。"""
    config, registry, db, csv_dir = env
    importer = CsvImporter(config, registry, db, csv_dir)
    importer.import_all()
    (csv_dir / "users.csv").write_text("id,name,email\n1,A,A@x\n2,B,B@x\n")
    (csv_dir / "products.csv").write_text("id,name,price\n1,x,bad\n")
    report = importer.import_all()
    by_table = {r.table: r for r in report.results}
    assert by_table["products"].inserted == 0
    assert by_table["products"].skipped == 1
    assert by_table["products"].warnings == [
        "products.csv 2行目: 'bad' を float に変換できません",
        "products.csv: 有効行 0 件のため置換を中止(既存データを保持)",
    ]
    assert by_table["users"].inserted == 2 and by_table["orders"].inserted == 3
    assert _rows(db, "SELECT name FROM users ORDER BY id") == [("A",), ("B",)]
    assert _rows(db, "SELECT name FROM products ORDER BY id") == [
        ("apple",),
        ("banana",),
    ]
    assert report.total_inserted == 5


def test_every_csv_empty_changes_nothing(env):
    config, registry, db, csv_dir = env
    importer = CsvImporter(config, registry, db, csv_dir)
    importer.import_all()
    for name in CSV:
        (csv_dir / f"{name}.csv").write_text(CSV[name].splitlines()[0] + "\n")
    report = importer.import_all()
    assert report.total_inserted == 0
    assert all(
        r.warnings[-1].endswith("置換を中止(既存データを保持)") for r in report.results
    )
    assert _counts(db) == {"users": 2, "products": 2, "orders": 3}


def test_a_failure_in_a_later_table_rolls_back_savepoints_too(env, monkeypatch):
    """文字コードの読み直し用の SAVEPOINT を使っても、全体は 1 トランザクションのまま
    (SQLite で RELEASE が確定にならない)。"""
    config, registry, db, csv_dir = env
    importer = CsvImporter(config, registry, db, csv_dir)  # 既定の auto(SAVEPOINT あり)
    importer.import_all()
    (csv_dir / "users.csv").write_text("id,name,email\n1,X,x@x\n2,Y,y@x\n")
    (csv_dir / "orders.csv").write_text(
        "id,user_id,product_id,amount\n1,1,1,1\n1,1,1,1\n"  # 主キーの重複
    )
    # 主キーの重複は CSV の行番号つきで知らせる(D5-08。以前はドライバの UNIQUE の文言)
    with pytest.raises(
        ConflictError, match=r"orders\.csv 3行目: 主キー id=1 が 2行目と"
    ):
        importer.import_all()
    assert _rows(db, "SELECT name FROM users ORDER BY id") == [("a",), ("b",)]
    assert _counts(db) == {"users": 2, "products": 2, "orders": 3}


def test_append_failure_in_a_later_table_keeps_earlier_tables_unchanged(env):
    """追記では最初の SQL が SAVEPOINT になる。SQLite でそれがトランザクションの外で
    始まると RELEASE が確定(COMMIT)になり、後のテーブルの失敗で取り消せない。"""
    config, registry, db, csv_dir = env
    importer = CsvImporter(config, registry, db, csv_dir)  # auto: SAVEPOINT を使う
    importer.import_all()
    (csv_dir / "users.csv").write_text(CSV["users"] + "3,c,c@x\n")
    (csv_dir / "products.csv").write_text(CSV["products"] + "3,cherry,10\n")
    (csv_dir / "orders.csv").write_text(
        "id,user_id,product_id,amount\n9,99,1,1\n"  # 存在しない user_id(外部キー違反)
    )
    # CSV のファイル名と行番号つきで知らせる(D3-06。以前はドライバの FOREIGN KEY の文言)
    with pytest.raises(ConflictError, match=r"orders\.csv 2行目: 外部キー制約に違反"):
        importer.import_all(keep=True)
    assert _counts(db) == {"users": 2, "products": 2, "orders": 3}


# ---------------------------------------------------------------------------
# ヘッダの検査(CRIT-03)
# ---------------------------------------------------------------------------
def test_replace_is_refused_when_a_declared_column_is_missing(env, caplog):
    caplog.set_level(logging.INFO, logger="monitor_app.importer")
    config, registry, db, csv_dir = env
    importer = CsvImporter(config, registry, db, csv_dir)
    importer.import_all()
    (csv_dir / "users.csv").write_text("id,Name,e-mail\n1,A,a@y\n")
    report = importer.import_all()
    users = next(r for r in report.results if r.table == "users")
    assert users.refused and users.inserted == 0
    assert users.missing == ["email"] and users.unknown == ["e-mail"]
    assert users.renamed == {"Name": "name"}
    assert users.matched == ["id", "name"]
    assert users.warnings == [
        "users.csv: config.py の列 email が CSV のヘッダに無いため、置換を中止しました"
        "(既存データを保持)(CSV の列: 'e-mail' → email?)。"
        "ヘッダの列名を config.py の columns に合わせてください"
    ]
    assert _rows(db, "SELECT id, name, email FROM users ORDER BY id") == [
        (1, "a", "a@x"),
        (2, "b", "b@x"),
    ]
    assert "users.csv: 列名 'Name' を 'name' として取り込みます" in caplog.messages
    # ほかのテーブルは置き換わる
    assert _counts(db) == {"users": 2, "products": 2, "orders": 3}


def test_force_replaces_even_with_missing_columns(env):
    config, registry, db, csv_dir = env
    importer = CsvImporter(config, registry, db, csv_dir)
    importer.import_all()
    (csv_dir / "users.csv").write_text("id,name\n1,A\n2,B\n")
    report = importer.import_all(force=True)
    users = next(r for r in report.results if r.table == "users")
    assert not users.refused and users.inserted == 2
    assert users.warnings == [
        "users.csv: config.py の列 email が CSV のヘッダにありません(空のまま取り込みます)"
    ]
    assert _rows(db, "SELECT id, name, email FROM users ORDER BY id") == [
        (1, "A", None),
        (2, "B", None),
    ]


def test_append_and_if_empty_import_with_a_warning_instead_of_refusing(env):
    config, registry, db, csv_dir = env
    (csv_dir / "users.csv").write_text("id,name\n1,A\n2,B\n")
    report = CsvImporter(config, registry, db, csv_dir).import_all(mode="if-empty")
    users = next(r for r in report.results if r.table == "users")
    assert not users.refused and users.inserted == 2
    assert users.warnings[0].endswith("CSV のヘッダにありません(空のまま取り込みます)")
    (csv_dir / "users.csv").write_text("id,name\n1,A\n2,B\n3,C\n")
    report = CsvImporter(config, registry, db, csv_dir).import_all(keep=True)
    users = next(r for r in report.results if r.table == "users")
    assert not users.refused and (users.inserted, users.duplicates) == (1, 2)


def test_missing_primary_key_column_is_not_a_reason_to_refuse(env):
    config, registry, db, csv_dir = env
    (csv_dir / "orders.csv").unlink()
    (csv_dir / "users.csv").write_text("name,email\nA,a@x\nB,b@x\n")
    report = CsvImporter(config, registry, db, csv_dir).import_all()
    users = next(r for r in report.results if r.table == "users")
    assert not users.refused and users.missing == ["id"] and users.warnings == []
    assert _rows(db, "SELECT id, name FROM users ORDER BY id") == [(1, "A"), (2, "B")]


def test_header_variants_are_mapped_case_width_and_bom_insensitively(env, caplog):
    config, registry, db, csv_dir = env
    (csv_dir / "orders.csv").unlink()
    # BOM を除かない文字コードで読んでも見出しの BOM を無視する
    (csv_dir / "users.csv").write_text(
        "\ufeffID,ＮＡＭＥ, Email \n1,A,a@x\n", encoding="utf-8"
    )
    importer = CsvImporter(config, registry, db, csv_dir, encoding="utf-8")
    report = importer.import_all()
    users = next(r for r in report.results if r.table == "users")
    assert users.renamed == {"\ufeffID": "id", "ＮＡＭＥ": "name", " Email ": "email"}
    assert _rows(db, "SELECT id, name, email FROM users") == [(1, "A", "a@x")]


def test_unmatched_csv_files_are_reported_with_a_suggestion(env, caplog):
    config, registry, db, csv_dir = env
    (csv_dir / "Products.CSV").write_text("id\n1\n")
    (csv_dir / "production.csv").write_text("id\n1\n")
    (csv_dir / "notes.txt").write_text("not a csv")
    report = CsvImporter(config, registry, db, csv_dir).import_all()
    assert report.unmatched_files == [
        UnmatchedFile("Products.CSV", "products"),
        UnmatchedFile("production.csv", None),
    ]
    assert (
        "Products.CSV: 対応するテーブルがありません(テーブル 'products' のつもりなら"
        "ファイル名を products.csv にしてください)" in caplog.messages
    )
    assert any(
        m.startswith("production.csv: 対応するテーブルがありません")
        for m in caplog.messages
    )


def test_case_insensitive_file_systems_do_not_report_imported_files(env, monkeypatch):
    config, registry, db, csv_dir = env
    (csv_dir / "users.csv").rename(csv_dir / "Users.csv")
    (csv_dir / "orders.csv").unlink()
    # Windows・macOS では Users.csv を users.csv として開ける
    monkeypatch.setattr(
        importer_mod,
        "_same_file",
        lambda a, b: a.name.lower() == b.name.lower(),
    )
    importer = CsvImporter(config, registry, db, csv_dir)
    assert importer.unmatched_files() == []


# ---------------------------------------------------------------------------
# CLI(run_csv_import と import-csv)
# ---------------------------------------------------------------------------
def _settings(tmp_path, csv_dir) -> AppSettings:
    return AppSettings(
        _env_file=None,
        database_url=f"sqlite:///{(tmp_path / 'app.db').as_posix()}",
        csv_dir=csv_dir,
    )


def test_run_csv_import_modes(env, tmp_path):
    config, _registry, _db, csv_dir = env
    settings = _settings(tmp_path, csv_dir)
    assert run_csv_import(settings, config).total_inserted == 7
    assert run_csv_import(settings, config, keep=True).total_duplicates == 7
    empty = run_csv_import(settings, config, only_empty=True)
    assert empty.results == [] and len(empty.skipped_tables) == 3
    (csv_dir / "orders.csv").unlink()
    (csv_dir / "users.csv").write_text("id,name\n1,A\n")
    refused = run_csv_import(settings, config)
    assert next(r for r in refused.results if r.table == "users").refused
    forced = run_csv_import(settings, config, force=True)
    assert next(r for r in forced.results if r.table == "users").inserted == 1


def test_import_csv_keep_reports_skipped_duplicates(tmp_path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(cli_app, ["startproject", "proj"]).exit_code == 0
    monkeypatch.chdir(tmp_path / "proj")
    assert runner.invoke(cli_app, ["import-csv"]).exit_code == 0
    result = runner.invoke(cli_app, ["import-csv", "--keep"])
    assert result.exit_code == 0, result.output
    assert "主キーが既にある 5 行を読み飛ばしました(追記)" in result.output
