"""``serve``: CSV を置いたフォルダから、設定ファイルなしでダッシュボードを起動する。

CSV のデータから列の型・主キー・グラフを推論し(:mod:`monitor_app.settings.infer`)、
SQLite(既定は終了時に消える一時ファイル)へ取り込んで Web サーバーを起動する。
CSV の変更はフォルダ監視で自動的に取り込む(``--append`` なら追記された行だけ)。
``--write-config`` で推論した設定を config.py に書き出せば、通常の ``runserver`` の
運用へそのまま移れる。

CSV の文字コードは取り込みがファイルごとに判別する(``MONITOR_CSV_ENCODING=auto``)。
``--append`` では、起動前の取り込みの読み取り位置をフォルダ監視が引き継ぐため、起動の
途中に追記された行も取りこぼさない。
"""

from __future__ import annotations

import logging
import shutil
import socket
import tempfile
import threading
import time
import webbrowser
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import typer

from . import server as _server
from ._common import StopRequested, fail, load_settings, stop_signals
from .data import run_csv_import

if TYPE_CHECKING:
    from fastapi import FastAPI

    from ..services.importer import ImportReport
    from ..services.ingest_watcher import IngestWatcher
    from ..settings.infer import InferredProject
    from ..settings.runtime import AppSettings

#: 一時ファイルの DB を置くディレクトリ名の接頭辞。
TEMP_PREFIX = "monitor-app-serve-"
#: 一時ファイルの DB のファイル名。
TEMP_DB_NAME = "serve.db"
#: フォルダ監視の間隔(秒)。MONITOR_INGEST_INTERVAL を設定していればそちらを使う。
WATCH_INTERVAL = 1.0
#: ``--open`` でサーバーの待ち受け開始を待つ最大秒数。
OPEN_TIMEOUT = 15.0
#: 取り込みの警告を表示する件数の上限(テーブルごと)。
MAX_IMPORT_WARNINGS = 5


def _default_path() -> Path:
    csv_dir = Path("csv")
    return csv_dir if csv_dir.is_dir() else Path(".")


def inference_encodings(base: AppSettings) -> tuple[str, ...]:
    """推論で試す文字コード(MONITOR_CSV_ENCODING を指定していればそれだけ)。

    ``auto`` の順序と別名のまとめ方は取り込みと同じ(:mod:`monitor_app.services.csv_encoding`)。
    使えない名前は最後に残す(推論が「文字コード '…' は使えません」と知らせる)。
    """
    from ..services import csv_encoding

    chosen = base.csv_encoding
    if "csv_encoding" in base.model_fields_set and not csv_encoding.is_auto(chosen):
        return (chosen,)
    fallback = base.csv_encoding_fallback
    known = csv_encoding.candidates(csv_encoding.AUTO, fallback, strict=False)
    unknown = [
        name
        for name in csv_encoding.split_encodings(fallback)
        if not csv_encoding.is_known(name)
    ]
    return tuple(dict.fromkeys([*known, *unknown]))


def _sqlite_url(path: Path) -> str:
    """SQLite ファイルの接続 URL(``?`` を含むパスは扱えないため呼び出し前に弾く)。"""
    from sqlalchemy.engine import URL

    text = path.as_posix()
    if "#" in text or "%" in text:
        # URL として特別な意味を持つ文字は、SQLAlchemy に任せてエスケープする
        return URL.create("sqlite", database=text).render_as_string(hide_password=False)
    return f"sqlite:///{text}"


def _serve_settings(
    base: AppSettings,
    project: InferredProject,
    db_path: Path,
    *,
    append: bool,
    watch: bool,
    host: str,
    port: int,
) -> AppSettings:
    """serve 用の実行時設定(.env / MONITOR_* の値に、serve が決める値を上書きする)。

    CSV の文字コード(MONITOR_CSV_ENCODING、既定は auto)はそのまま使う。取り込みが
    ファイルごとに判別するため、UTF-8 と cp932 の CSV が混在していてもよい。
    """
    from ..settings.runtime import AppSettings

    overrides: dict = {
        "database_url": _sqlite_url(db_path),
        "csv_dir": project.csv_dir,
        "ingest_watch": watch,
        "ingest_mode": "append" if append else "replace",
        "host": host,
        "port": port,
    }
    if "ingest_interval" not in base.model_fields_set:
        overrides["ingest_interval"] = WATCH_INTERVAL
    return AppSettings(**overrides)


def _encoding_label(encoding: str) -> str:
    from ..services.csv_encoding import label

    return label(encoding)


def print_inferred_project(project: InferredProject) -> None:
    """推論した設定(ファイル → テーブル・列の型・主キー・グラフ・注意)を表示する。"""
    typer.secho(
        f"CSV {len(project.files)} ファイルから設定を推論しました: {project.csv_dir}",
        bold=True,
    )
    for item in project.files:
        typer.echo(
            f"  {item.path.name} → テーブル {item.table}"
            f"({_encoding_label(item.encoding)}・{item.rows_label})"
        )
        columns = ", ".join(
            f"{name}:{logical}" for name, logical in item.columns.items()
        )
        typer.echo(f"    列: {columns}")
        auto = "(自動採番)" if item.synthetic_pk else ""
        typer.echo(f"    主キー: {item.primary_key}{auto}")
        if item.chart_y:
            typer.echo(
                f"    グラフ: 折れ線 x={item.chart_x} / y={', '.join(item.chart_y)}"
            )
        for note in item.notes:
            typer.secho(f"    注意: {note}", fg=typer.colors.YELLOW)
    for note in project.notes:
        typer.secho(f"  注意: {note}", fg=typer.colors.YELLOW)


def _write_config(path: Path, project: InferredProject) -> None:
    """推論した設定を config.py として書き出す(既存のファイルは上書きしない)。"""
    from ..settings.infer import render_config_py

    text = render_config_py(project)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as fh:
            fh.write(text)
    except FileExistsError:
        fail(
            f"{path} は既に存在するため書き出しません(上書きはしません)",
            "別のファイル名を --write-config に指定してください",
        )
    except OSError as exc:  # 書き込み権限が無い・フォルダの名前と重なる など
        fail(
            f"{path} に書き出せません: {exc.strerror or exc}",
            "書き込みできる場所のファイル名を --write-config に指定してください",
        )
    typer.secho(f"✅ 設定を書き出しました: {path}", fg=typer.colors.GREEN)
    typer.echo(
        f"   この設定で運用するには、.env に MONITOR_CSV_DIR={project.csv_dir} を書き、"
        f"monitor-app runserver --config {path} --import-csv-if-empty で起動します"
    )


@contextmanager
def _db_errors_explained(*, reuse: bool, db_path: Path | None) -> Iterator[None]:
    """--db のファイルを使えないときは、直し方を示して終える。

    ``db_path`` は --db で指定されたファイル(一時ファイルの DB なら None)。
    """
    from sqlalchemy.exc import OperationalError

    try:
        yield
    except OperationalError as exc:
        detail = str(exc.orig).lower()
        if reuse and "no column" in detail:
            fail(
                f"--db のファイルの表が、今の CSV の列と合いません: {exc.orig}",
                "別の CSV から作ったファイルです。別のファイル名を --db に指定するか、"
                "ファイルを削除してから起動してください",
            )
        if db_path is not None and "unable to open database file" in detail:
            fail(
                f"--db のファイルを開けません: {db_path}",
                "書き込みできるフォルダのファイル名を --db に指定してください",
            )
        raise


def _append_watcher(app: FastAPI) -> IngestWatcher:
    from ..services.ingest_watcher import IngestWatcher

    return next(
        task
        for task in app.state.services.background_tasks
        if isinstance(task, IngestWatcher)
    )


def _print_appended(counts: dict[str, int | None]) -> None:
    """``--append`` の起動前の取り込み(:meth:`IngestWatcher.import_existing`)の結果。"""
    for table, inserted in counts.items():
        if inserted is None:
            typer.echo(
                f"  取り込み {table}: 既存の行を残し、CSV の末尾から追記を取り込みます"
            )
        else:
            typer.echo(f"  取り込み {table}: {inserted:,} 行")


def _shown_warnings(report: ImportReport) -> set[str]:
    """:func:`_print_import` が表示する警告(テーブルごとに先頭の MAX_IMPORT_WARNINGS 件)。"""
    return {
        warning
        for result in report.results
        for warning in result.warnings[:MAX_IMPORT_WARNINGS]
    }


def _print_import(report: ImportReport) -> None:
    for result in report.results:
        typer.echo(
            f"  取り込み {result.table}: {result.inserted:,} 行 (skip {result.skipped})"
        )
        for warning in result.warnings[:MAX_IMPORT_WARNINGS]:
            typer.secho(f"    ⚠️  {warning}", fg=typer.colors.YELLOW)
        rest = len(result.warnings) - MAX_IMPORT_WARNINGS
        if rest > 0:
            typer.secho(f"    ⚠️  ほか {rest} 件の警告", fg=typer.colors.YELLOW)


class _HoldRecords(logging.Filter):
    """ロガーに届いたレコードを出さずに溜める(:func:`_summary_replaces_import_logs`)。"""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def filter(self, record: logging.LogRecord) -> bool:
        self.records.append(record)
        return False


@contextmanager
def _summary_replaces_import_logs() -> Iterator[set[str]]:
    """起動前の取り込みのログのうち、要約(:func:`_print_import`)に出す警告を出さない。

    取り込みは文字コードの判別(cp932 で読んだ、など)・見出しの対応付けを警告として
    ログに残し、同じ文を結果にも積む。serve は結果を要約に表示するため、そのままだと
    同じ文が 2 回並ぶ。取り込みの間は取り込みのログを溜め、終わったら、要約に出す文
    (``yield`` した集合に呼び出し側が入れる)と同じもの以外を元の順に出す。サーバーの
    動作中の取り込み(フォルダ監視)のログはこれまでどおり出る。
    """
    logger = logging.getLogger("monitor_app.importer")
    held = _HoldRecords()
    shown: set[str] = set()
    logger.addFilter(held)
    try:
        yield shown
    finally:
        logger.removeFilter(held)
        for record in held.records:
            if record.getMessage() not in shown:
                logger.handle(record)


def _browser_url(host: str, port: int) -> str:
    if host in ("", "0.0.0.0"):
        host = "127.0.0.1"
    elif host == "::":
        host = "::1"
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{port}/"


def _open_when_ready(url: str, host: str, port: int, timeout: float = OPEN_TIMEOUT):
    """サーバーが待ち受けを始めたらブラウザで開く(別スレッドで呼ぶ)。"""
    target = {"": "127.0.0.1", "0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((target, port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.2)
    webbrowser.open(url)


def warn_if_exposed(host: str, settings: AppSettings) -> None:
    """ループバック以外で API キーなしに公開するときの警告。"""
    if not _server.is_loopback(host) and not settings.api_key:
        typer.secho(
            f"警告: {host} で公開していますが API キーが未設定です。同じ"
            "ネットワークの誰でもデータを変更・削除できます"
            "(MONITOR_API_KEY を設定してください)",
            fg=typer.colors.RED,
            err=True,
        )


def _run_server(
    app: FastAPI, settings: AppSettings, host: str, port: int, open_browser: bool
) -> None:
    if open_browser:
        threading.Thread(
            target=_open_when_ready,
            args=(_browser_url(host, port), host, port),
            daemon=True,
        ).start()
    # runserver と同じ起動(1 プロセス・停止の猶予 5 秒・ログとアクセスログ・プロキシの
    # 設定)。停止のシグナルは serve の stop_signals() が受ける。
    _server.run_uvicorn(app, settings, host, port)


def serve(
    paths: list[Path] | None = typer.Argument(
        None,
        help="CSV ファイル(同じフォルダのもの)かフォルダ。"
        "既定: ./csv があればそこ、なければ現在のフォルダ",
        show_default=False,
    ),
    host: str | None = typer.Option(
        None, help="ホスト(既定: MONITOR_HOST または 127.0.0.1)"
    ),
    port: int | None = typer.Option(
        None, help="ポート(既定: MONITOR_PORT または 9990)"
    ),
    db: Path | None = typer.Option(
        None,
        "--db",
        help="データを保存する SQLite ファイル(既定: 一時ファイル。終了時に削除)",
    ),
    append: bool = typer.Option(
        False,
        "--append",
        help="CSV に追記された行だけを取り込む(ログ・計測値など追記される CSV 向け)",
    ),
    no_watch: bool = typer.Option(
        False, "--no-watch", help="CSV の変更を監視しない(起動時に 1 回だけ取り込む)"
    ),
    write_config: Path | None = typer.Option(
        None,
        "--write-config",
        help="推論した設定を config.py として書き出す(既存のファイルは上書きしない)",
    ),
    open_browser: bool = typer.Option(False, "--open", help="起動したらブラウザで開く"),
) -> None:
    """設定ファイルなしで、CSV からダッシュボードを起動する(型・グラフを自動推論)。"""
    from ..main import create_app
    from ..settings.infer import InferError, infer_project

    base = load_settings()
    targets = list(paths) if paths else [_default_path()]
    try:
        project = infer_project(
            targets, encodings=inference_encodings(base), bound_all_views=append
        )
    except InferError as exc:
        fail(
            str(exc),
            "CSV のあるフォルダかファイルを指定してください(例: monitor-app serve data)。"
            "設定なしで画面を見るには monitor-app demo",
        )
    print_inferred_project(project)
    if write_config is not None:
        _write_config(write_config, project)

    host = host or base.host
    port = port or base.port
    _server.check_port(port)
    # 取り込みの前に確かめ、二重起動で稼働中の DB を置き換えないようにする。
    _server.ensure_port_available(host, port)

    workdir: Path | None = None
    if db is None:
        workdir = Path(tempfile.mkdtemp(prefix=TEMP_PREFIX))
        db_path = workdir / TEMP_DB_NAME
        reuse = False
    else:
        db_path = db.expanduser().resolve()
        if "?" in db_path.as_posix():
            # SQLAlchemy 2.0 は URL の ? 以降を接続オプションとして読むため
            fail(f"--db のパスに ? は使えません: {db_path}")
        if db_path.is_dir():
            fail(f"--db にはファイル名を指定してください(フォルダです): {db_path}")
        try:
            db_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            fail(
                f"--db のフォルダを作れません: {db_path.parent}({exc.strerror or exc})"
            )
        reuse = db_path.exists()
    app = None
    serving = False
    try:
        # SIGTERM(docker stop など)・端末を閉じたときの SIGHUP でも、uvicorn の停止
        # 処理と一時ファイルの DB の削除(finally)を通って終える。uvicorn は停止の処理の
        # あと受けたシグナルを送り直すため、サーバーの動作中もこのブロックの中に置く。
        with stop_signals():
            settings = _serve_settings(
                base,
                project,
                db_path,
                append=append,
                watch=not no_watch,
                host=host,
                port=port,
            )
            explained = _db_errors_explained(
                reuse=reuse, db_path=None if workdir is not None else db_path
            )
            if append and not no_watch:
                # 追記の監視: 起動前の取り込みを監視の読み取り位置つきで行い、監視が
                # その続きから読む(起動の途中に追記された行も取りこぼさない)。既存の
                # DB の行のあるテーブルは取り込み済みとみなす(重複させない)。
                with explained:
                    app = create_app(project.config, settings)
                    counts = _append_watcher(app).import_existing()
                _print_appended(counts)
            else:
                # --append で既存の DB を使うときは、空のテーブルにだけ取り込む(重複させない)。
                with explained, _summary_replaces_import_logs() as shown:
                    report = run_csv_import(
                        settings, project.config, only_empty=append and reuse
                    )
                    shown.update(_shown_warnings(report))
                _print_import(report)
                app = create_app(project.config, settings)

            if workdir is not None:
                where = "一時ファイル(終了時に削除。残すには --db FILE)"
            else:
                where = str(db_path)
            typer.echo(f"  データ: {where}")
            if not no_watch:
                mode = (
                    "追記された行だけ" if append else "変更されたファイルを置き換えて"
                )
                typer.echo(f"  CSV の変更を監視し、{mode}取り込みます")
            warn_if_exposed(host, settings)
            hint = _server.access_hint(host, port)
            if hint:
                typer.echo(f"  {hint}")
            typer.secho(
                f"✅ 起動します: {_browser_url(host, port)} (Ctrl+C で終了)",
                fg=typer.colors.GREEN,
            )
            serving = True
            _run_server(app, settings, host, port, open_browser)
    # サーバーの停止処理のあとに送り直されたシグナルなら、正常な終了として扱う
    # (起動前に中断されたなら、Ctrl+C は通常どおり中断・SIGTERM は 143 で終える)。
    except KeyboardInterrupt:
        if not serving:
            raise
    except StopRequested:
        if not serving:
            raise typer.Exit(143) from None
    finally:
        if app is not None:
            # 起動に失敗した場合も DB のファイルを閉じる(一時フォルダを消せるように)
            app.state.services.close()
        if workdir is not None:
            shutil.rmtree(workdir, ignore_errors=True)
