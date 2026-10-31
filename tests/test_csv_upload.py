"""CSV のアップロード(/upload・/api/upload/{table})のテスト。

monitor-app import-csv と同じ取り込み(CsvImporter)を使うこと、取り込みの結果・失敗の
知らせ、送ったファイルを残さないこと、管理の機能として守られていることを確かめる。
"""

from __future__ import annotations

import tempfile
from pathlib import Path, PureWindowsPath

import pytest
from fastapi.testclient import TestClient

from monitor_app import MonitorConfig, TableDef, ViewDef
from monitor_app.api.routers.uploads import _upload_name
from monitor_app.main import create_app
from monitor_app.settings.runtime import AppSettings


def _app(**settings_kw):
    config = MonitorConfig(
        tables={
            "lines": TableDef(columns={"id": "int", "name": "str"}, primary_key="id"),
            "readings": TableDef(
                columns={"id": "int", "line_id": "int", "temp": "float"},
                primary_key="id",
                foreign_keys={"line_id": "lines.id"},
            ),
        },
        views={"all": ViewDef(query="SELECT * FROM readings ORDER BY id")},
    )
    settings_kw.setdefault("database_url", "sqlite://")
    return create_app(config, AppSettings(**settings_kw))


def _upload(client, table, content: bytes, name="data.csv", headers=None, **data):
    return client.post(
        f"/api/upload/{table}",
        files={"file": (name, content, "text/csv")},
        data=data,
        headers=headers,
    )


def _rows(client):
    return client.get("/api/views/all").json()["data"]


def test_append_imports_rows_and_refreshes_views():
    client = TestClient(_app())
    _upload(client, "lines", b"id,name\n1,A\n")
    assert _rows(client) == []  # 一度表示してキャッシュに載せる
    res = _upload(client, "readings", b"id,line_id,temp\n1,1,20.5\n2,1,21\n")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["file"] == "data.csv"
    assert body["mode"] == "append"
    assert body["table"] == "readings"
    assert body["inserted"] == 2
    assert body["refused"] is False
    assert [r["temp"] for r in _rows(client)] == [20.5, 21.0]

    again = _upload(client, "readings", b"id,line_id,temp\n2,1,99\n3,1,22\n").json()
    assert (again["inserted"], again["duplicates"]) == (1, 1)
    assert [r["temp"] for r in _rows(client)] == [20.5, 21.0, 22.0]


def test_replace_swaps_all_rows():
    client = TestClient(_app())
    _upload(client, "lines", b"id,name\n1,A\n2,B\n")
    res = _upload(client, "lines", b"id,name\n3,C\n", mode="replace")
    assert res.status_code == 200
    assert res.json()["inserted"] == 1
    names = client.get("/api/tables/lines").json()["data"]
    assert [r["name"] for r in names] == ["C"]


def test_replace_is_refused_when_declared_columns_are_missing_unless_forced():
    client = TestClient(_app())
    _upload(client, "lines", b"id,name\n1,A\n")
    res = _upload(client, "lines", b"id\n5\n", mode="replace")
    assert res.status_code == 200
    assert res.json()["refused"] is True
    assert res.json()["missing"] == ["name"]
    assert [r["id"] for r in client.get("/api/tables/lines").json()["data"]] == [1]

    forced = _upload(client, "lines", b"id\n5\n", mode="replace", force="true")
    assert forced.json()["refused"] is False
    assert [r["id"] for r in client.get("/api/tables/lines").json()["data"]] == [5]


def test_excel_shift_jis_csv_is_read():
    client = TestClient(_app())
    res = _upload(client, "lines", "id,name\n1,第一ライン\n".encode("cp932"))
    assert res.status_code == 200
    assert res.json()["encoding"] == "cp932"
    assert client.get("/api/tables/lines").json()["data"][0]["name"] == "第一ライン"


def test_duplicate_keys_in_a_replacement_undo_everything():
    client = TestClient(_app())
    _upload(client, "lines", b"id,name\n1,A\n")
    res = _upload(
        client, "lines", b"id,name\n7,X\n7,Y\n", name="dup.csv", mode="replace"
    )
    assert res.status_code == 409
    message = res.json()["detail"]["message"]
    assert "dup.csv" in message and "重複" in message
    assert [r["name"] for r in client.get("/api/tables/lines").json()["data"]] == ["A"]


def test_bad_values_skip_rows_with_line_numbers():
    client = TestClient(_app())
    _upload(client, "lines", b"id,name\n1,A\n")
    res = _upload(client, "readings", b"id,line_id,temp\n1,1,hot\n2,1,20\n")
    body = res.json()
    assert (body["inserted"], body["skipped"]) == (1, 1)
    assert any("2行目" in w or "2 行目" in w for w in body["warnings"])


def test_unknown_table_is_404_and_bad_mode_is_422():
    client = TestClient(_app())
    assert _upload(client, "nope", b"id\n1\n").status_code == 404
    assert _upload(client, "lines", b"id,name\n1,A\n", mode="wipe").status_code == 422


def test_uploaded_file_is_not_kept(monkeypatch, tmp_path):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    client = TestClient(_app())
    assert _upload(client, "lines", b"id,name\n1,A\n").status_code == 200
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("sent", "kept"),
    [
        ("../../etc/passwd.csv", "passwd.csv"),
        ("C:\\Users\\a\\data.csv", "data.csv"),
        ("", "lines.csv"),
        ("..", "lines.csv"),
        ("bad\nname.csv", "lines.csv"),
        ("D:config.py", "config.py"),
        ("\\\\server\\share\\data.csv", "data.csv"),
        ("D:..", "lines.csv"),
        ("...", "lines.csv"),
        ("data.csv:hidden", "lines.csv"),
        ("CON.csv", "lines.csv"),
        ("nul", "lines.csv"),
        ("com1 .csv", "lines.csv"),
        ("CONSOLE.csv", "CONSOLE.csv"),
        ("生産実績.csv", "生産実績.csv"),
    ],
)
def test_upload_names_drop_folders(sent, kept):
    assert _upload_name(sent, "lines") == kept


@pytest.mark.parametrize(
    "sent",
    ["D:config.py", "d:.env", "E:start-monitor.bat", "C:x.csv", "\\\\srv\\s\\x.csv"],
)
def test_upload_names_stay_in_the_temp_folder_on_windows(sent):
    # Windows では、別のドライブの名前をつなぐと一時フォルダの外を指す
    folder = PureWindowsPath("C:/Users/u/AppData/Local/Temp/monitor-upload-x")
    assert (folder / _upload_name(sent, "lines")).parent == folder


def test_audit_log_records_uploads():
    client = TestClient(_app(audit_enabled=True))
    _upload(client, "lines", b"id,name\n1,A\n", name="lines.csv")
    entry = client.get("/api/audit", params={"table": "lines"}).json()["entries"][0]
    assert entry["action"] == "import"
    assert entry["after"]["file"] == "lines.csv"
    assert entry["after"]["inserted"] == 1


def test_upload_requires_the_admin_key_when_set():
    client = TestClient(
        _app(api_key="user-key-123456789", admin_key="admin-key-12345678")
    )
    content = b"id,name\n1,A\n"
    assert _upload(client, "lines", content).status_code == 401
    user = {"X-API-Key": "user-key-123456789"}
    assert _upload(client, "lines", content, headers=user).status_code == 401
    admin = {"X-API-Key": "admin-key-12345678"}
    assert _upload(client, "lines", content, headers=admin).status_code == 200


def test_upload_page_lists_tables_and_columns():
    html = TestClient(_app()).get("/upload").text
    assert 'data-columns="id, name"' in html
    assert 'data-columns="id, line_id, temp"' in html
    assert "upload.js" in html


def test_upload_page_without_tables_explains():
    app = create_app(
        MonitorConfig(views={"v": ViewDef(query="SELECT 1 AS x")}),
        AppSettings(database_url="sqlite://"),
    )
    html = TestClient(app).get("/upload").text
    assert "取り込み先のテーブルがありません" in html
    assert "upload.js" not in html


def test_settings_table_name_is_reserved_for_csv_folders(tmp_path: Path):
    # 画面で変えた設定を保存する内部テーブル(_settings)は、CSV のテーブル名にできない
    from monitor_app.settings.infer import infer_project

    (tmp_path / "_settings.csv").write_text("id,x\n1,2\n", encoding="utf-8")
    (tmp_path / "ok.csv").write_text("id,x\n1,2\n", encoding="utf-8")
    project = infer_project([tmp_path])
    assert list(project.config.tables) == ["ok"]
    assert any("内部テーブル名" in note for note in project.notes)
