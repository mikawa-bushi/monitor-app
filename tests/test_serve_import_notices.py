"""serve が起動前の取り込みの注意(cp932 で読んだ、など)を 1 回だけ表示すること。

取り込みはログに警告を出し、同じ文を結果にも積む。serve は結果を要約に表示するため、
以前は同じ注意がログと要約に 2 回並んでいた。要約に出す文はログに出さない(要約に
出ない注意と、サーバーの動作中の取り込みのログは、これまでどおりログに出る)。
"""

from __future__ import annotations

import logging

from typer.testing import CliRunner

from monitor_app.cli import app as cli_app
from monitor_app.cli import zero


def test_cp932_notice_is_shown_once(tmp_path, monkeypatch):
    csv_dir = tmp_path / "data"
    csv_dir.mkdir()
    (csv_dir / "生産.csv").write_bytes(
        "日時,数\r\n2026/10/21 8:00,1\r\n".encode("cp932")
    )
    monkeypatch.setattr("uvicorn.run", lambda *a, **k: None)
    result = CliRunner().invoke(cli_app, ["serve", str(csv_dir), "--no-watch"])
    assert result.exit_code == 0, result.output
    notice = "生産.csv: UTF-8 として読めないため cp932 で読み込みました"
    assert result.output.count(notice) == 1, result.output
    assert f"⚠️  {notice}" in result.stdout  # 要約に表示する


def test_other_import_logs_are_kept_in_order(monkeypatch):
    logger = logging.getLogger("monitor_app.importer")
    seen: list[str] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            seen.append(record.getMessage())

    handler = _Collect()
    logger.addHandler(handler)
    old_level = logger.level
    logger.setLevel(logging.INFO)
    try:
        with zero._summary_replaces_import_logs() as shown:
            logger.warning("要約にも出す注意")
            logger.info("ログだけの情報")
            logger.warning("ログだけの注意")
            assert seen == []  # 取り込みの間は溜める
            shown.add("要約にも出す注意")
        assert seen == ["ログだけの情報", "ログだけの注意"]
        logger.warning("後のログ")  # 取り込みの後はそのまま出る
        assert seen[-1] == "後のログ"
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)


def test_logs_are_released_when_the_import_fails():
    logger = logging.getLogger("monitor_app.importer")
    seen: list[str] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            seen.append(record.getMessage())

    handler = _Collect()
    logger.addHandler(handler)
    try:
        try:
            with zero._summary_replaces_import_logs():
                logger.warning("失敗の前の注意")
                raise RuntimeError("取り込みの失敗")
        except RuntimeError:
            pass
        assert seen == ["失敗の前の注意"]
    finally:
        logger.removeHandler(handler)
