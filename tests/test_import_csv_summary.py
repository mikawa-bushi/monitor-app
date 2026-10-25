"""``import-csv`` の表示(D5-06)と、``run_csv_import`` の ``force`` の受け渡し(D7-07)。

結果の一覧に載る内容(文字コードの判別・列の対応・読み飛ばした行など)を、ログ
(標準エラー)にも出して 2 回ずつ表示していた。追記で主キーが既にあった行は
``--keep`` の目的どおりなので、⚠️ ではなく行数の欄に「既存 N 行」と示す。
"""

from __future__ import annotations

import logging

import pytest
from contract import helpers

from monitor_app.cli.data import run_csv_import
from monitor_app.settings.loader import load_config


@pytest.fixture
def project(tmp_path, monkeypatch):
    proj = helpers.start_project(tmp_path)
    monkeypatch.chdir(proj)
    return proj


def test_each_importer_notice_is_shown_once(project):
    (project / "csv" / "users.csv").write_bytes(
        "id,name,email\n1,山田,y@x\n".encode("cp932")
    )
    notice = "users.csv: UTF-8 として読めないため cp932 で読み込みました"
    first = helpers.run_cli("import-csv", cwd=project)
    assert first.returncode == 0, first.stderr.decode("utf-8", "replace")
    assert first.stdout.decode("utf-8").count(notice) == 1
    err = first.stderr.decode("utf-8")
    assert notice not in err  # ログ(標準エラー)には出さない
    # 結果に入らないログ(取り込んだ行数)は従来どおり出す
    assert "imported users: 1 rows" in err
    proc = helpers.run_cli("import-csv", "--keep", cwd=project)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    out, err = proc.stdout.decode("utf-8"), proc.stderr.decode("utf-8")
    assert out.count(notice) == 1
    assert notice not in err and "主キーが既にある" not in err
    # 既にあった行は行数の欄に(⚠️ ではない)
    assert "  users: 0 行 (skip 0、既存 1 行)" in out
    assert "⚠️  users.csv: 主キーが既にある" not in out
    assert "    users.csv: 主キーが既にある 1 行を読み飛ばしました(追記)" in out


class _Records(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def test_other_callers_keep_the_importer_logs(project):
    """結果の一覧を出さない呼び出し(runserver --import-csv など)ではログに出す。"""
    from monitor_app.cli._common import load_project

    (project / "csv" / "users.csv").write_bytes(
        "id,name,email\n1,山田,y@x\n".encode("cp932")
    )
    _path, settings, cfg = load_project("config.py")
    records = _Records()
    importer_logger = logging.getLogger("monitor_app.importer")
    importer_logger.addHandler(records)
    try:
        run_csv_import(settings, cfg)
    finally:
        importer_logger.removeHandler(records)
    assert any("cp932 で読み込みました" in m for m in records.messages)


def test_force_is_passed_through_to_the_importer(project, monkeypatch):
    calls = []

    def fake_import_all(self, **kwargs):
        calls.append(kwargs)
        from monitor_app.services.importer import ImportReport

        return ImportReport()

    monkeypatch.setattr(
        "monitor_app.services.importer.CsvImporter.import_all", fake_import_all
    )
    from monitor_app.cli._common import load_project

    _path, settings, cfg = load_project("config.py")
    run_csv_import(settings, cfg, force=True)
    run_csv_import(settings, cfg, keep=True)
    assert calls == [{"keep": False, "force": True}, {"keep": True, "force": False}]
    assert load_config(project / "config.py")


def test_invisible_characters_in_headers_are_shown(project):
    """BOM が残った見出し('\\ufeffid')は見える形で表示する(check と同じ)。"""
    (project / "csv" / "users.csv").write_bytes("﻿id,name,email\n1,a,a@x\n".encode())
    env = helpers.child_env(MONITOR_CSV_ENCODING="utf-8")
    proc = helpers.run_cli("import-csv", cwd=project, env=env)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    assert "表記ゆれを対応付けた列: '\\ufeffid' → id" in proc.stdout.decode("utf-8")
