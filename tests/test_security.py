"""埋め込み JSON の XSS 無害化(#S1)のテスト。

config 由来文字列(ビュータイトル・チャートラベル等)に ``</script>`` が含まれても、
``<script type="application/json">`` を閉じて DOM 破壊/XSS にならないことを確認する。
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from monitor_app import ChartDef, MonitorConfig, TableDef, ViewDef
from monitor_app.api.routers.pages import _escape_json_for_script
from monitor_app.main import create_app
from monitor_app.settings.declarative import DashboardConfig
from monitor_app.settings.runtime import AppSettings

EVIL = "</script><script>window.__xss=1//"


# ---------------------------------------------------------------------------
# ヘルパー単体
# ---------------------------------------------------------------------------
def test_escape_neutralizes_script_close():
    raw = json.dumps({"title": EVIL}, ensure_ascii=False)
    out = _escape_json_for_script(raw)
    # 生の </script> や < > & が残らない
    assert "</script>" not in out
    assert "<" not in out and ">" not in out and "&" not in out
    assert "\\u003c" in out  # < がエスケープされている
    # JSON としては等価(復号すると元に戻る)
    assert json.loads(out) == {"title": EVIL}


def test_escape_handles_line_separators():
    # U+2028 / U+2029 は JS 文字列を壊すためエスケープする(防御的)。
    raw = json.dumps({"x": "a\u2028b\u2029c"}, ensure_ascii=False)
    out = _escape_json_for_script(raw)
    assert "\u2028" not in out and "\u2029" not in out
    assert json.loads(out) == {"x": "a\u2028b\u2029c"}


# ---------------------------------------------------------------------------
# レンダリング統合
# ---------------------------------------------------------------------------
def _client(config: MonitorConfig) -> TestClient:
    settings = AppSettings(database_url="sqlite:///:memory:")
    return TestClient(create_app(config, settings))


def test_index_escapes_evil_view_title():
    config = MonitorConfig(
        tables={"t": TableDef(columns=["id", "v"], primary_key="id")},
        views={
            "v1": ViewDef(
                query="SELECT 1 AS id, 2 AS v",
                title=EVIL,
                chart=ChartDef(type="bar", x="id", y="v"),
            )
        },
        dashboard=DashboardConfig(views=["v1"], columns=1),
    )
    res = _client(config).get("/")
    assert res.status_code == 200
    # 生の </script> ブレイクアウトが無く、エスケープ済みで埋め込まれている
    assert EVIL not in res.text
    assert "\\u003c/script\\u003e" in res.text


def test_table_view_escapes_evil_chart_label():
    config = MonitorConfig(
        tables={"t": TableDef(columns=["id", "v"], primary_key="id")},
        views={
            "v1": ViewDef(
                query="SELECT 1 AS id, 2 AS v",
                title="ok",
                chart=ChartDef(type="bar", x="id", y="v", x_label=EVIL),
            )
        },
    )
    res = _client(config).get("/table/v1")
    assert res.status_code == 200
    assert EVIL not in res.text
    assert "\\u003c/script\\u003e" in res.text


# ---------------------------------------------------------------------------
# S2: セキュリティヘッダ
# ---------------------------------------------------------------------------
def test_security_headers_present():
    config = MonitorConfig(
        tables={"t": TableDef(columns=["id"], primary_key="id")},
        views={"v1": ViewDef(query="SELECT 1 AS id", title="v1")},
    )
    res = _client(config).get("/")
    assert res.headers["x-content-type-options"] == "nosniff"
    assert res.headers["x-frame-options"] == "SAMEORIGIN"
    assert res.headers["referrer-policy"] == "same-origin"


# ---------------------------------------------------------------------------
# 画面から保存する SQL(複数の文)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "SELECT line, AVG(tmp) FROM readings GROUP BY line",
        "SELECT 1 -- note\nFROM t",
    ],
)
def test_sql_without_semicolons_passes(sql):
    from monitor_app.security import may_contain_multiple_statements

    assert may_contain_multiple_statements(sql) is False


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1; DELETE FROM t",
        "SELECT 1;COMMIT",
        # 引用・コメントの読み方は DB ごとに違うため、どこにある ; も断る
        "SELECT ';' AS sep",
        "SELECT $$'$$; DELETE FROM t; SELECT $$'$$",
        "SELECT 'a\\' ; DELETE FROM t; SELECT '",
        # MySQL では -- の後ろに空白が要る(1 --1 は引き算で、; が文を区切る)
        "SELECT 1 --1; COMMIT; DELETE FROM t; COMMIT",
        # MySQL は /*! */ の中身を実行し、コメントを入れ子にしない
        "SELECT 1 /*! ; COMMIT; DELETE FROM t */",
        "SELECT 1 /* /* */ ; COMMIT; DELETE FROM t; -- */",
    ],
)
def test_any_semicolon_is_refused(sql):
    from monitor_app.security import may_contain_multiple_statements

    assert may_contain_multiple_statements(sql) is True


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"bound_host": "127.0.0.1"}, True),
        ({"bound_host": "localhost"}, True),
        ({"bound_host": "0.0.0.0"}, False),
        ({"bound_host": "127.0.0.1", "root_path": "/monitor"}, False),
        ({"bound_host": "127.0.0.1", "forwarded_allow_ips": "*"}, False),
    ],
)
def test_serves_this_machine_only(kwargs, expected):
    from monitor_app.security import serves_this_machine_only

    assert serves_this_machine_only(**kwargs) is expected
