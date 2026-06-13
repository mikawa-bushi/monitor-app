"""IngestWatcher 一般化・CLI sync-sources・アプリ配線の E2E テスト。"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from monitor_app import MonitorConfig, TableDef
from monitor_app.cli import app as cli_app
from monitor_app.db.engine import Database
from monitor_app.db.ingest_state import IngestStateStore
from monitor_app.db.registry import TableRegistry
from monitor_app.main import create_app
from monitor_app.services.connectors import SourceConnector, build_connectors
from monitor_app.services.importer import CsvImporter
from monitor_app.services.ingest_watcher import IngestWatcher
from monitor_app.settings.declarative import SourceDef
from monitor_app.settings.runtime import AppSettings

SCAFFOLD_CSV = Path(__file__).parent.parent / "monitor_app" / "scaffold" / "csv"


# ---------------------------------------------------------------------------
# ヘルパ
# ---------------------------------------------------------------------------


def _make_events_config(sources: dict | None = None) -> MonitorConfig:
    """events テーブルを持つテスト用 MonitorConfig を生成する。"""
    return MonitorConfig(
        tables={
            "events": TableDef(
                columns={"id": "int", "name": "str", "value": "float"},
                primary_key="id",
            ),
        },
        sources=sources or {},
    )


def _make_env(tmp_path: Path, config: MonitorConfig):
    """DB / Registry / IngestStateStore をファイル DB で組み立て返す。"""
    db_path = tmp_path / "app.db"
    db = Database(f"sqlite:///{db_path}")
    registry = TableRegistry(config)
    registry.create_all(db)
    state = IngestStateStore(db)
    state.create()
    return db, registry, state


# ---------------------------------------------------------------------------
# 1. create_app 配線: _ingest_state テーブルと app.state.connectors
# ---------------------------------------------------------------------------


def test_create_app_wires_connectors(tmp_path):
    """sources 付き MonitorConfig で create_app すると:
    - _ingest_state テーブルが存在する
    - app.state.connectors にコネクタが載っている
    """
    jsonl_path = tmp_path / "events.jsonl"
    jsonl_path.touch()

    config = _make_events_config(
        sources={
            "ev": SourceDef(
                kind="jsonl",
                path=str(jsonl_path),
                table="events",
                mode="append",
            )
        }
    )
    db_path = tmp_path / "app.db"
    settings = AppSettings(database_url=f"sqlite:///{db_path}")
    app = create_app(config, settings)

    # _ingest_state テーブルが存在する
    from sqlalchemy import inspect as sa_inspect

    with app.state.db.connect() as conn:
        table_names = sa_inspect(conn).get_table_names()
    assert "_ingest_state" in table_names

    # コネクタが app.state に載っている
    assert app.state.connectors is not None
    assert "ev" in app.state.connectors


def test_create_app_no_sources_no_connectors(tmp_path):
    """sources が空なら app.state.connectors は None のまま。"""
    config = _make_events_config()
    db_path = tmp_path / "app.db"
    settings = AppSettings(database_url=f"sqlite:///{db_path}")
    app = create_app(config, settings)
    assert app.state.connectors is None


# ---------------------------------------------------------------------------
# 2. watcher.poll_once(): CSV 変更とソース取り込みが同時に報告される
# ---------------------------------------------------------------------------


def test_watcher_poll_once_csv_and_source(tmp_path):
    """IngestWatcher.poll_once(): CSV 変更 + コネクタ取り込みの両方が返る。"""
    # CSV テーブル用設定(users テーブル)
    from monitor_app import ViewDef

    config = MonitorConfig(
        tables={
            "users": TableDef(columns=["id", "name", "email"], primary_key="id"),
            "events": TableDef(
                columns={"id": "int", "name": "str", "value": "float"},
                primary_key="id",
            ),
        },
        sources={},
    )

    db_path = tmp_path / "app.db"
    db = Database(f"sqlite:///{db_path}")
    registry = TableRegistry(config)
    registry.create_all(db)
    state = IngestStateStore(db)
    state.create()

    # CSV ディレクトリと users.csv を用意
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()
    users_csv = csv_dir / "users.csv"
    users_csv.write_text("id,name,email\n1,Alice,a@example.com\n")

    importer = CsvImporter(config, registry, db, csv_dir)

    # jsonl ソース
    jsonl_path = tmp_path / "events.jsonl"
    line = json.dumps({"id": 1, "name": "ev", "value": 1.5}) + "\n"
    jsonl_path.write_text(line)

    # sources 付きの config でコネクタを作る
    sdef = SourceDef(
        kind="jsonl",
        path=str(jsonl_path),
        table="events",
        mode="append",
    )
    config_with_src = MonitorConfig(
        tables=config.tables,
        sources={"ev": sdef},
    )
    connectors = build_connectors(config_with_src, registry, db, state)

    watcher = IngestWatcher(importer, interval=1.0, keep=False, connectors=connectors)

    # poll_once: CSV 変更(初回なので変更あり) + ソース取り込み
    result = watcher.poll_once()
    assert "users" in result
    assert "ev" in result


# ---------------------------------------------------------------------------
# 3. コネクタが例外を投げても poll_once が他ソースを処理して戻る
# ---------------------------------------------------------------------------


class _FailConnector(SourceConnector):
    """常に例外を投げるダミーコネクタ。"""

    def poll_once(self) -> int:
        raise RuntimeError("意図的な失敗")


class _OkConnector(SourceConnector):
    """1 行を取り込むダミーコネクタ。"""

    def poll_once(self) -> int:
        return 1


def _dummy_connector(tmp_path, config, registry, db, state, cls):
    """ダミーコネクタを最小限の引数で生成する。"""
    sdef = SourceDef(
        kind="jsonl",
        path=str(tmp_path / "dummy.jsonl"),
        table="events",
        mode="append",
    )
    return cls("dummy", sdef, config, registry, db, state)


def test_watcher_connector_exception_does_not_stop_others(tmp_path):
    """失敗コネクタがあっても watcher.poll_once は例外を外に出さず、
    成功コネクタの結果は返す。"""
    config = _make_events_config()
    db, registry, state = _make_env(tmp_path, config)

    # CSV は空ディレクトリ(変更なし)
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()
    importer = CsvImporter(config, registry, db, csv_dir)

    fail = _FailConnector("fail", MagicMock(), config, registry, db, state)
    ok = _OkConnector("ok", MagicMock(), config, registry, db, state)

    watcher = IngestWatcher(
        importer, interval=1.0, keep=False, connectors={"fail": fail, "ok": ok}
    )

    result = watcher.poll_once()
    # 失敗したものは含まれない、成功したものは含まれる
    assert "fail" not in result
    assert "ok" in result


# ---------------------------------------------------------------------------
# 4. API 経由確認: jsonl ソース → poll_once → テーブル API で行が見える
# ---------------------------------------------------------------------------


def test_api_after_poll_once(tmp_path):
    """jsonl ファイルに行を書いて poll_once すると、CRUD API で見える。"""
    jsonl_path = tmp_path / "events.jsonl"
    jsonl_path.write_text(json.dumps({"id": 1, "name": "hello", "value": 9.9}) + "\n")

    config = _make_events_config(
        sources={
            "ev": SourceDef(
                kind="jsonl",
                path=str(jsonl_path),
                table="events",
                mode="append",
            )
        }
    )
    db_path = tmp_path / "app.db"
    settings = AppSettings(database_url=f"sqlite:///{db_path}")
    app = create_app(config, settings)

    # poll_once を直接呼ぶ(非同期ループは起動しない)
    connector = app.state.connectors["ev"]
    n = connector.poll_once()
    assert n == 1

    with TestClient(app) as client:
        res = client.get("/api/tables/events")
    assert res.status_code == 200
    rows = res.json()["data"]
    assert len(rows) == 1
    assert rows[0]["name"] == "hello"


# ---------------------------------------------------------------------------
# 5. CLI: sync-sources の動作確認
# ---------------------------------------------------------------------------


def test_cli_sync_sources_with_data(tmp_path):
    """sync-sources が取り込み件数を出力して exit code 0 で終わる。"""
    # jsonl ファイルを用意
    jsonl_path = tmp_path / "events.jsonl"
    jsonl_path.write_text(json.dumps({"id": 1, "name": "x", "value": 1.0}) + "\n")

    # config.py を一時ファイルとして作成
    db_path = tmp_path / "app.db"
    config_src = (
        "from monitor_app import MonitorConfig, TableDef\n"
        "from monitor_app.settings.declarative import SourceDef\n"
        f"config = MonitorConfig(\n"
        f"    tables={{'events': TableDef(\n"
        f"        columns={{'id': 'int', 'name': 'str', 'value': 'float'}},\n"
        f"        primary_key='id'\n"
        f"    )}},\n"
        f"    sources={{'ev': SourceDef(\n"
        f"        kind='jsonl',\n"
        f"        path={repr(str(jsonl_path))},\n"
        f"        table='events',\n"
        f"        mode='append',\n"
        f"    )}},\n"
        f")\n"
    )
    config_file = tmp_path / "config.py"
    config_file.write_text(config_src)

    runner = CliRunner()
    result = runner.invoke(
        cli_app,
        ["sync-sources", "--config", str(config_file)],
        env={"MONITOR_DATABASE_URL": f"sqlite:///{db_path}"},
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    assert "ev: 1 行" in result.output
    assert "合計 1 行" in result.output


def test_cli_sync_sources_empty_sources(tmp_path):
    """sources が定義されていないとき、案内メッセージを出して正常終了する。"""
    db_path = tmp_path / "app.db"
    config_src = (
        "from monitor_app import MonitorConfig, TableDef\n"
        "config = MonitorConfig(\n"
        "    tables={'events': TableDef(\n"
        "        columns={'id': 'int', 'name': 'str'},\n"
        "        primary_key='id'\n"
        "    )},\n"
        ")\n"
    )
    config_file = tmp_path / "config.py"
    config_file.write_text(config_src)

    runner = CliRunner()
    result = runner.invoke(
        cli_app,
        ["sync-sources", "--config", str(config_file)],
        env={"MONITOR_DATABASE_URL": f"sqlite:///{db_path}"},
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    assert "sources が定義されていません" in result.output
