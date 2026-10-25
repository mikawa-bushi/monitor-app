"""``monitor-app check``(点検の結果の表示・終了コード・表定義のずれ)と ``migrate``。

check は設定(未知のキー)・SQL・実行時設定(未知の MONITOR_*)・CSV(--data)・DB の
表定義を点検し、重要度ごとにまとめて表示する。エラーがあれば終了コード 1、--strict なら
警告でも 1。DB は読み取り専用で開き、点検では変えない。
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest
from contract import helpers
from typer.testing import CliRunner

from monitor_app.cli import app

SECRET = "s3cret-pw"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    helpers.isolate_from_host(monkeypatch, tmp_path)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def project(tmp_path, monkeypatch) -> Path:
    """startproject の雛形(作業フォルダもそこへ移す)。"""
    proj = helpers.start_project(tmp_path)
    monkeypatch.chdir(proj)
    return proj


def _db(proj: Path) -> Path:
    return proj / "instances" / "database.db"


def _import(runner: CliRunner) -> None:
    result = runner.invoke(app, ["import-csv"])
    assert result.exit_code == 0, result.output


def _edit(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, old
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def _add_phone_column(proj: Path) -> None:
    _edit(
        proj / "config.py",
        'columns={"id": "int", "name": "str", "email": "str"},',
        'columns={"id": "int", "name": "str", "email": "str", "phone": "str"},',
    )


def _columns(db: Path, table: str) -> list[str]:
    with closing(sqlite3.connect(db)) as conn:
        return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


# ---------------------------------------------------------------------------
# 表示と終了コード
# ---------------------------------------------------------------------------
def test_clean_project_prints_only_the_ok_line(project, runner):
    result = runner.invoke(app, ["check"])
    assert result.exit_code == 0, result.output
    assert result.stdout == "✅ OK — テーブル 3 件 / ビュー 3 件\n"


def test_warnings_are_grouped_with_hints_and_keep_exit_code_zero(project, runner):
    _edit(project / "config.py", 'primary_key="id"', 'primary_key="id", primay_key=1')
    (project / ".env").write_text("MONITOR_PORTT=1\n", encoding="utf-8")
    result = runner.invoke(app, ["check"])
    assert result.exit_code == 0, result.output
    out = result.stdout
    assert "警告(2 件):" in out
    assert "TableDef の未知のキー 'primay_key'" in out
    assert "'primary_key' の誤りではありませんか?" in out
    assert ".env の MONITOR_PORTT は monitor-app の設定ではありません" in out
    assert "    ヒント: MONITOR_PORT の誤りではありませんか?" in out
    # 成功の行は従来どおり(件数の要約を添える)
    assert out.rstrip().endswith(
        "✅ OK — テーブル 3 件 / ビュー 3 件(エラー 0 件・警告 2 件・情報 0 件)"
    )


def test_strict_turns_warnings_into_a_failure(project, runner):
    (project / ".env").write_text("MONITOR_PORTT=1\n", encoding="utf-8")
    result = runner.invoke(app, ["check", "--strict"])
    assert result.exit_code == 1
    assert "警告(1 件):" in result.stdout
    assert "--strict のため、警告があると失敗にします" in result.stderr
    assert "✅ OK" not in result.output


def test_strict_ignores_info_only_issues(project, runner):
    # CSV の無いテーブルは「情報」: --strict でも失敗にしない
    (project / "csv" / "orders.csv").unlink()
    result = runner.invoke(app, ["check", "--data", "--strict"])
    assert result.exit_code == 0, result.output
    assert "情報(1 件):" in result.stdout
    assert "orders.csv がないため" in result.stdout


def test_errors_fail_and_are_listed_before_warnings(project, runner):
    _edit(project / "config.py", 'column="amount"', 'column="amout"')
    (project / ".env").write_text("MONITOR_PORTT=1\n", encoding="utf-8")
    result = runner.invoke(app, ["check"])
    assert result.exit_code == 1
    out = result.stdout
    assert out.index("エラー(1 件):") < out.index("警告(1 件):")
    assert "このアラートは発火しません" in out
    assert "ヒント: 'amount' の誤りではありませんか?" in out
    # 結果の一覧は標準出力、失敗の行は標準エラー
    assert "❌ 問題があります(エラー 1 件・警告 1 件・情報 0 件)" in result.stderr
    assert "✅ OK" not in result.output


def test_mistyped_api_key_variable_is_an_error(project, runner):
    (project / ".env").write_text("MONITOR_API_KYE=abc\n", encoding="utf-8")
    result = runner.invoke(app, ["check"])
    assert result.exit_code == 1
    assert "このままでは認証が有効になりません" in result.stdout
    assert "MONITOR_API_KEY の誤りではありませんか?" in result.stdout


def test_data_option_checks_the_csv_headers(project, runner):
    (project / "csv" / "users.csv").write_text("id,name\n1,a\n", encoding="utf-8")
    assert "email" not in runner.invoke(app, ["check"]).stdout  # --data なしは読まない
    result = runner.invoke(app, ["check", "--data"])
    assert result.exit_code == 0, result.output
    assert "users.csv に列 'email' がありません" in result.stdout


def test_invalid_setting_value_is_a_short_error(project, runner, monkeypatch):
    monkeypatch.setenv("MONITOR_PORT", "abc")
    result = runner.invoke(app, ["check"])
    assert result.exit_code == 1
    assert "MONITOR_PORT='abc'(整数を指定してください)" in result.stderr
    assert "Traceback" not in result.output
    assert len(result.stderr.strip().splitlines()) <= 3


def test_broken_config_is_reported_briefly(project, runner):
    (project / "config.py").write_text("x = 1\n", encoding="utf-8")
    result = runner.invoke(app, ["check"])
    assert result.exit_code == 1
    assert "MonitorConfig" in result.stderr
    assert result.stdout == ""


# ---------------------------------------------------------------------------
# 表定義のずれ(DB)
# ---------------------------------------------------------------------------
def test_missing_database_is_skipped_and_not_created(project, runner):
    (project / "instances" / ".gitkeep").unlink()
    (project / "instances").rmdir()
    result = runner.invoke(app, ["check"])
    assert result.exit_code == 0, result.output
    assert not (project / "instances").exists()  # 点検で DB もフォルダも作らない


def test_missing_column_is_a_warning_with_the_migrate_hint(project, runner):
    _import(runner)
    _add_phone_column(project)
    result = runner.invoke(app, ["check"])
    assert result.exit_code == 0, result.output
    assert "テーブル 'users' に列 'phone' がありません" in result.stdout
    assert "ALTER TABLE users ADD COLUMN phone" in result.stdout
    assert "ヒント: monitor-app migrate で追加できます" in result.stdout
    assert runner.invoke(app, ["check", "--strict"]).exit_code == 1


def test_new_table_is_reported_as_information(project, runner):
    _import(runner)
    _edit(
        project / "config.py",
        "    tables={\n",
        '    tables={\n        "notes": TableDef(columns={"id": "int", "body": "str"}),\n',
    )
    result = runner.invoke(app, ["check", "--strict"])
    assert result.exit_code == 0, result.output
    assert "テーブル 'notes' はまだデータベースにありません" in result.stdout


def test_check_does_not_change_the_database(project, runner):
    # v2.3.1 までの DB と同じ rollback ジャーナルの DB(WAL にしない・-wal を作らない)
    _import(runner)
    db = _db(project)
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("PRAGMA journal_mode = DELETE")
    _add_phone_column(project)
    before = db.read_bytes()
    result = runner.invoke(app, ["check"])
    assert result.exit_code == 0, result.output
    assert "列 'phone' がありません" in result.stdout
    assert db.read_bytes() == before
    assert not Path(f"{db}-wal").exists()
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"


def test_fix_schema_adds_the_missing_column_after_printing_the_sql(project, runner):
    _import(runner)
    _add_phone_column(project)
    result = runner.invoke(app, ["check", "--fix-schema", "--strict"])
    assert result.exit_code == 0, result.output
    out = result.stdout
    assert out.index("ALTER TABLE users ADD COLUMN phone") < out.index(
        "✅ 1 列を追加しました"
    )
    assert "列 'phone' がありません" not in out  # 追加した後の状態で点検する
    assert "phone" in _columns(_db(project), "users")


def test_unreachable_server_database_is_a_warning_without_the_password(
    project, runner, monkeypatch
):
    monkeypatch.setenv(
        "MONITOR_DATABASE_URL", f"postgresql+pg8000://monitor:{SECRET}@127.0.0.1:1/db"
    )
    result = runner.invoke(app, ["check"])
    assert result.exit_code == 0, result.output
    assert "データベースの表定義を読めないため、config.py との照合を省きました" in (
        result.stdout
    )
    assert SECRET not in result.output


def test_file_that_is_not_a_database_is_a_warning(project, runner):
    _db(project).write_bytes(b"not a database" * 100)
    result = runner.invoke(app, ["check"])
    assert result.exit_code == 0, result.output
    assert "file is not a database" in result.stdout


def test_check_output_survives_a_cp932_terminal(project):
    """日本語版 Windows でリダイレクトした出力(cp932)でも、まとめた表示で落ちない。"""
    (project / ".env").write_text("MONITOR_PORTT=1\n", encoding="utf-8")
    env = helpers.child_env(PYTHONIOENCODING="cp932", PYTHONUTF8="0")
    proc = helpers.run_cli("check", "--data", cwd=project, env=env)
    assert proc.returncode == 0, proc.stderr.decode("cp932", "replace")
    out = proc.stdout.decode("cp932")
    assert "警告(1 件):" in out
    assert "MONITOR_PORT の誤りではありませんか?" in out
    # cp932 に無い ✅ と — は \\uXXXX で出る(処理は成功のまま)
    assert "\\u2705 OK \\u2014 テーブル 3 件 / ビュー 3 件" in out


def test_python_m_check_fails_with_exit_code_one_on_errors(project):
    _edit(project / "config.py", 'column="amount"', 'column="amout"')
    env = helpers.child_env(PYTHONIOENCODING="cp932", PYTHONUTF8="0")
    proc = subprocess.run(
        [sys.executable, "-m", "monitor_app", "check"],
        cwd=project,
        env=env,
        capture_output=True,
        timeout=60,
    )
    assert proc.returncode == 1
    assert "このアラートは発火しません" in proc.stdout.decode("cp932")
    assert "Traceback" not in proc.stderr.decode("cp932", "replace")


# ---------------------------------------------------------------------------
# migrate
# ---------------------------------------------------------------------------
def test_migrate_without_a_database_needs_nothing(project, runner):
    result = runner.invoke(app, ["migrate"])
    assert result.exit_code == 0, result.output
    assert "データベースはまだありません" in result.stdout
    assert not _db(project).exists()


def test_migrate_dry_run_prints_the_sql_without_changing_anything(project, runner):
    _import(runner)
    _add_phone_column(project)
    before = _db(project).read_bytes()
    result = runner.invoke(app, ["migrate", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "追加する列(1 件):" in result.stdout
    assert "  ALTER TABLE users ADD COLUMN phone VARCHAR(255)" in result.stdout
    assert "--dry-run のため変更していません" in result.stdout
    assert _db(project).read_bytes() == before


def test_migrate_adds_columns_and_is_idempotent(project, runner):
    _import(runner)
    _add_phone_column(project)
    first = runner.invoke(app, ["migrate"])
    assert first.exit_code == 0, first.output
    assert "✅ 1 列を追加しました" in first.stdout
    assert "phone" in _columns(_db(project), "users")
    # 既存の行は残り、足した列は空
    with closing(sqlite3.connect(_db(project))) as conn:
        assert conn.execute("SELECT COUNT(*), MAX(phone) FROM users").fetchone() == (
            5,
            None,
        )
    second = runner.invoke(app, ["migrate"])
    assert second.exit_code == 0, second.output
    assert "✅ データベースの表定義は config.py と一致しています" in second.stdout


def test_migrate_lists_the_changes_it_cannot_make(project, runner):
    _import(runner)
    _add_phone_column(project)
    _edit(project / "config.py", '"amount": "float"', '"amount": "int"')
    result = runner.invoke(app, ["migrate"])
    assert result.exit_code == 0, result.output
    out = result.stdout
    assert "✅ 1 列を追加しました" in out
    assert "手動での対応が必要なもの(1 件):" in out
    assert "テーブル 'orders' の列 'amount' の型が一致しません" in out


def test_migrate_reports_a_table_without_a_primary_key(project, runner):
    # v0.x(pandas の to_sql)で作った、主キーの無い表
    _db(project).parent.mkdir(exist_ok=True)
    with closing(sqlite3.connect(_db(project))) as conn:
        conn.execute("CREATE TABLE users (id INTEGER, name TEXT, email TEXT)")
    result = runner.invoke(app, ["migrate"])
    assert result.exit_code == 0, result.output
    assert "テーブル 'users' に主キーがありません" in result.stdout
    assert "追加する列はありません" in result.stdout


def test_migrate_on_an_in_memory_database(project, runner, monkeypatch):
    monkeypatch.setenv("MONITOR_DATABASE_URL", "sqlite://")
    result = runner.invoke(app, ["migrate"])
    assert result.exit_code == 0, result.output
    assert "メモリ上のデータベースは移行の対象外です" in result.stdout


def test_migrate_with_an_invalid_url_is_a_short_error(project, runner, monkeypatch):
    monkeypatch.setenv("MONITOR_DATABASE_URL", f"not a url {SECRET}")
    result = runner.invoke(app, ["migrate"])
    assert result.exit_code == 1
    assert "MONITOR_DATABASE_URL の形式が正しくありません" in result.stderr
    assert SECRET not in result.output


def test_migrate_needs_a_config(tmp_path, runner):
    result = runner.invoke(app, ["migrate"])
    assert result.exit_code == 1
    assert "config.py が見つかりません" in result.stderr


def test_check_and_migrate_accept_the_project_folder(project, runner):
    _import(runner)
    _add_phone_column(project)
    for command in (["check", "--config", str(project)], ["migrate", "--config", "."]):
        result = runner.invoke(app, command)
        assert result.exit_code == 0, result.output
    assert "phone" in _columns(_db(project), "users")


# ---------------------------------------------------------------------------
# アラートの通知先
# ---------------------------------------------------------------------------
def test_alert_channels_that_cannot_deliver_are_warned(project, runner, monkeypatch):
    _edit(project / "config.py", 'notify=["console"]', 'notify=["email", "slack"]')
    result = runner.invoke(app, ["check"])
    assert result.exit_code == 0, result.output
    out = result.stdout
    assert (
        "alerts[0] の通知先 'email': MONITOR_SMTP_HOST と MONITOR_ALERT_EMAIL_TO が"
        "未設定のため送れません" in out
    )
    assert "alerts[0] の通知先 'slack': 未知のチャネルです" in out
    monkeypatch.setenv("MONITOR_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("MONITOR_ALERT_EMAIL_TO", "ops@example.com")
    result = runner.invoke(app, ["check"])
    assert "通知先 'email'" not in result.stdout
    assert "通知先 'slack'" in result.stdout


# ---------------------------------------------------------------------------
# 接続 URL の書き方ごとの点検
# ---------------------------------------------------------------------------
def test_uri_style_sqlite_url_is_inspected_read_only(project, runner, monkeypatch):
    _import(runner)
    _add_phone_column(project)
    db = _db(project)
    monkeypatch.setenv(
        "MONITOR_DATABASE_URL", f"sqlite:///file:{db.as_posix()}?uri=true"
    )
    before = db.read_bytes()
    result = runner.invoke(app, ["check"])
    assert result.exit_code == 0, result.output
    assert "列 'phone' がありません" in result.stdout
    assert db.read_bytes() == before


def test_missing_driver_module_is_a_warning(project, runner, monkeypatch):
    monkeypatch.setenv("MONITOR_DATABASE_URL", f"mysql+mysqldb://u:{SECRET}@h/db")
    result = runner.invoke(app, ["check"])
    assert result.exit_code == 0, result.output
    assert "ドライバ MySQLdb がインストールされていません" in result.stdout
    assert SECRET not in result.output


def test_unparsable_url_is_reported_once_by_the_settings_check(
    project, runner, monkeypatch
):
    monkeypatch.setenv("MONITOR_DATABASE_URL", f"not a url {SECRET}")
    result = runner.invoke(app, ["check"])
    assert result.exit_code == 1
    assert "MONITOR_DATABASE_URL を接続先として解釈できません" in result.stdout
    assert "表定義を読めない" not in result.stdout
    assert SECRET not in result.output


def test_migrate_dry_run_against_an_unreachable_server_fails_briefly(
    project, runner, monkeypatch
):
    monkeypatch.setenv(
        "MONITOR_DATABASE_URL", f"postgresql+pg8000://u:{SECRET}@127.0.0.1:1/db"
    )
    result = runner.invoke(app, ["migrate", "--dry-run"])
    assert result.exit_code == 1
    assert "データベースの表定義を読めないため" in result.stderr
    assert SECRET not in result.output


def test_apply_missing_columns_without_statements_does_nothing(project):
    from monitor_app.cli.maintenance import apply_missing_columns
    from monitor_app.db.schema import SchemaReport
    from monitor_app.settings.loader import load_config
    from monitor_app.settings.runtime import AppSettings

    settings = AppSettings(_env_file=None)
    cfg = load_config(project / "config.py")
    assert apply_missing_columns(settings, cfg, SchemaReport()) == []
    assert not _db(project).exists()


def test_fix_schema_is_skipped_while_the_config_has_errors(project, runner):
    _import(runner)
    _add_phone_column(project)
    _edit(project / "config.py", 'column="amount"', 'column="amout"')
    before = _columns(_db(project), "users")
    result = runner.invoke(app, ["check", "--fix-schema"])
    assert result.exit_code == 1
    assert "エラーがあるため、足りない列の追加(--fix-schema)は行いません" in (
        result.stdout
    )
    assert _columns(_db(project), "users") == before
