"""``startproject NAME --from-csv DIR``: 手元の CSV から推論した設定でプロジェクトを作る。

config.py は serve と同じ推論(列の型・主キー・ビュー・グラフ・KPI)で書き、CSV は
プロジェクトの csv/ にコピーする(雛形のサンプル CSV は入れない)。生成した config.py は
check を通り、import-csv で serve と同じ行が入る。
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from contract import helpers
from typer.testing import CliRunner

from monitor_app.cli import app
from monitor_app.cli.data import run_csv_import
from monitor_app.settings.infer import infer_project
from monitor_app.settings.loader import load_config
from monitor_app.settings.runtime import AppSettings


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    helpers.isolate_from_host(monkeypatch, tmp_path)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def exports(tmp_path) -> Path:
    """Excel の CSV(cp932・日本語の見出し・/ 区切りの日時)と UTF-8 の計測ログ。"""
    folder = tmp_path / "exports"
    folder.mkdir()
    lines = ["日時,ライン,生産数,温度(℃)"]
    t0 = dt.datetime(2026, 10, 21, 8, 0)
    for i in range(30):
        t = t0 + dt.timedelta(minutes=5 * i)
        lines.append(
            f"{t.year}/{t.month}/{t.day} {t.hour}:{t.minute:02d},第{1 + i % 2}ライン,"
            f"{100 + i},{20 + i / 10:.1f}"
        )
    (folder / "生産実績.csv").write_bytes(("\r\n".join(lines) + "\r\n").encode("cp932"))
    (folder / "sensors.csv").write_text(
        "id,ts,temp\n1,2026-10-21T08:00:00,20.5\n2,2026-10-21T08:01:00,21.0\n",
        encoding="utf-8",
    )
    (folder / "readme.txt").write_text("CSV ではない", encoding="utf-8")
    (folder / "a,b.csv").write_text("id\n1\n", encoding="utf-8")  # テーブル名にできない
    return folder


def _rows(db: Path, table: str) -> list[tuple]:
    with closing(sqlite3.connect(db)) as conn:
        return conn.execute(f'SELECT * FROM "{table}" ORDER BY 1').fetchall()


def test_from_csv_writes_the_inferred_config_and_copies_the_csvs(
    tmp_path, runner, exports
):
    result = runner.invoke(app, ["startproject", "line1", "--from-csv", str(exports)])
    assert result.exit_code == 0, result.output
    proj = tmp_path / "line1"
    # 推論の結果を表示する(serve と同じ表示)
    assert "生産実績.csv → テーブル 生産実績(cp932・30 行)" in result.stdout
    assert "a,b.csv: ファイル名にテーブル名として使えない文字" in result.stdout
    # 使える CSV だけをコピーし、雛形のサンプルは入れない
    assert sorted(p.name for p in (proj / "csv").iterdir()) == [
        "sensors.csv",
        "生産実績.csv",
    ]
    assert (proj / "csv" / "生産実績.csv").read_bytes() == (
        exports / "生産実績.csv"
    ).read_bytes()
    for name in (".env.example", "README.md", ".gitignore", "instances/.gitkeep"):
        assert (proj / name).exists(), name

    text = (proj / "config.py").read_text(encoding="utf-8")
    assert "monitor-app startproject --from-csv が CSV から自動生成" in text
    assert "MONITOR_CSV_DIR" not in text  # CSV はプロジェクトの csv/(既定の場所)
    config = load_config(proj / "config.py")
    assert config.app_title == "line1"  # 画面のタイトルはプロジェクト名
    assert config.tables["生産実績"].columns["日時"] == "datetime"
    assert config.tables["sensors"].primary_key == "id"
    assert config.views["sensors_view"].chart is not None
    assert "  monitor-app runserver --import-csv-if-empty" in result.stdout


def test_generated_project_passes_check_and_imports_like_serve(
    tmp_path, runner, exports, monkeypatch
):
    assert (
        runner.invoke(app, ["startproject", "line1", "--from-csv", str(exports)])
    ).exit_code == 0
    proj = tmp_path / "line1"
    monkeypatch.chdir(proj)
    for args in (["check", "--strict"], ["check", "--data"]):
        result = runner.invoke(app, args)
        assert result.exit_code == 0, result.output
    imported = runner.invoke(app, ["import-csv"])
    assert imported.exit_code == 0, imported.output
    assert "✅ 取り込み完了: 合計 32 行" in imported.stdout

    # serve と同じ推論・取り込み(元のフォルダから直接)と、同じ行になる
    project = infer_project([exports])
    serve_db = tmp_path / "serve.db"
    settings = AppSettings(
        _env_file=None,
        database_url=helpers.sqlite_url(serve_db),
        csv_dir=project.csv_dir,
    )
    run_csv_import(settings, project.config)
    db = proj / "instances" / "database.db"
    for table in ("生産実績", "sensors"):
        assert _rows(db, table) == _rows(serve_db, table), table
    assert len(_rows(db, "生産実績")) == 30


def test_from_csv_accepts_a_single_file_and_deploy_targets(tmp_path, runner, exports):
    result = runner.invoke(
        app,
        [
            "startproject",
            "solo",
            "--from-csv",
            str(exports / "sensors.csv"),
            "--deploy",
            "docker",
        ],
    )
    assert result.exit_code == 0, result.output
    proj = tmp_path / "solo"
    assert [p.name for p in (proj / "csv").iterdir()] == ["sensors.csv"]
    assert (proj / "compose.yaml").exists()
    assert "[Docker]" in result.stdout
    assert set(load_config(proj / "config.py").tables) == {"sensors"}


@pytest.mark.parametrize(
    "make",
    [
        lambda root: root / "missing",
        lambda root: (root / "empty").mkdir() or root / "empty",
    ],
    ids=["missing", "no-csv"],
)
def test_unusable_source_fails_before_creating_anything(tmp_path, runner, make):
    source = make(tmp_path)
    result = runner.invoke(app, ["startproject", "proj", "--from-csv", str(source)])
    assert result.exit_code == 1
    assert "--from-csv に指定してください" in result.stderr
    assert not (tmp_path / "proj").exists()


def test_existing_target_is_refused_before_reading_the_csvs(tmp_path, runner, exports):
    (tmp_path / "line1").mkdir()
    result = runner.invoke(app, ["startproject", "line1", "--from-csv", str(exports)])
    assert result.exit_code == 1
    assert "既に存在します" in result.output
    assert "推論しました" not in result.output


def test_without_from_csv_the_scaffold_is_unchanged(tmp_path, runner):
    result = runner.invoke(app, ["startproject", "plain"])
    assert result.exit_code == 0, result.output
    proj = tmp_path / "plain"
    scaffold = Path(__file__).parent.parent / "monitor_app" / "scaffold"
    assert (proj / "config.py").read_bytes() == (scaffold / "config.py").read_bytes()
    assert {p.name for p in (proj / "csv").iterdir()} == {
        "users.csv",
        "products.csv",
        "orders.csv",
    }
