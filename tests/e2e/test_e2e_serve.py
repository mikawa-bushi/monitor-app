"""``monitor-app serve`` の E2E: 実サーバを起動し、CSV の推論・取り込み・監視・停止を確かめる。

日本語版 Excel の CSV(cp932・日本語の見出し・/ 区切りの日時)と、id 列の無い UTF-8
(BOM 付き)のログを置いたフォルダを、設定ファイルなしで表示する。
"""

from __future__ import annotations

import datetime as dt
import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

import pytest
from contract import helpers

pytestmark = pytest.mark.e2e

BOOT_TIMEOUT = 30.0
STOP_TIMEOUT = 8.0


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _get(port: int, path: str) -> tuple[int, bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", path)
        res = conn.getresponse()
        return res.status, res.read()
    finally:
        conn.close()


def _log(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-3000:]
    except OSError:
        return ""


def _make_csvs(csv_dir: Path) -> None:
    csv_dir.mkdir(parents=True)
    lines = ["日時,ライン,生産数,温度(℃),品番"]
    t0 = dt.datetime(2026, 10, 21, 8, 0)
    for i in range(48):
        t = t0 + dt.timedelta(minutes=5 * i)
        lines.append(
            f"{t.year}/{t.month}/{t.day} {t.hour}:{t.minute:02d},第{1 + i % 2}ライン,"
            f"{100 + i},{20 + i / 10:.1f},{i % 5:04d}"
        )
    (csv_dir / "生産実績.csv").write_bytes(
        ("\r\n".join(lines) + "\r\n").encode("cp932")
    )
    rows = ["ts,temp,ok"] + [
        f"2026-10-21T08:{i:02d}:00,{21 + i / 100:.2f},TRUE" for i in range(20)
    ]
    (csv_dir / "sensors.csv").write_bytes(("\n".join(rows) + "\n").encode("utf-8-sig"))


def test_serve_folder_of_excel_csvs(tmp_path):
    csv_dir = tmp_path / "csv"
    _make_csvs(csv_dir)
    temp_root = tmp_path / "tmp"
    temp_root.mkdir()
    env = helpers.child_env(
        TMPDIR=str(temp_root), TEMP=str(temp_root), TMP=str(temp_root)
    )
    port = _free_port()
    log_path = tmp_path / "serve.log"
    with log_path.open("wb") as log:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "monitor_app",
                "serve",
                str(csv_dir),
                "--port",
                str(port),
            ],
            cwd=tmp_path,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + BOOT_TIMEOUT
            while True:
                if proc.poll() is not None:
                    pytest.fail(
                        f"serve が終了しました(exit {proc.returncode})\n{_log(log_path)}"
                    )
                try:
                    if _get(port, "/api/health")[0] == 200:
                        break
                except OSError:
                    pass
                if time.monotonic() > deadline:
                    pytest.fail(f"serve が起動しません\n{_log(log_path)}")
                time.sleep(0.1)

            tables = json.loads(_get(port, "/api/schema")[1])["tables"]
            assert "sensors" in tables, _log(log_path)
            status, body = _get(port, "/api/views/sensors_view")
            assert status == 200
            assert len(json.loads(body)["data"]) == 20
            # 取り込みがファイルごとに文字コードを判別する(cp932 と UTF-8 BOM の混在)
            assert "生産実績" in tables, _log(log_path)
            status, body = _get(port, "/api/views/" + quote("生産実績_view"))
            assert status == 200
            data = json.loads(body)["data"]
            assert len(data) == 48 and data[0]["品番"] == "0000"
            assert _get(port, "/")[0] == 200
            assert _get(port, "/table/sensors_view")[0] == 200

            # CSV を書き換えると数秒以内に反映される
            with (csv_dir / "sensors.csv").open("ab") as fh:
                fh.write(b"2026-10-21T09:00:00,99.50,FALSE\n")
            deadline = time.monotonic() + 10
            count = 20
            while count == 20 and time.monotonic() < deadline:
                time.sleep(0.2)
                count = len(
                    json.loads(_get(port, "/api/views/sensors_view")[1])["data"]
                )
            assert count == 21, _log(log_path)

            if os.name != "nt":
                proc.send_signal(signal.SIGTERM)
                proc.wait(timeout=STOP_TIMEOUT)
                assert proc.returncode in (0, -signal.SIGTERM), _log(log_path)
                # 一時ファイルの DB は終了時に消える
                assert list(temp_root.glob("monitor-app-serve-*")) == []
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)
    # 推論の表示は出力の先頭にある(_log は失敗時の表示用に末尾だけを返すため全体を読む。
    # serve のアクセスログは runserver と同じ形式で、行が長い)
    output = log_path.read_text(encoding="utf-8", errors="replace")
    assert "sensors.csv → テーブル sensors(UTF-8・20 行)" in output
    assert "主キー: _row_id(自動採番)" in output


@pytest.mark.skipif(not hasattr(signal, "SIGHUP"), reason="SIGHUP の無い OS")
def test_serve_cleans_up_when_the_terminal_is_closed(tmp_path):
    """端末を閉じる(SIGHUP)と、停止処理を通って終わり一時ファイルの DB も消える。"""
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()
    (csv_dir / "items.csv").write_text("id,v\n1,2\n", encoding="utf-8")
    temp_root = tmp_path / "tmp"
    temp_root.mkdir()
    env = helpers.child_env(TMPDIR=str(temp_root))
    port = _free_port()
    log_path = tmp_path / "serve.log"
    with log_path.open("wb") as log:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "monitor_app",
                "serve",
                str(csv_dir),
                "--port",
                str(port),
                "--no-watch",
            ],
            cwd=tmp_path,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + BOOT_TIMEOUT
            while True:
                if proc.poll() is not None:
                    pytest.fail(f"serve が終了しました\n{_log(log_path)}")
                try:
                    if _get(port, "/api/health")[0] == 200:
                        break
                except OSError:
                    pass
                if time.monotonic() > deadline:
                    pytest.fail(f"serve が起動しません\n{_log(log_path)}")
                time.sleep(0.1)
            assert list(temp_root.glob("monitor-app-serve-*")), _log(log_path)
            proc.send_signal(signal.SIGHUP)
            proc.wait(timeout=STOP_TIMEOUT)
            assert proc.returncode == 0, _log(log_path)
            assert list(temp_root.glob("monitor-app-serve-*")) == []
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)
