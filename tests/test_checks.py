"""設定・データ・実行環境の点検(``monitor_app.checks``、#DX-07 / #DX-08 / #CRIT-04)。"""

from __future__ import annotations

import sqlite3
import textwrap
from contextlib import closing
from pathlib import Path

import pytest
from contract import helpers, snapshot

from monitor_app import (
    AlertRule,
    CellStyle,
    ChartDef,
    KpiCard,
    MonitorConfig,
    SourceDef,
    TableDef,
    ViewDef,
)
from monitor_app.checks import (
    Issue,
    check_project,
    has_errors,
    is_loopback_host,
    run_checks,
    summarize,
)
from monitor_app.settings.loader import load_config
from monitor_app.settings.runtime import AppSettings, find_unknown_env_keys

SCAFFOLD = Path(__file__).parent.parent / "monitor_app" / "scaffold"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    helpers.isolate_from_host(monkeypatch, tmp_path)


def _settings(**overrides) -> AppSettings:
    overrides.setdefault("database_url", "sqlite://")
    return AppSettings(_env_file=None, **overrides)


def _codes(issues: list[Issue]) -> list[tuple[str, str]]:
    return [(i.level, i.code) for i in issues]


def _base(**overrides) -> MonitorConfig:
    values = dict(
        tables={
            "sensors": TableDef(
                columns={"id": "int", "ts": "datetime", "temp": "float", "line": "str"}
            )
        },
        views={"v": ViewDef(query="SELECT ts, temp, line FROM sensors")},
    )
    values.update(overrides)
    return MonitorConfig(**values)


# ---------------------------------------------------------------------------
# 問題のない設定
# ---------------------------------------------------------------------------
def test_shipped_configs_have_no_problems():
    for path in (SCAFFOLD / "config.py", snapshot.GOLDEN_PROJECT / "config.py"):
        issues = run_checks(load_config(path), _settings())
        assert issues == [], (path, issues)


def test_scaffold_csvs_match_the_scaffold_config(monkeypatch):
    monkeypatch.chdir(SCAFFOLD)
    issues = run_checks(load_config(SCAFFOLD / "config.py"), _settings(), data=True)
    assert [i for i in issues if i.level != "info"] == [], issues


# ---------------------------------------------------------------------------
# SQL の空実行と列の照合
# ---------------------------------------------------------------------------
def test_sql_typos_are_errors_with_suggestions():
    config = _base(
        views={"v": ViewDef(query="SELECT * FROM sensor")},
        kpis={"k": KpiCard(title="k", query="SELECT AVG(tmp) FROM sensors")},
    )
    issues = run_checks(config, _settings())
    assert _codes(issues) == [("error", "sql-error"), ("error", "sql-error")]
    assert "no such table: sensor" in issues[0].message
    assert issues[0].hint == "'sensors' の誤りではありませんか?"
    assert issues[1].hint == "'temp' の誤りではありませんか?"
    assert has_errors(issues)


def test_columns_of_alerts_charts_styles_and_labels_are_checked():
    config = _base(
        views={
            "v": ViewDef(
                query="SELECT ts, temp FROM sensors",
                chart=ChartDef(x="ts", y=["temp", "humidity"]),
                styles={"tmp": CellStyle(bold=True)},
                labels={"temp": "温度", "ts2": "時刻"},
            )
        },
        alerts=[AlertRule(view="v", column="Temp", op=">", value=30)],
    )
    issues = run_checks(config, _settings())
    assert _codes(issues) == [
        ("error", "chart-column"),
        ("error", "alert-column"),
        ("warning", "style-column"),
        ("warning", "label-column"),
    ]
    assert "このアラートは発火しません" in issues[1].message
    assert issues[1].hint == "'temp' の誤りではありませんか?"


def test_non_sqlite_databases_downgrade_sql_errors_to_warnings():
    config = _base(views={"v": ViewDef(query="SELECT NOW() AS t FROM sensors")})
    issues = run_checks(
        config, _settings(database_url="postgresql+psycopg://u:p@db.invalid/x")
    )
    assert _codes(issues) == [("warning", "sql-error")]
    assert "postgresql では問題ない可能性" in issues[0].message


def test_query_that_cannot_be_wrapped_is_reported_but_columns_still_checked():
    config = _base(
        views={
            "v": ViewDef(
                query="SELECT ts, temp FROM sensors -- newest",
                chart=ChartDef(x="ts", y="tmp"),
            )
        }
    )
    issues = run_checks(config, _settings())
    assert _codes(issues) == [("error", "chart-column"), ("warning", "sql-limit-wrap")]


def test_internal_tables_and_tables_only_in_the_db_file(tmp_path):
    db_path = tmp_path / "app.db"
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("CREATE TABLE legacy (id INTEGER, note TEXT)")
    config = _base(
        views={
            "audit": ViewDef(query="SELECT table_name, action FROM _audit"),
            "legacy": ViewDef(query="SELECT id, note FROM legacy"),
            "legacy_note": ViewDef(query="SELECT note FROM legacy -- tail comment"),
            "ghost": ViewDef(query="SELECT * FROM ghost"),
        },
        alerts=[AlertRule(view="legacy", column="nope", op=">", value=1)],
    )
    issues = run_checks(config, _settings(database_url=helpers.sqlite_url(db_path)))
    assert _codes(issues) == [
        ("error", "sql-error"),  # ghost は DB にも無い
        ("error", "alert-column"),  # legacy の列は DB から取れた
        ("warning", "sql-limit-wrap"),  # DB にある表でも、包めない SQL は知らせる
        ("info", "sql-external-table"),
        ("info", "sql-external-table"),
    ]


def test_schema_that_cannot_be_created_is_one_error():
    config = MonitorConfig(
        tables={
            "Users": TableDef(columns={"id": "int"}),
            "users": TableDef(columns={"id": "int"}),
        }
    )
    assert _codes(run_checks(config, _settings())) == [("error", "schema")]


def test_kpi_format_is_checked():
    config = _base(kpis={"k": KpiCard(title="k", query="SELECT 1", format="{:.1x}")})
    assert _codes(run_checks(config, _settings())) == [("warning", "kpi-format")]


def test_v1_default_favicon_is_reported():
    """v1 の既定値 'favicon.ico' は画面では img/favicon.ico で表示する(情報として知らせる)。"""
    issues = run_checks(load_config(snapshot.LEGACY_V1_CONFIG), _settings())
    assert _codes(issues) == [("info", "favicon-fallback")]
    assert "'img/favicon.ico'" in issues[0].hint


def test_missing_favicon_is_a_warning():
    issues = run_checks(_base(favicon_path="img/nothing-here.png"), _settings())
    assert _codes(issues) == [("warning", "favicon-missing")]
    assert "img/favicon.ico" in issues[0].hint


# ---------------------------------------------------------------------------
# セキュリティ・実行時設定
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "host, loopback",
    [
        ("127.0.0.1", True),
        ("::1", True),
        ("[::1]", True),
        ("localhost", True),
        ("0.0.0.0", False),
        ("192.168.1.5", False),
        ("factory-pc", False),
    ],
)
def test_is_loopback_host(host, loopback):
    assert is_loopback_host(host) is loopback


def test_lan_bind_without_api_key_warns():
    config = _base()
    assert _codes(run_checks(config, _settings(host="0.0.0.0"))) == [
        ("warning", "insecure-bind")
    ]
    assert run_checks(config, _settings(host="0.0.0.0", api_key="k" * 32)) == []
    assert _codes(run_checks(config, _settings(protect_reads=True))) == [
        ("warning", "protect-reads-without-key")
    ]


def test_bad_database_url_and_encodings_are_errors():
    issues = run_checks(
        _base(),
        _settings(
            database_url="not a url",
            csv_encoding="shift-jiss",
            csv_encoding_fallback="cp932, nope",
        ),
    )
    assert _codes(issues) == [
        ("error", "database-url"),
        ("error", "csv-encoding-name"),
        ("error", "csv-encoding-name"),
    ]


def test_unknown_env_keys_in_environment_and_dotenv(tmp_path, monkeypatch):
    monkeypatch.setenv("MONITOR_DATABSE_URL", "sqlite://")
    monkeypatch.setenv("MONITOR_DEBUG", "1")  # CLI が読む変数
    monkeypatch.setenv("MONITOR_TEST_PG_URL", "x")  # テスト用の変数
    monkeypatch.setenv("PATH_LIKE_OTHER_TOOL", "1")
    (tmp_path / ".env").write_text(
        "MONITOR_API_KYE=secret\n"
        "MONTOR_PORT=9999\n"  # 接頭辞の綴りの誤り
        "MONITOR_PORT=9990\n"
        "OTHER_TOOL=1\n"
        "monitor_log_level=DEBUG\n",  # 大文字小文字は区別しない
        encoding="utf-8",
    )
    found = find_unknown_env_keys()
    assert [(f.key, f.suggestion) for f in found] == [
        ("MONITOR_DATABSE_URL", "MONITOR_DATABASE_URL"),
        ("MONITOR_API_KYE", "MONITOR_API_KEY"),
        ("MONTOR_PORT", "MONITOR_PORT"),
    ]
    assert found[0].source == "環境変数" and found[1].source == ".env"

    issues = run_checks(_base(), _settings())
    assert _codes(issues) == [
        ("error", "env-unknown-key"),  # 認証の設定の綴りの誤りはエラー
        ("warning", "env-unknown-key"),
        ("warning", "env-unknown-key"),
    ]
    assert "認証が有効になりません" in issues[0].message
    assert issues[0].hint == "MONITOR_API_KEY の誤りではありませんか?"


def test_auth_typo_is_only_a_warning_when_auth_is_configured(tmp_path):
    (tmp_path / ".env").write_text("MONITOR_API_KYE=old\n", encoding="utf-8")
    issues = run_checks(_base(), _settings(api_key="k" * 32))
    assert _codes(issues) == [("warning", "env-unknown-key")]


def test_unreadable_env_file_is_ignored(tmp_path):
    (tmp_path / ".env").write_bytes(b"MONITOR_X=\xff\xfe\n")
    assert find_unknown_env_keys(environ={}) == []


# ---------------------------------------------------------------------------
# データ(CSV)
# ---------------------------------------------------------------------------
def _data_project(tmp_path: Path) -> Path:
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()
    (csv_dir / "sensors.csv").write_text(
        "ID,ts,Temp,extra\n1,2026-10-21T08:00:00,20.5,x\n", encoding="utf-8"
    )
    (csv_dir / "m.csv").write_text(
        "id,temp,qty,price,flag\n1,9.5,3,abc,0\n2,10.25,100,5,1\n", encoding="utf-8"
    )
    (csv_dir / "jp.csv").write_bytes("id,名前\n1,山田\n".encode("cp932"))
    (csv_dir / "Users.csv").write_text("id\n1\n", encoding="utf-8")
    return csv_dir


def test_data_checks(tmp_path):
    csv_dir = _data_project(tmp_path)
    config = MonitorConfig(
        tables={
            "sensors": TableDef(
                columns={"id": "int", "ts": "str", "temp": "float", "line": "str"}
            ),
            "m": TableDef(columns=["id", "temp", "qty", "price", "flag"]),
            "jp": TableDef(columns={"id": "int", "名前": "str"}),
            "users": TableDef(columns={"id": "int"}),
            "events": TableDef(columns={"id": "int"}),
            "fed": TableDef(columns={"id": "int"}),
        },
        sources={"src": SourceDef(kind="jsonl", path="missing.jsonl", table="fed")},
    )
    issues = run_checks(
        config, _settings(csv_dir=csv_dir, csv_encoding="auto"), data=True
    )
    messages = {(i.level, i.code): i for i in issues}
    found = _codes(issues)
    # m.temp / m.qty / m.flag は列名から str と推論されるが数値(DX-08)
    numeric = [i for i in issues if i.code == "csv-numeric-as-str"]
    assert [i.message.split(" ")[0] for i in numeric] == ["m.temp", "m.qty", "m.flag"]
    assert (
        numeric[0].hint == 'columns に {"temp": "float"} のように型を明示してください'
    )
    # m.price は列名から float だが 'abc' がある
    mismatch = messages[("warning", "csv-type-mismatch")]
    assert "'price'" in mismatch.message and "2 行目 'abc'" in mismatch.message
    # sensors: 見出しの表記ゆれ(ID → id, Temp → temp)・欠けた列・余分な列
    assert ("info", "csv-header-variant") in found
    missing = messages[("warning", "csv-header-missing")]
    assert "'line'" in missing.message
    assert ("info", "csv-header-unknown") in found
    # jp.csv は cp932 として読む / users は Users.csv(名前の大文字小文字違い)
    assert (
        "jp.csv は UTF-8 ではないため cp932"
        in messages[("info", "csv-encoding")].message
    )
    missing_csv = [i.message for i in issues if i.code == "csv-missing"]
    if (csv_dir / "users.csv").exists():
        # 大文字・小文字を区別しないファイルシステム(Windows・macOS の既定)では
        # Users.csv が users.csv として取り込まれる
        assert ("info", "csv-unmatched") not in found
        assert len(missing_csv) == 1 and "'events'" in missing_csv[0]
    else:
        unmatched = messages[("info", "csv-unmatched")]
        assert "Users.csv" in unmatched.message and "users.csv" in unmatched.hint
        # events.csv と users.csv が無い(fed はソースから取り込むので対象外)
        assert len(missing_csv) == 2
        assert all("'events'" in m or "'users'" in m for m in missing_csv)
    assert ("info", "source-missing") in found
    assert not has_errors(issues)


def test_explicit_encoding_that_cannot_read_the_file_is_an_error(tmp_path):
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()
    (csv_dir / "jp.csv").write_bytes("id,名前\n1,山田\n".encode("cp932"))
    config = MonitorConfig(
        tables={"jp": TableDef(columns={"id": "int", "名前": "str"})}
    )
    issues = run_checks(
        config, _settings(csv_dir=csv_dir, csv_encoding="utf-8"), data=True
    )
    assert _codes(issues) == [("error", "csv-unreadable")]
    assert "MONITOR_CSV_ENCODING=utf-8" in issues[0].hint


def test_utf8_file_read_as_cp932_warns_about_mojibake(tmp_path):
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()
    # UTF-8 の「山田」は cp932 としても(文字化けして)読めてしまう
    (csv_dir / "u.csv").write_text("id,名前\n1,山田\n", encoding="utf-8")
    config = MonitorConfig(tables={"u": TableDef(columns={"id": "int", "名前": "str"})})
    issues = run_checks(
        config, _settings(csv_dir=csv_dir, csv_encoding="cp932"), data=True
    )
    assert ("warning", "csv-encoding") in _codes(issues)


def test_missing_csv_dir(tmp_path):
    config = MonitorConfig(tables={"t": TableDef(columns={"id": "int"})})
    issues = run_checks(config, _settings(csv_dir=tmp_path / "nope"), data=True)
    assert _codes(issues) == [("warning", "csv-dir-missing")]


# ---------------------------------------------------------------------------
# check_project / 表示
# ---------------------------------------------------------------------------
def test_check_project_includes_typo_warnings(tmp_path):
    path = tmp_path / "config.py"
    path.write_text(
        textwrap.dedent("""
            from monitor_app import MonitorConfig, TableDef, ViewDef
            config = MonitorConfig(
                tables={"t": TableDef(columns={"id": "int"}, primay_key="id")},
                views={"v": ViewDef(query="SELECT * FROM tt")},
            )
            """),
        encoding="utf-8",
    )
    config, issues = check_project(path, _settings())
    assert list(config.tables) == ["t"]
    assert _codes(issues) == [("error", "sql-error"), ("warning", "config-unknown-key")]
    assert issues[1].message.startswith("config.py 4 行目: TableDef の未知のキー")


def test_summary():
    issues = [
        Issue("error", "x", "壊れています", "直してください"),
        Issue("info", "y", "参考"),
    ]
    assert summarize(issues) == "エラー 1 件・警告 0 件・情報 1 件"


def test_a_failing_stage_does_not_hide_the_others(monkeypatch):
    def boom(*args):
        raise RuntimeError("壊れた")

    monkeypatch.setattr("monitor_app.checks._static_issues", boom)
    config = _base(views={"v": ViewDef(query="SELECT * FROM nope")})
    issues = run_checks(config, _settings())
    assert _codes(issues) == [("error", "sql-error"), ("warning", "check-failed")]
    assert "RuntimeError: 壊れた" in issues[1].message


def test_foreign_key_to_a_missing_column_is_one_schema_error():
    config = MonitorConfig(
        tables={
            "users": TableDef(columns={"id": "int"}),
            "orders": TableDef(
                columns={"id": "int", "user_id": "int"},
                foreign_keys={"user_id": "users.idx"},
            ),
        }
    )
    issues = run_checks(config, _settings())
    assert _codes(issues) == [("error", "schema")]
    assert "foreign_keys" in issues[0].hint


def test_missing_non_integer_primary_key_is_a_warning(tmp_path):
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()
    (csv_dir / "parts.csv").write_text("name\nbolt\n", encoding="utf-8")
    config = MonitorConfig(
        tables={
            "parts": TableDef(
                columns={"code": "str", "name": "str"}, primary_key="code"
            )
        }
    )
    issues = run_checks(config, _settings(csv_dir=csv_dir), data=True)
    assert _codes(issues) == [("warning", "csv-header-missing")]
    assert "自動採番されない" in issues[0].message


# ---------------------------------------------------------------------------
# レビューで見つかった問題の回帰テスト
# ---------------------------------------------------------------------------
def test_columns_are_compared_with_the_names_the_app_sees():
    """SQLite は副問い合わせで包むと列名が変わる(name → 宣言の Name ではなく name)。

    画面・アラートは包まずに実行した結果の列名を使うため、そちらと照合する。
    """
    tables = {
        "t": TableDef(columns={"id": "int", "Name": "str", "temp": "float"}),
        "u": TableDef(columns={"id": "int", "t_id": "int"}),
    }
    ok = MonitorConfig(
        tables=tables,
        views={
            "v": ViewDef(query="SELECT id, name FROM t"),
            "j": ViewDef(
                query="SELECT t.id, u.id FROM t JOIN u ON u.t_id = t.id",
                chart=ChartDef(x="id", y="id"),
            ),
        },
        alerts=[AlertRule(view="v", column="Name", op=">", value=1)],
    )
    assert run_checks(ok, _settings()) == []
    wrong = ok.model_copy(
        update={"alerts": [AlertRule(view="v", column="name", op=">", value=1)]}
    )
    issues = run_checks(wrong, _settings())
    assert _codes(issues) == [("error", "alert-column")]
    assert issues[0].hint == "'Name' の誤りではありませんか?"


def test_max_rows_views_also_accept_the_wrapped_column_names():
    config = MonitorConfig(
        tables={"t": TableDef(columns={"id": "int", "Name": "str"})},
        views={"v": ViewDef(query="SELECT id, name FROM t", max_rows=100)},
        alerts=[AlertRule(view="v", column="name", op=">", value=1)],
    )
    assert run_checks(config, _settings()) == []


def test_kpi_queries_are_not_wrapped():
    """KPI は副問い合わせで包まずに実行されるため、末尾の -- コメントは問題ない。"""
    config = _base(
        kpis={"k": KpiCard(title="k", query="SELECT COUNT(*) FROM sensors -- 件数")}
    )
    assert run_checks(config, _settings()) == []
    broken = _base(kpis={"k": KpiCard(title="k", query="SELECT COUNT(*) FROM nope")})
    assert _codes(run_checks(broken, _settings())) == [("error", "sql-error")]


def test_column_problems_are_warnings_on_other_databases():
    """PostgreSQL は引用符の無い別名を小文字にする(avgTemp → avgtemp)。"""
    config = _base(
        views={
            "v": ViewDef(
                query="SELECT ts, AVG(temp) AS avgTemp FROM sensors GROUP BY ts",
                chart=ChartDef(x="ts", y="avgtemp"),
            )
        },
        alerts=[AlertRule(view="v", column="avgtemp", op=">", value=30)],
    )
    pg = _settings(database_url="postgresql+psycopg://u:p@db.invalid/x")
    issues = run_checks(config, pg)
    assert _codes(issues) == [("warning", "chart-column"), ("warning", "alert-column")]
    assert all("postgresql では問題ない可能性" in i.message for i in issues)
    assert _codes(run_checks(config, _settings())) == [
        ("error", "chart-column"),
        ("error", "alert-column"),
    ]


def test_endless_queries_are_cut_off(monkeypatch):
    import time

    from monitor_app import checks

    monkeypatch.setattr(checks, "_DRY_RUN_TIMEOUT", 0.2)
    endless = (
        "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) "
        "SELECT COUNT(*) AS n FROM c"
    )
    config = _base(
        views={
            "v": ViewDef(query=endless),
            "w": ViewDef(query=endless + " -- 包むと失敗する"),
        },
        kpis={"k": KpiCard(title="k", query=endless)},
        alerts=[AlertRule(view="v", column="n", op=">", value=1)],
    )
    started = time.monotonic()
    issues = run_checks(config, _settings())
    assert time.monotonic() - started < 10
    assert _codes(issues) == [("warning", "sql-limit-wrap")]


def test_colon_parameters_get_a_hint_without_sqlalchemy_noise():
    config = _base(
        views={"v": ViewDef(query="SELECT json_extract('{\"a\" :1}', '$.a') AS a")}
    )
    (issue,) = run_checks(config, _settings())
    assert issue.code == "sql-error"
    assert "Background on this error" not in issue.message
    assert "\\:" in issue.hint


def test_type_mismatch_is_confirmed_with_the_importer_conversion(tmp_path):
    """推論では int にしない 007 も、取り込み(int())は 7 として受け付ける。"""
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()
    (csv_dir / "parts.csv").write_text(
        "id,code,qty\n1,007,3\n2,12,x\n3,0042,4\n", encoding="utf-8"
    )
    config = MonitorConfig(
        tables={"parts": TableDef(columns={"id": "int", "code": "int", "qty": "int"})}
    )
    issues = run_checks(config, _settings(csv_dir=csv_dir), data=True)
    by_code = {i.code: i for i in issues}
    assert _codes(issues) == [
        ("warning", "csv-leading-zeros"),
        ("warning", "csv-type-mismatch"),
    ]
    assert "'qty'" in by_code["csv-type-mismatch"].message
    assert "3 行目 'x'" in by_code["csv-type-mismatch"].message
    leading = by_code["csv-leading-zeros"]
    assert "'code'" in leading.message and "2 行目 '007'" in leading.message


def test_csv_with_other_letter_case_is_not_unmatched_when_the_fs_ignores_case(
    tmp_path,
):
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()
    (csv_dir / "Sensors.CSV").write_text("id\n1\n", encoding="utf-8")
    if not (csv_dir / "sensors.csv").exists():
        pytest.skip("大文字・小文字を区別するファイルシステム")
    config = MonitorConfig(tables={"sensors": TableDef(columns={"id": "int"})})
    issues = run_checks(config, _settings(csv_dir=csv_dir), data=True)
    assert "csv-unmatched" not in [i.code for i in issues]
