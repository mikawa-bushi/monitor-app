"""``monitor-app serve``(設定ファイルなしの起動、#DX-09)。

uvicorn.run を差し替え、渡されたアプリを TestClient で動かして確かめる(serve は
uvicorn.run から戻ると一時ファイルの DB を消すため、確認は差し替えた run の中で行う)。
"""

from __future__ import annotations

import datetime as dt
import os
import signal
import socket
import threading
import time
from pathlib import Path
from urllib.parse import quote

import pytest
from contract import helpers
from fastapi.testclient import TestClient
from sqlalchemy.engine import make_url
from typer.testing import CliRunner

from monitor_app.cli import app as cli_app
from monitor_app.cli import zero
from monitor_app.cli._common import StopRequested, stop_signals
from monitor_app.services.ingest_watcher import IngestWatcher


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    helpers.isolate_from_host(monkeypatch, tmp_path)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


class FakeUvicorn:
    """uvicorn.run の代わり。アプリを TestClient で動かし、結果を記録する。"""

    def __init__(self, check=None) -> None:
        self.calls: list[dict] = []
        self.check = check
        self.results: dict = {}

    def __call__(self, app, **kwargs) -> None:
        settings = app.state.settings
        self.calls.append({"app": app, "settings": settings, **kwargs})
        self.db_file = Path(make_url(settings.database_url).database)
        self.results["db_existed"] = self.db_file.exists()
        if self.check is not None:
            with TestClient(app) as client:  # lifespan(フォルダ監視)も動かす
                self.check(client, app, self.results)


@pytest.fixture
def fake_uvicorn(monkeypatch):
    def install(check=None) -> FakeUvicorn:
        fake = FakeUvicorn(check)
        monkeypatch.setattr("uvicorn.run", fake)
        return fake

    return install


def _excel_jp_csv(path: Path, rows: int = 24) -> Path:
    """日本語版 Excel の「CSV(コンマ区切り)」: cp932・CRLF・日本語の見出し・/ 区切りの日時。"""
    lines = ["日時,ライン,生産数,不良数,温度(℃),品番"]
    t0 = dt.datetime(2026, 10, 21, 8, 0)
    for i in range(rows):
        t = t0 + dt.timedelta(minutes=5 * i)
        lines.append(
            f"{t.year}/{t.month}/{t.day} {t.hour}:{t.minute:02d},第{1 + i % 3}ライン,"
            f"{90 + i},{i % 4},{20 + i / 10:.1f},{i % 3 + 1:05d}"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(("\r\n".join(lines) + "\r\n").encode("cp932"))
    return path


def _sensor_csv(path: Path, rows: int = 10) -> Path:
    """id 列の無い計測ログ(Excel の「CSV UTF-8」: BOM 付き)。"""
    lines = ["ts,temp,ok"]
    for i in range(rows):
        lines.append(
            f"2026-10-21T08:{i:02d}:00,{20 + i / 10:.2f},{'TRUE' if i % 2 else 'FALSE'}"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(("\n".join(lines) + "\n").encode("utf-8-sig"))
    return path


def _codes_csv(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("id,code,name\n1,0001,A\n2,0042,B\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 起動と表示
# ---------------------------------------------------------------------------
def test_serve_japanese_excel_csv_end_to_end(tmp_path, runner, fake_uvicorn):
    csv_dir = tmp_path / "line1"
    _excel_jp_csv(csv_dir / "生産実績.csv")

    def check(client, app, results):
        view = quote("生産実績_view")
        for path in ("/", "/kiosk", "/api/schema", f"/table/{view}", "/api/kpis"):
            assert client.get(path).status_code == 200, path
        payload = client.get(f"/api/views/{view}").json()
        results["columns"] = payload["columns"]
        results["first"] = payload["data"][0]
        results["kpis"] = client.get("/api/kpis").json()["kpis"]

    fake = fake_uvicorn(check)
    result = runner.invoke(cli_app, ["serve", str(csv_dir)])
    assert result.exit_code == 0, result.output

    (call,) = fake.calls
    assert (call["host"], call["port"]) == ("127.0.0.1", 9990)
    assert (call["workers"], call["timeout_graceful_shutdown"]) == (1, 5)
    settings = call["settings"]
    assert settings.csv_dir == csv_dir.resolve()
    assert (settings.ingest_watch, settings.ingest_mode) == (True, "replace")
    assert fake.results["columns"] == [
        "_row_id",
        "日時",
        "ライン",
        "生産数",
        "不良数",
        "温度(℃)",
        "品番",
    ]
    first = fake.results["first"]
    assert (first["_row_id"], first["ライン"], first["温度(℃)"], first["品番"]) == (
        1,
        "第1ライン",
        20.0,
        "00001",  # 先頭の 0 が残る
    )
    assert fake.results["kpis"][0]["value"] == 24
    # 推論したスキーマと URL を表示する
    assert "生産実績.csv → テーブル 生産実績(cp932・24 行)" in result.output
    assert "温度(℃):float" in result.output and "品番:str" in result.output
    assert "主キー: _row_id(自動採番)" in result.output
    assert "http://127.0.0.1:9990/" in result.output
    # 一時ファイルの DB は終了時に消える
    assert fake.results["db_existed"]
    assert not fake.db_file.parent.exists()


def test_serve_mixed_folder_codes_and_missing_ids(tmp_path, runner, fake_uvicorn):
    csv_dir = tmp_path / "csv"
    _excel_jp_csv(csv_dir / "生産実績.csv")
    _sensor_csv(csv_dir / "sensors.csv")
    _codes_csv(csv_dir / "items.csv")

    def check(client, app, results):
        results["tables"] = sorted(client.get("/api/schema").json()["tables"])
        results["items"] = client.get("/api/views/items_view").json()["data"]
        results["sensors"] = client.get("/api/views/sensors_view").json()["data"]

    fake = fake_uvicorn(check)
    result = runner.invoke(cli_app, ["serve", str(csv_dir)])
    assert result.exit_code == 0, result.output
    assert [r["code"] for r in fake.results["items"]] == ["0001", "0042"]
    sensors = fake.results["sensors"]
    assert len(sensors) == 10 and sensors[0]["_row_id"] == 1
    assert sensors[1]["ok"] in (True, 1)
    # 取り込みがファイルごとに文字コードを判別する(cp932 と UTF-8 の混在でもすべて表示)
    assert fake.results["tables"] == ["items", "sensors", "生産実績"]
    assert fake.calls[0]["settings"].csv_encoding == "auto"
    assert "文字コードが混在" not in result.output


def test_serve_defaults_to_csv_folder_then_current_folder(
    tmp_path, runner, fake_uvicorn, monkeypatch
):
    fake = fake_uvicorn()
    _codes_csv(tmp_path / "here.csv")
    assert runner.invoke(cli_app, ["serve"]).exit_code == 0
    assert fake.calls[-1]["settings"].csv_dir == tmp_path.resolve()
    _codes_csv(tmp_path / "csv" / "inner.csv")
    assert runner.invoke(cli_app, ["serve"]).exit_code == 0
    assert fake.calls[-1]["settings"].csv_dir == (tmp_path / "csv").resolve()


def test_serve_without_csv_fails_with_a_hint(tmp_path, runner, fake_uvicorn):
    fake = fake_uvicorn()
    result = runner.invoke(cli_app, ["serve", str(tmp_path)])
    assert result.exit_code == 1
    assert "CSV ファイルが見つかりません" in result.output
    assert "ヒント:" in result.output
    assert "monitor-app demo" in result.output
    assert fake.calls == []


def test_serve_honours_env_and_options(tmp_path, runner, fake_uvicorn, monkeypatch):
    monkeypatch.setenv("MONITOR_PORT", "9123")
    monkeypatch.setenv("MONITOR_INGEST_INTERVAL", "7")
    monkeypatch.setenv("MONITOR_DATABASE_URL", "postgresql+psycopg://u:p@x/y")
    path = _codes_csv(tmp_path / "items.csv")
    fake = fake_uvicorn()
    result = runner.invoke(cli_app, ["serve", str(path), "--append", "--no-watch"])
    assert result.exit_code == 0, result.output
    call = fake.calls[0]
    settings = call["settings"]
    assert call["port"] == 9123
    assert settings.ingest_interval == 7.0
    assert settings.database_url.startswith("sqlite:///")  # serve の DB を使う
    assert (settings.ingest_watch, settings.ingest_mode) == (False, "append")
    assert not any(
        isinstance(t, IngestWatcher)
        for t in call["app"].state.services.background_tasks
    )
    assert "LIMIT 5000" in call["app"].state.config.views["items_view"].query


def test_serve_watches_and_reimports_changed_csv(tmp_path, runner, fake_uvicorn):
    path = _sensor_csv(tmp_path / "csv" / "sensors.csv", rows=3)

    def check(client, app, results):
        watcher = next(
            t
            for t in app.state.services.background_tasks
            if isinstance(t, IngestWatcher)
        )
        assert watcher.interval == zero.WATCH_INTERVAL and not watcher.keep

        def count() -> int:
            return client.get("/api/kpis").json()["kpis"][0]["value"]

        results["before"] = count()
        before = path.stat().st_mtime_ns
        with path.open("ab") as fh:
            fh.write(b"2026-10-21T09:00:00,99.5,TRUE\n")
        mtime = max(path.stat().st_mtime_ns, before + 1_000_000_000)
        os.utime(path, ns=(mtime, mtime))
        deadline = time.monotonic() + 10
        while count() == results["before"] and time.monotonic() < deadline:
            time.sleep(0.1)
        results["after"] = count()

    fake = fake_uvicorn(check)
    result = runner.invoke(cli_app, ["serve", str(path.parent)])
    assert result.exit_code == 0, result.output
    assert (fake.results["before"], fake.results["after"]) == (3, 4)


# ---------------------------------------------------------------------------
# --db / --write-config / --open / ポート
# ---------------------------------------------------------------------------
def test_serve_db_file_persists_and_append_does_not_duplicate(
    tmp_path, runner, fake_uvicorn
):
    path = _sensor_csv(tmp_path / "csv" / "sensors.csv", rows=5)
    db = tmp_path / "data" / "serve.db"
    fake_uvicorn()
    for _ in range(2):
        result = runner.invoke(
            cli_app, ["serve", str(path.parent), "--db", str(db), "--append"]
        )
        assert result.exit_code == 0, result.output
    assert helpers.query(db, "SELECT COUNT(*) FROM sensors") == [
        (5,)
    ]  # 2 回目は空の表だけ
    assert str(db.resolve()) in result.output
    # 置き換え(既定)なら CSV の内容で置き換える
    result = runner.invoke(cli_app, ["serve", str(path.parent), "--db", str(db)])
    assert result.exit_code == 0, result.output
    assert helpers.query(db, "SELECT COUNT(*) FROM sensors") == [(5,)]


def test_serve_write_config_round_trip_and_no_overwrite(tmp_path, runner, fake_uvicorn):
    csv_dir = tmp_path / "csv"
    _excel_jp_csv(csv_dir / "生産実績.csv")
    _codes_csv(csv_dir / "items.csv")
    target = tmp_path / "config.py"
    fake = fake_uvicorn()
    result = runner.invoke(
        cli_app, ["serve", str(csv_dir), "--write-config", str(target)]
    )
    assert result.exit_code == 0, result.output
    assert "設定を書き出しました" in result.output
    assert f"MONITOR_CSV_DIR={csv_dir.resolve()}" in result.output

    check = runner.invoke(cli_app, ["check", "--config", str(target)])
    assert check.exit_code == 0, check.output

    target.write_text("# 利用者が編集した設定\n", encoding="utf-8")
    again = runner.invoke(
        cli_app, ["serve", str(csv_dir), "--write-config", str(target)]
    )
    assert again.exit_code == 1
    assert "上書きはしません" in again.output
    assert target.read_text(encoding="utf-8") == "# 利用者が編集した設定\n"
    assert len(fake.calls) == 1


def test_serve_busy_port_fails_before_import(
    tmp_path, runner, fake_uvicorn, real_port_probe
):
    fake = fake_uvicorn()
    path = _codes_csv(tmp_path / "items.csv")
    db = tmp_path / "serve.db"
    with socket.create_server(("127.0.0.1", 0)) as sock:
        port = sock.getsockname()[1]
        result = runner.invoke(
            cli_app, ["serve", str(path), "--port", str(port), "--db", str(db)]
        )
    assert result.exit_code == 1
    assert f"ポート {port} は使用中です" in result.output
    assert fake.calls == [] and not db.exists()


def test_serve_open_starts_the_browser_helper(
    tmp_path, runner, fake_uvicorn, monkeypatch
):
    opened: list = []
    monkeypatch.setattr(zero, "_open_when_ready", lambda *args: opened.append(args))
    fake_uvicorn()
    path = _codes_csv(tmp_path / "items.csv")
    result = runner.invoke(
        cli_app, ["serve", str(path), "--open", "--host", "0.0.0.0", "--port", "9555"]
    )
    assert result.exit_code == 0, result.output
    deadline = time.monotonic() + 5
    while not opened and time.monotonic() < deadline:
        time.sleep(0.01)
    assert opened == [("http://127.0.0.1:9555/", "0.0.0.0", 9555)]
    assert "API キーが未設定" in result.output  # LAN に公開するときの警告


def test_open_when_ready_waits_for_the_listener(monkeypatch):
    opened: list = []
    monkeypatch.setattr("webbrowser.open", opened.append)
    with socket.create_server(("127.0.0.1", 0)) as sock:
        port = sock.getsockname()[1]
        thread = threading.Thread(
            target=zero._open_when_ready, args=("http://x/", "127.0.0.1", port, 5.0)
        )
        thread.start()
        thread.join(5)
    assert opened == ["http://x/"]


@pytest.mark.parametrize(
    "host, url",
    [
        ("127.0.0.1", "http://127.0.0.1:1/"),
        ("0.0.0.0", "http://127.0.0.1:1/"),
        ("::", "http://[::1]:1/"),
        ("::1", "http://[::1]:1/"),
        ("factory-pc", "http://factory-pc:1/"),
    ],
)
def test_browser_url(host, url):
    assert zero._browser_url(host, 1) == url


def test_sqlite_url_escapes_special_characters(tmp_path):
    odd = tmp_path / "a#b%25c" / "serve.db"
    url = zero._sqlite_url(odd)
    assert make_url(url).database == odd.as_posix()
    plain = tmp_path / "日本語 フォルダ" / "serve.db"
    assert zero._sqlite_url(plain) == f"sqlite:///{plain.as_posix()}"


def test_db_path_with_question_mark_is_rejected(tmp_path, runner, fake_uvicorn):
    fake = fake_uvicorn()
    path = _codes_csv(tmp_path / "items.csv")
    result = runner.invoke(
        cli_app, ["serve", str(path), "--db", str(tmp_path / "a?b.db")]
    )
    assert result.exit_code == 1
    assert "? は使えません" in result.output and fake.calls == []


def test_explicit_encoding_setting_is_respected(
    tmp_path, runner, fake_uvicorn, monkeypatch
):
    monkeypatch.setenv("MONITOR_CSV_ENCODING", "utf-8")
    csv_dir = tmp_path / "csv"
    _excel_jp_csv(csv_dir / "生産実績.csv")
    _codes_csv(csv_dir / "items.csv")
    fake = fake_uvicorn()
    result = runner.invoke(cli_app, ["serve", str(csv_dir)])
    assert result.exit_code == 0, result.output
    assert "文字コードを判別できません" in result.output  # cp932 の表は読まない
    assert list(fake.calls[0]["app"].state.config.tables) == ["items"]
    assert fake.calls[0]["settings"].csv_encoding == "utf-8"


def test_temp_dir_is_removed_even_when_the_server_fails(tmp_path, runner, monkeypatch):
    seen: list[Path] = []

    def boom(app, **kwargs):
        seen.append(Path(make_url(app.state.settings.database_url).database))
        raise OSError("bind failed")

    monkeypatch.setattr("uvicorn.run", boom)
    path = _codes_csv(tmp_path / "items.csv")
    result = runner.invoke(cli_app, ["serve", str(path)])
    assert result.exit_code != 0
    assert seen and not seen[0].parent.exists()


def test_imported_rows_land_in_the_served_database(tmp_path, runner, fake_uvicorn):
    db = tmp_path / "keep.db"
    fake_uvicorn()
    path = _codes_csv(tmp_path / "items.csv")
    assert runner.invoke(cli_app, ["serve", str(path), "--db", str(db)]).exit_code == 0
    assert helpers.query(db, "SELECT code FROM items ORDER BY id") == [
        ("0001",),
        ("0042",),
    ]


# ---------------------------------------------------------------------------
# 停止(シグナル)
# ---------------------------------------------------------------------------
def test_sigterm_resent_by_uvicorn_still_cleans_up(tmp_path, runner, monkeypatch):
    """uvicorn は停止の処理のあと SIGTERM を送り直す。その場で終了せず後片付けする。"""
    import signal

    seen: list[Path] = []

    def fake_run(app, **kwargs):
        seen.append(Path(make_url(app.state.settings.database_url).database))
        signal.raise_signal(signal.SIGTERM)  # uvicorn の capture_signals と同じ

    previous = signal.getsignal(signal.SIGTERM)
    monkeypatch.setattr("uvicorn.run", fake_run)
    path = _codes_csv(tmp_path / "items.csv")
    result = runner.invoke(cli_app, ["serve", str(path)])
    assert result.exit_code == 0, result.output
    assert seen and not seen[0].parent.exists()
    assert signal.getsignal(signal.SIGTERM) is previous  # ハンドラを元に戻す


def test_ctrl_c_resent_by_uvicorn_is_a_normal_exit(tmp_path, runner, monkeypatch):
    def fake_run(app, **kwargs):
        raise KeyboardInterrupt  # uvicorn が送り直した SIGINT

    monkeypatch.setattr("uvicorn.run", fake_run)
    result = runner.invoke(cli_app, ["serve", str(_codes_csv(tmp_path / "i.csv"))])
    assert result.exit_code == 0, result.output


def test_interrupt_before_serving_aborts(tmp_path, runner, monkeypatch):
    calls: list = []
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: calls.append(app))

    def terminated(*args, **kwargs):
        raise StopRequested  # 取り込み中の SIGTERM

    monkeypatch.setattr(zero, "run_csv_import", terminated)
    result = runner.invoke(cli_app, ["serve", str(_codes_csv(tmp_path / "i.csv"))])
    assert result.exit_code == 143 and calls == []

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(zero, "run_csv_import", interrupted)
    result = runner.invoke(cli_app, ["serve", str(_codes_csv(tmp_path / "i.csv"))])
    assert result.exit_code not in (0, 143) and calls == []  # click の中断(Aborted!)


def test_sigterm_handler_is_only_installed_on_the_main_thread():
    import signal

    previous = signal.getsignal(signal.SIGTERM)
    errors: list = []

    def worker():
        try:
            with stop_signals():
                pass
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    assert errors == [] and signal.getsignal(signal.SIGTERM) is previous


def test_reused_db_from_other_csv_columns_explains_the_fix(
    tmp_path, runner, fake_uvicorn
):
    fake = fake_uvicorn()
    db = tmp_path / "keep.db"
    path = tmp_path / "csv" / "items.csv"
    _codes_csv(path)
    assert runner.invoke(cli_app, ["serve", str(path), "--db", str(db)]).exit_code == 0
    path.write_text("id,code,name,price\n1,0001,A,10\n", encoding="utf-8")  # 列が増えた
    result = runner.invoke(cli_app, ["serve", str(path), "--db", str(db)])
    assert result.exit_code == 1
    assert "今の CSV の列と合いません" in result.output
    assert "別のファイル名を --db に指定する" in result.output
    assert len(fake.calls) == 1


# ---------------------------------------------------------------------------
# レビューで見つかった問題の回帰テスト
# ---------------------------------------------------------------------------
needs_sighup = pytest.mark.skipif(
    not hasattr(signal, "SIGHUP"), reason="SIGHUP の無い OS"
)


@needs_sighup
def test_closing_the_terminal_sighup_cleans_up_like_sigterm(
    tmp_path, runner, monkeypatch
):
    """端末を閉じた(SIGHUP)ときも、停止処理と一時ファイルの DB の削除を行う。"""
    import signal

    seen: list[Path] = []
    handlers: list = []

    def fake_run(app, **kwargs):
        seen.append(Path(make_url(app.state.settings.database_url).database))
        handlers.append(signal.getsignal(signal.SIGHUP))
        if handlers[-1] in (signal.SIG_DFL, signal.SIG_IGN):
            return  # 対策が無い(既定の動作ではテストのプロセスごと終わってしまう)
        signal.raise_signal(signal.SIGHUP)

    previous = signal.signal(signal.SIGHUP, signal.SIG_DFL)
    monkeypatch.setattr("uvicorn.run", fake_run)
    try:
        result = runner.invoke(cli_app, ["serve", str(_codes_csv(tmp_path / "i.csv"))])
        restored = signal.getsignal(signal.SIGHUP)
    finally:
        signal.signal(signal.SIGHUP, previous)
    assert handlers and handlers[0] not in (signal.SIG_DFL, signal.SIG_IGN)
    assert result.exit_code == 0, result.output
    assert seen and not seen[0].parent.exists()
    assert restored is signal.SIG_DFL  # 元のハンドラに戻す


@needs_sighup
def test_sighup_ignored_by_nohup_stays_ignored(tmp_path, runner, monkeypatch):
    import signal

    states: list = []
    monkeypatch.setattr(
        "uvicorn.run", lambda app, **kw: states.append(signal.getsignal(signal.SIGHUP))
    )
    previous = signal.signal(signal.SIGHUP, signal.SIG_IGN)
    try:
        result = runner.invoke(cli_app, ["serve", str(_codes_csv(tmp_path / "i.csv"))])
    finally:
        signal.signal(signal.SIGHUP, previous)
    assert result.exit_code == 0, result.output
    assert states == [signal.SIG_IGN]


@pytest.mark.skipif(
    os.name == "nt" or os.geteuid() == 0, reason="POSIX のパーミッションで確かめる"
)
def test_unwritable_targets_fail_with_a_message(tmp_path, runner, fake_uvicorn):
    fake = fake_uvicorn()
    path = _codes_csv(tmp_path / "csv" / "items.csv")
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        for args in (
            ["--write-config", str(locked / "config.py")],
            ["--write-config", str(locked / "sub" / "config.py")],
            ["--db", str(locked / "serve.db")],
            ["--db", str(locked / "sub" / "serve.db")],
        ):
            result = runner.invoke(cli_app, ["serve", str(path.parent), *args])
            assert result.exit_code == 1, (args, result.output)
            assert result.exception is None or isinstance(
                result.exception, SystemExit
            ), (args, result.exception)
            assert "Traceback" not in result.output
            assert "ヒント" in result.output or "作れません" in result.output, args
    finally:
        locked.chmod(0o700)
    assert fake.calls == []


def test_db_option_pointing_at_a_folder_is_rejected(tmp_path, runner, fake_uvicorn):
    fake = fake_uvicorn()
    path = _codes_csv(tmp_path / "csv" / "items.csv")
    result = runner.invoke(cli_app, ["serve", str(path), "--db", str(tmp_path)])
    assert result.exit_code == 1
    assert "--db にはファイル名を指定してください" in result.output
    assert fake.calls == []


def test_headers_with_surrounding_spaces_are_imported(tmp_path, runner, fake_uvicorn):
    """推論は空白を除いた列名にし、取り込みも見出しの表記ゆれ(前後の空白)を対応づける。"""
    path = tmp_path / "csv" / "hand.csv"
    path.parent.mkdir()
    path.write_text("id, name , temp\n1, a , 2.5\n", encoding="utf-8")

    def check(client, app, results):
        results["rows"] = client.get("/api/views/hand_view").json()["data"]

    fake = fake_uvicorn(check)
    assert runner.invoke(cli_app, ["serve", str(path.parent)]).exit_code == 0
    (row,) = fake.results["rows"]
    assert (row["temp"], row["name"].strip()) == (2.5, "a")


def test_explicit_auto_encoding_is_not_passed_to_an_importer_without_it(
    tmp_path, runner, fake_uvicorn, monkeypatch
):
    """MONITOR_CSV_ENCODING=auto(.env.example の既定の説明どおり)でも取り込める。"""
    monkeypatch.setenv("MONITOR_CSV_ENCODING", "auto")
    csv_dir = tmp_path / "csv"
    _excel_jp_csv(csv_dir / "生産実績.csv")

    def check(client, app, results):
        results["count"] = client.get("/api/kpis").json()["kpis"][0]["value"]

    fake = fake_uvicorn(check)
    result = runner.invoke(cli_app, ["serve", str(csv_dir)])
    assert result.exit_code == 0, result.output
    assert fake.results["count"] == 24
