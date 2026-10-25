"""GET /api/tables/{t}?limit=&offset= は主キーの順で区切る(D3-15)。

順序が無いと PostgreSQL では更新した行が物理的に後ろへ移り、ページをまたいで行が
抜けたり重複したりした(実サーバーでの確認は tests/server_db/test_nonfinite_server.py)。
SQLite でも主キーが文字列の表は挿入順に返っていた。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from monitor_app import MonitorConfig, TableDef
from monitor_app.main import create_app
from monitor_app.settings.runtime import AppSettings


def test_pages_are_ordered_by_a_text_primary_key(tmp_path):
    config = MonitorConfig(
        tables={
            "c": TableDef(columns={"code": "str", "name": "str"}, primary_key="code")
        }
    )
    app = create_app(
        config, AppSettings(database_url=f"sqlite:///{(tmp_path / 'a.db').as_posix()}")
    )
    with TestClient(app) as client:
        client.post(
            "/api/ingest/c",
            json=[{"code": c, "name": c} for c in ("m", "b", "x", "a", "q")],
        )
        pages = [
            [
                r["code"]
                for r in client.get(f"/api/tables/c?limit=2&offset={o}").json()["data"]
            ]
            for o in (0, 2, 4)
        ]
        everything = [r["code"] for r in client.get("/api/tables/c").json()["data"]]
    assert pages == [["a", "b"], ["m", "q"], ["x"]]
    # limit なしの全件は従来どおり(順序は変えない)
    assert everything == ["m", "b", "x", "a", "q"]
