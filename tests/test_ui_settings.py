"""画面での設定の編集(/settings・/api/settings)のテスト。

config.py の設定に画面で変えた差分を重ね、DB(_settings 表)に保存して、サーバーを
止めずに反映する。保存の前の点検(monitor-app check と同じ判定と、画面から入れた SQL・
表示形式の制限)と、管理の機能の認証(管理者の鍵・API キー・この PC だけ・CSRF)も確かめる。
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text

from monitor_app import AlertRule, KpiCard, MonitorConfig, TableDef, ViewDef
from monitor_app.api.routers.views import _sse_event_stream
from monitor_app.main import CONFIG_REVISION_HEADER, create_app
from monitor_app.services.alert_scheduler import AlertScheduler
from monitor_app.settings.overrides import (
    apply_overrides,
    diff_patch,
    merge_patch,
)
from monitor_app.settings.runtime import AppSettings


def _config(**extra) -> MonitorConfig:
    values = {
        "tables": {
            "readings": TableDef(
                columns={"id": "int", "line": "str", "temp": "float"},
                primary_key="id",
            )
        },
        "views": {
            "latest": ViewDef(query="SELECT * FROM readings", title="最新"),
            "hot": ViewDef(
                query="SELECT line, temp FROM readings WHERE temp > 50", title="高温"
            ),
        },
        "kpis": {"count": KpiCard(title="件数", query="SELECT COUNT(*) FROM readings")},
    }
    values.update(extra)
    return MonitorConfig(**values)


def _app(config: MonitorConfig | None = None, **settings_kw):
    settings_kw.setdefault("database_url", "sqlite://")
    return create_app(config or _config(), AppSettings(**settings_kw))


def _load(client: TestClient, **headers) -> dict:
    res = client.get("/api/settings", headers=headers)
    assert res.status_code == 200, res.text
    return res.json()


def _save(client: TestClient, body: dict, settings: dict, **headers):
    return client.put(
        "/api/settings",
        json={"settings": settings, "revision": body["revision"]},
        headers=headers,
    )


def _stored(app) -> dict | None:
    with app.state.db.engine.connect() as conn:
        if not inspect(conn).has_table("_settings"):
            return None
        row = conn.execute(text("SELECT value FROM _settings")).fetchone()
    return json.loads(row[0]) if row else None


# ---------------------------------------------------------------------------
# 差分(JSON Merge Patch)
# ---------------------------------------------------------------------------
def test_diff_and_merge_patch_round_trip():
    base = {"a": 1, "views": {"x": {"title": "X", "chart": {"ucl": 1}}}, "list": [1, 2]}
    desired = {"a": 2, "views": {"x": {"title": "X", "chart": None}, "y": {"t": 1}}}
    patch = diff_patch(base, desired)
    assert patch == {
        "a": 2,
        "views": {"x": {"chart": None}, "y": {"t": 1}},
        "list": None,
    }
    assert merge_patch(base, patch) == {
        "a": 2,
        "views": {"x": {"title": "X"}, "y": {"t": 1}},
    }
    assert base["views"]["x"]["chart"] == {"ucl": 1}  # 元の値は変えない
    assert diff_patch(base, base) == {}


def test_apply_overrides_only_touches_editable_fields():
    base = _config()
    patch = {
        "views": {"latest": {"title": "ライン別"}},
        "tables": {"readings": None},  # 編集できない項目は無視する
        "favicon_path": "../../etc/passwd",
    }
    config = apply_overrides(base, patch)
    assert config.views["latest"].title == "ライン別"
    assert config.views["latest"].query == "SELECT * FROM readings"
    assert config.tables == base.tables
    assert config.favicon_path == base.favicon_path
    assert base.views["latest"].title == "最新"
    assert apply_overrides(base, None) is base


# ---------------------------------------------------------------------------
# 読み込み・保存・反映
# ---------------------------------------------------------------------------
def test_get_returns_settings_candidates_and_columns():
    client = TestClient(_app())
    body = _load(client)
    assert body["settings"]["views"]["latest"]["title"] == "最新"
    assert body["config_py"] == body["settings"]
    assert body["customized"] is False
    assert body["tables"] == ["readings"]
    assert [c["name"] for c in body["notify_channels"]] == [
        "console",
        "webhook",
        "email",
    ]
    assert body["view_columns"]["hot"] == ["line", "temp"]
    assert "tables" not in body["settings"] and "sources" not in body["settings"]


def test_save_stores_only_the_difference_and_applies_without_restart():
    app = _app()
    client = TestClient(app)
    client.post("/api/ingest/readings", json={"id": 1, "line": "A", "temp": 70})
    body = _load(client)
    before = client.get("/api/views/latest").headers[CONFIG_REVISION_HEADER]
    settings = body["settings"]
    settings["views"]["latest"]["title"] = "ライン別の最新"
    settings["views"]["hot"]["chart"] = {"x": "line", "y": "temp", "ucl": 80}
    res = _save(client, body, settings)
    assert res.status_code == 200, res.text
    assert res.json()["customized"] is True
    assert _stored(app) == {
        "views": {
            "latest": {"title": "ライン別の最新"},
            "hot": {
                "chart": {
                    "type": "line",
                    "x": "line",
                    "y": "temp",
                    "ucl": 80.0,
                }
            },
        }
    }
    # サーバーを止めずに反映される(ビューのキャッシュも捨てる)
    view = client.get("/api/views/latest")
    assert view.json()["title"] == "ライン別の最新"
    assert view.headers[CONFIG_REVISION_HEADER] != before
    assert "ライン別の最新" in client.get("/table/latest").text
    assert app.state.config.views["hot"].chart.ucl == 80
    assert app.state.services.config_revision == view.headers[CONFIG_REVISION_HEADER]


def test_saved_settings_survive_restart_and_config_py_edits_still_apply(tmp_path):
    url = f"sqlite:///{tmp_path / 'db.sqlite'}"
    client = TestClient(_app(database_url=url))
    body = _load(client)
    body["settings"]["views"]["latest"]["title"] = "画面で変えた"
    assert _save(client, body, body["settings"]).status_code == 200

    # config.py で別の項目を変えて再起動: 画面の変更と config.py の変更の両方が効く
    edited = _config()
    edited.views["hot"] = ViewDef(
        query="SELECT * FROM readings", title="config.py で変えた"
    )
    app = _app(edited, database_url=url)
    assert app.state.config.views["latest"].title == "画面で変えた"
    assert app.state.config.views["hot"].title == "config.py で変えた"
    again = _load(TestClient(app))
    assert again["customized"] is True
    assert again["config_py"]["views"]["latest"]["title"] == "最新"


def test_overrides_that_no_longer_fit_config_py_fall_back_with_a_notice(tmp_path):
    url = f"sqlite:///{tmp_path / 'db.sqlite'}"
    client = TestClient(_app(database_url=url))
    body = _load(client)
    body["settings"]["views"]["hot"]["title"] = "高温(画面)"
    assert _save(client, body, body["settings"]).status_code == 200

    # config.py から hot を消して再起動: 差分を重ねられないので config.py だけで動く
    shrunk = _config(views={"latest": ViewDef(query="SELECT * FROM readings")})
    app = _app(shrunk, database_url=url)
    assert list(app.state.config.views) == ["latest"]
    body = _load(TestClient(app))
    assert "config.py の設定だけで動いています" in body["load_error"]
    # 保存すれば今の画面の内容に置き換わり、知らせは消える
    res = _save(TestClient(app), body, body["settings"])
    assert res.status_code == 200
    assert res.json()["load_error"] is None
    assert res.json()["customized"] is False


def test_saving_the_config_py_values_removes_the_stored_difference():
    app = _app()
    client = TestClient(app)
    body = _load(client)
    body["settings"]["app_title"] = "ライン 1"
    assert _save(client, body, body["settings"]).status_code == 200
    body = _load(client)
    body["settings"]["app_title"] = "Monitor App"
    res = _save(client, body, body["settings"])
    assert res.json()["customized"] is False
    assert _stored(app) is None


def test_reset_returns_to_config_py():
    app = _app()
    client = TestClient(app)
    body = _load(client)
    body["settings"]["views"]["latest"]["title"] = "変えた"
    body = _save(client, body, body["settings"]).json()
    res = client.delete("/api/settings", params={"revision": body["revision"]})
    assert res.status_code == 200
    assert res.json()["customized"] is False
    assert client.get("/api/views/latest").json()["title"] == "最新"
    assert _stored(app) is None


def test_stale_revision_is_refused():
    client = TestClient(_app())
    body = _load(client)
    first = dict(body["settings"], app_title="A")
    assert _save(client, body, first).status_code == 200
    second = dict(body["settings"], app_title="B")  # 古い版のまま保存しようとする
    res = _save(client, body, second)
    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "STALE_SETTINGS"
    assert _load(client)["settings"]["app_title"] == "A"
    res = client.delete("/api/settings", params={"revision": body["revision"]})
    assert res.status_code == 409


def test_audit_log_records_settings_changes():
    app = _app(audit_enabled=True)
    client = TestClient(app)
    body = _load(client)
    body["settings"]["app_title"] = "監査"
    assert _save(client, body, body["settings"]).status_code == 200
    entries = client.get("/api/audit", params={"table": "_settings"}).json()["entries"]
    assert entries[0]["action"] == "update"
    assert entries[0]["after"] == {"app_title": "監査"}
    assert entries[0]["actor"].startswith("anon@")


# ---------------------------------------------------------------------------
# 点検
# ---------------------------------------------------------------------------
def test_sql_errors_block_saving_and_keep_the_running_config():
    app = _app()
    client = TestClient(app)
    body = _load(client)
    body["settings"]["views"]["latest"]["query"] = "SELECT * FROM readngs"
    res = _save(client, body, body["settings"])
    assert res.status_code == 422
    issue = res.json()["issues"][0]
    assert issue["code"] == "sql-error"
    assert "readings" in issue["hint"]
    assert app.state.config.views["latest"].query == "SELECT * FROM readings"
    assert _stored(app) is None


def test_validation_errors_are_reported_in_japanese():
    client = TestClient(_app())
    body = _load(client)
    body["settings"]["alerts"] = [
        {"view": "missing", "column": "temp", "op": ">", "value": 1}
    ]
    res = client.post("/api/settings/check", json={"settings": body["settings"]})
    assert res.status_code == 200
    payload = res.json()
    assert payload["ok"] is False
    assert any("missing" in i["message"] for i in payload["issues"])


def test_alert_column_mismatch_is_an_error_and_check_returns_columns():
    client = TestClient(_app())
    body = _load(client)
    settings = body["settings"]
    settings["views"]["new_view"] = {"query": "SELECT line AS ライン FROM readings"}
    settings["alerts"] = [{"view": "hot", "column": "tmp", "op": ">", "value": 80}]
    payload = client.post("/api/settings/check", json={"settings": settings}).json()
    codes = {i["code"] for i in payload["issues"]}
    assert "alert-column" in codes
    assert payload["view_columns"]["new_view"] == ["ライン"]


@pytest.mark.parametrize(
    "query",
    [
        "SELECT 1; DELETE FROM readings",
        "SELECT $$x$$; COMMIT; DELETE FROM readings",
    ],
)
def test_multiple_statements_from_the_screen_are_refused(query):
    client = TestClient(_app())
    body = _load(client)
    body["settings"]["views"]["latest"]["query"] = query
    res = _save(client, body, body["settings"])
    assert res.status_code == 422
    assert "sql-multiple-statements" in {i["code"] for i in res.json()["issues"]}


def test_semicolons_inside_strings_and_trailing_ones_are_fine():
    client = TestClient(_app())
    body = _load(client)
    body["settings"]["views"]["latest"]["query"] = "SELECT ';' AS sep FROM readings;"
    res = _save(client, body, body["settings"])
    assert res.status_code == 200, res.text
    assert res.json()["settings"]["views"]["latest"]["query"] == (
        "SELECT ';' AS sep FROM readings"
    )


@pytest.mark.parametrize(
    ("fmt", "ok"),
    [
        ("{:.1f} %", True),
        ("{:,.0f}", True),
        ("{0:.2f}", True),
        ("{0.__class__}", False),
        ("{0[0]}", False),
        ("{:{width}}", False),
        ("{}{}", False),
        ("{!r}", False),
    ],
)
def test_kpi_formats_from_the_screen_only_format_the_value(fmt, ok):
    client = TestClient(_app())
    body = _load(client)
    body["settings"]["kpis"]["count"]["format"] = fmt
    payload = client.post("/api/settings/check", json={"settings": body["settings"]})
    codes = {i["code"] for i in payload.json()["issues"]}
    assert ("kpi-format-fields" not in codes) is ok


@pytest.mark.parametrize("name", ["a/b", "x,y", " lead", "", "q?"])
def test_new_view_names_must_be_safe_for_urls(name):
    client = TestClient(_app())
    body = _load(client)
    body["settings"]["views"][name] = {"query": "SELECT * FROM readings"}
    payload = client.post("/api/settings/check", json={"settings": body["settings"]})
    assert "invalid-name" in {i["code"] for i in payload.json()["issues"]}


def test_config_py_queries_are_trusted_even_if_the_screen_would_refuse_them():
    # config.py に書いた SQL(サーバーの管理者が書いたもの)は画面の制限の対象外
    config = _config(
        views={"odd": ViewDef(query="SELECT 'a\\' AS v FROM readings")},  # 'a\'
        kpis={"k": KpiCard(title="K", query="SELECT 1", format="{0.real:.0f}")},
    )
    client = TestClient(_app(config))
    body = _load(client)
    codes = {i["code"] for i in body["issues"]}
    assert not codes & {"sql-multiple-statements", "kpi-format-fields"}
    body["settings"]["app_title"] = "x"
    assert _save(client, body, body["settings"]).status_code == 200


# ---------------------------------------------------------------------------
# アラートの差し替え
# ---------------------------------------------------------------------------
def test_first_alert_added_on_the_screen_starts_the_scheduler():
    app = _app()
    with TestClient(app) as client:
        services = app.state.services
        assert not any(isinstance(t, AlertScheduler) for t in services.background_tasks)
        body = _load(client)
        body["settings"]["alerts"] = [
            {"view": "hot", "column": "temp", "op": ">", "value": 80}
        ]
        assert _save(client, body, body["settings"]).status_code == 200
        schedulers = [
            t for t in services.background_tasks if isinstance(t, AlertScheduler)
        ]
        assert len(schedulers) == 1 and schedulers[0].running
        assert services.alert_engine.alert_views() == ["hot"]


def test_changing_a_threshold_reevaluates_with_the_new_rule():
    config = _config(alerts=[AlertRule(view="hot", column="temp", op=">", value=60)])
    app = _app(config)
    client = TestClient(app)
    client.post("/api/ingest/readings", json={"id": 1, "line": "A", "temp": 70})
    client.get("/api/views/hot")
    assert [a["message"] for a in client.get("/api/alerts").json()["alerts"]] == [
        "hot.temp > 60.0"
    ]
    body = _load(client)
    body["settings"]["alerts"][0]["value"] = 90
    assert _save(client, body, body["settings"]).status_code == 200
    assert client.get("/api/alerts").json()["alerts"] == []  # 古いルールは解除
    client.get("/api/views/hot")
    assert client.get("/api/alerts").json()["alerts"] == []  # 70 は 90 を超えない
    assert app.state.services.background_tasks[0].interval == 2.0


def test_refresh_interval_change_updates_the_alert_scheduler():
    config = _config(alerts=[AlertRule(view="hot", column="temp", op=">", value=60)])
    app = _app(config)
    client = TestClient(app)
    body = _load(client)
    body["settings"]["refresh_interval_ms"] = 5000
    assert _save(client, body, body["settings"]).status_code == 200
    scheduler = app.state.services.background_tasks[0]
    assert isinstance(scheduler, AlertScheduler) and scheduler.interval == 5.0


def test_sse_stream_asks_the_page_to_reload_when_the_settings_change():
    app = _app()
    services = app.state.services
    revision = {"value": "a"}

    async def disconnected():
        return False

    async def collect():
        events = []
        stream = _sse_event_stream(
            services.view_service,
            services.alert_engine,
            "latest",
            0.01,
            disconnected,
            revision=lambda: revision["value"],
        )
        async for event in stream:
            events.append(event["event"])
            revision["value"] = "b"  # 最初のデータを送った後に設定が変わる
        return events

    assert asyncio.run(collect()) == ["message", "reload"]


def test_pages_carry_the_config_revision_and_admin_links():
    app = _app()
    html = TestClient(app).get("/").text
    revision = app.state.services.config_revision
    assert f'<meta name="monitor-config" content="{revision}">' in html
    assert 'href="/settings"' in html and 'href="/upload"' in html
    kiosk = TestClient(app).get("/kiosk").text
    assert f'<meta name="monitor-config" content="{revision}">' in kiosk


# ---------------------------------------------------------------------------
# 認証(管理の機能)
# ---------------------------------------------------------------------------
def test_no_keys_on_this_machine_only_allows_the_editor():
    client = TestClient(_app())  # 既定は 127.0.0.1 で待ち受け
    assert client.get("/api/settings").status_code == 200
    assert "settings-app" in client.get("/settings").text


@pytest.mark.parametrize(
    "settings_kw",
    [
        {"host": "0.0.0.0"},
        {"host": "127.0.0.1", "forwarded_allow_ips": "*"},
        {"host": "127.0.0.1", "root_path": "/monitor"},
    ],
)
def test_no_keys_when_exposed_disables_the_editor(settings_kw):
    client = TestClient(_app(**settings_kw))
    res = client.get("/api/settings")
    assert res.status_code == 403
    assert res.json()["detail"]["code"] == "ADMIN_DISABLED"
    page = client.get("/settings")
    assert page.status_code == 200
    assert "MONITOR_ADMIN_KEY" in page.text and "settings-app" not in page.text
    upload = client.post(
        "/api/upload/readings", files={"file": ("r.csv", b"id,line,temp\n1,A,1\n")}
    )
    assert upload.status_code == 403


def test_api_key_protects_the_editor_when_no_admin_key():
    client = TestClient(_app(api_key="user-key-123456789"))
    assert client.get("/api/settings").status_code == 401
    ok = client.get("/api/settings", headers={"X-API-Key": "user-key-123456789"})
    assert ok.status_code == 200
    page = client.get("/settings", follow_redirects=False)
    assert page.status_code == 303
    assert page.headers["location"] == "/login?next=/settings"
    client.post("/api/login", data={"key": "user-key-123456789", "next": "/settings"})
    assert client.get("/api/settings").status_code == 200


def test_admin_key_separates_admins_from_api_key_holders():
    app = _app(api_key="user-key-123456789", admin_key="admin-key-123456789")
    client = TestClient(app)
    user = {"X-API-Key": "user-key-123456789"}
    admin = {"X-API-Key": "admin-key-123456789"}
    assert client.get("/api/settings", headers=user).status_code == 401
    assert client.get("/api/settings", headers=admin).status_code == 200
    # 管理者の鍵は書き込みの API キーの代わりにもなる
    res = client.post(
        "/api/tables/readings", json={"line": "A", "temp": 1}, headers=admin
    )
    assert res.status_code == 201

    # API キーでログインしても管理の画面は開けない(管理者の鍵のログインへ)
    client.post("/api/login", data={"key": "user-key-123456789"})
    page = client.get("/settings", follow_redirects=False)
    assert page.headers["location"] == "/login?next=/settings"
    login = client.get("/login?next=/settings")
    assert "管理者の鍵" in login.text and 'value="/settings"' in login.text

    res = client.post(
        "/api/login",
        data={"key": "admin-key-123456789", "next": "/settings"},
        follow_redirects=False,
    )
    assert res.headers["location"] == "/settings"
    assert "monitor_admin_session" in client.cookies
    assert client.get("/settings").status_code == 200
    assert client.get("/api/settings").status_code == 200

    client.post("/api/logout")
    assert client.get("/api/settings").status_code == 401


def test_audit_actor_of_an_admin_login_is_the_ui():
    app = _app(admin_key="admin-key-123456789", audit_enabled=True)
    client = TestClient(app)
    client.post("/api/login", data={"key": "admin-key-123456789"})
    body = _load(client)
    body["settings"]["app_title"] = "管理者"
    assert _save(client, body, body["settings"]).status_code == 200
    entry = client.get("/api/audit", params={"table": "_settings"}).json()["entries"][0]
    assert entry["actor"].startswith("ui@")


def test_admin_session_cookie_cannot_be_replayed_after_logout():
    client = TestClient(_app(admin_key="admin-key-123456789"))
    client.post("/api/login", data={"key": "admin-key-123456789"})
    cookie = client.cookies["monitor_admin_session"]
    client.post("/api/logout")
    client.cookies.set("monitor_admin_session", cookie)
    assert client.get("/api/settings").status_code == 401


@pytest.mark.parametrize(
    "target", ["//evil.example/x", "https://evil.example", "/a/../b", "/\\evil", "x"]
)
def test_login_next_only_returns_inside_the_app(target):
    client = TestClient(_app(api_key="user-key-123456789"))
    res = client.post(
        "/api/login",
        data={"key": "user-key-123456789", "next": target},
        follow_redirects=False,
    )
    assert res.headers["location"] == "/"


@pytest.mark.parametrize(
    "headers",
    [
        {"Sec-Fetch-Site": "cross-site"},
        {"Origin": "http://evil.example"},
        {"Origin": "null"},
    ],
)
def test_cross_site_requests_are_refused_for_cookie_or_keyless_access(headers):
    client = TestClient(_app())
    body = _load(client)
    res = _save(client, body, body["settings"], **headers)
    assert res.status_code == 403
    assert res.json()["detail"]["code"] == "CROSS_SITE_REQUEST"
    upload = client.post(
        "/api/upload/readings",
        files={"file": ("r.csv", b"id,line,temp\n1,A,1\n")},
        headers=headers,
    )
    assert upload.status_code == 403


def test_same_origin_and_header_key_requests_pass_the_csrf_check():
    client = TestClient(_app(api_key="user-key-123456789"))
    key = {"X-API-Key": "user-key-123456789"}
    body = _load(client, **key)
    res = _save(
        client, body, body["settings"], **key, **{"Sec-Fetch-Site": "cross-site"}
    )
    assert res.status_code == 200  # 鍵をヘッダで送る要求はほかのサイトから作れない
    client.post("/api/login", data={"key": "user-key-123456789"})
    body = _load(client)
    res = _save(
        client,
        body,
        body["settings"],
        **{"Sec-Fetch-Site": "same-origin", "Origin": "http://testserver"},
    )
    assert res.status_code == 200


def test_settings_page_opens_from_a_link_on_another_site():
    client = TestClient(_app())
    res = client.get("/settings", headers={"Sec-Fetch-Site": "cross-site"})
    assert res.status_code == 200 and "settings-app" in res.text


def test_weak_admin_key_is_warned(caplog):
    _app(admin_key="short")
    assert "MONITOR_ADMIN_KEY が短すぎます" in caplog.text


def test_restored_readonly_after_a_failed_kpi(monkeypatch):
    app = _app(
        _config(
            kpis={
                "bad": KpiCard(title="B", query="SELECT * FROM nope"),
                "good": KpiCard(title="G", query="SELECT 1"),
            }
        )
    )
    db = app.state.db
    calls = []
    original = db.rollback_readonly
    monkeypatch.setattr(
        db, "rollback_readonly", lambda conn: calls.append(conn) or original(conn)
    )
    kpis = {k["key"]: k for k in TestClient(app).get("/api/kpis").json()["kpis"]}
    assert kpis["good"]["value"] == 1.0
    assert len(calls) == 1


def test_rollback_readonly_reapplies_per_transaction_read_only(monkeypatch):
    from monitor_app.db import engine as engine_module

    executed = []
    db = engine_module.Database("sqlite://")
    monkeypatch.setitem(
        engine_module._READONLY_SQL, "sqlite", ("SELECT 'readonly'", None)
    )
    engine = create_engine("sqlite://")
    with engine.connect() as conn:
        monkeypatch.setattr(conn, "execute", lambda stmt: executed.append(str(stmt)))
        db.rollback_readonly(conn)
    assert executed == ["SELECT 'readonly'"]
