"""SQLite データベースのバックアップと復元(CRIT-11)。

SQLite のオンラインバックアップ API(``sqlite3.Connection.backup``)を使う。読み取り
トランザクションの中でページ単位に写すため、WAL モード(-wal ファイルにだけある
コミット済みのデータを含む)でも一貫したコピーになり、サーバーの稼働中に実行して
よい。``database.db`` をファイルコピーするだけでは WAL の内容が欠ける。

バックアップは ``<DB 名>-YYYYmmdd-HHMMSS.db`` という 1 ファイル(journal_mode=delete)
で、新しい順に ``keep`` 世代を残す。PostgreSQL / MySQL は ``pg_dump`` / ``mysqldump``
を使う(ここでは扱わない)。

復元は、壊れて開けなくなった DB(停電による SD カードの破損など)にも使える: 壊れた
ファイルはバックアップ API で書き込めないため、新しいファイルに写してから置き換える。
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import shutil
import sqlite3
import tempfile
import threading
import time
from contextlib import closing, suppress
from datetime import datetime
from pathlib import Path

from ..exceptions import BackupError
from .engine import Database

logger = logging.getLogger("monitor_app.backup")

_STAMP_FORMAT = "%Y%m%d-%H%M%S"
#: オンラインバックアップ API がロック待ちで返す結果コード(SQLITE_BUSY・SQLITE_LOCKED)。
_BUSY_CODES = frozenset({5, 6})
#: バックアップのファイル名(``<stem>-YYYYmmdd-HHMMSS[-N].db``)の日時と連番。
_NAME_RE = r"-(?P<stamp>\d{8}-\d{6})(?:-(?P<seq>\d+))?\.db"
#: DB が壊れていることを表す SQLite の結果コード(SQLITE_CORRUPT・SQLITE_NOTADB)。
_CORRUPT_CODES = frozenset({11, 26})
#: DB ファイルと一緒に使われる付属ファイル(WAL・共有メモリ・ロールバックジャーナル)。
_SIDE_SUFFIXES = ("-wal", "-shm", "-journal")
#: 書き込み中の一時ファイルの名前(``.<stem>-XXXXXXXX.tmp`` と SQLite の付属ファイル)。
_TEMP_RE = r"-[A-Za-z0-9_]{8}\.tmp(?:-journal|-wal|-shm)?"
#: これより長く更新されていない一時ファイルは、強制終了されたバックアップ・復元の残り
#: とみなして消す(秒)。書き込み中のファイルは更新され続けるため消さない。
STALE_TEMP_AGE = 3600.0
#: SQLite のファイルの先頭(ヘッダ)の目印と、application_id の位置。
_SQLITE_MAGIC = b"SQLite format 3\x00"
_APP_ID_OFFSET = 68
#: 同じフォルダにほかのデータベースのバックアップがあると警告した (フォルダ, stem, 番号)。
_foreign_warned: set[tuple[str, str, int]] = set()
#: quick_check を通ったバックアップ (パス, 大きさ, 更新時刻)。同じファイルを何度も
#: 調べない(restore は確認の表示と復元の前に 2 回確かめる)。
_validated: set[tuple[str, int, int]] = set()
_validated_lock = threading.Lock()


def is_corrupt_error(exc: BaseException) -> bool:
    """SQLite のファイルが壊れている(データベースとして読めない)ことを表す例外か。

    ロック待ち(database is locked)などの一時的な失敗とは区別する。
    """
    if not isinstance(exc, sqlite3.DatabaseError):
        return False
    code = getattr(exc, "sqlite_errorcode", None)  # Python 3.11 以降
    if code is not None:
        return (code & 0xFF) in _CORRUPT_CODES
    message = str(exc).lower()
    return "file is not a database" in message or "malformed" in message


def _require_sqlite_file(db: Database) -> Path:
    path = db.sqlite_path
    if path is not None:
        return path
    if db.backend == "sqlite":
        raise BackupError(
            "メモリ内の SQLite データベースはバックアップ・復元できません"
        )
    raise BackupError(
        "バックアップ・復元は SQLite のデータベースだけに対応しています。"
        f"{db.backend} は pg_dump / mysqldump などの DB 標準のツールを使ってください"
    )


def _parse(name: str, stem: str) -> tuple[datetime, int] | None:
    match = re.fullmatch(re.escape(stem) + _NAME_RE, name)
    if match is None:
        return None
    try:
        stamp = datetime.strptime(match.group("stamp"), _STAMP_FORMAT)
    except ValueError:
        return None
    return stamp, int(match.group("seq") or 1)


def backup_owner(path: str | Path) -> int | None:
    """SQLite のファイルのヘッダにある application_id(読めなければ None)。

    :func:`backup_database` はデータベースごとの番号をここに入れ(バックアップにも
    そのまま写る)、同じフォルダにある別のデータベースのバックアップと区別する。
    0 はこの仕組みより前のバックアップ(どのデータベースのものか分からない)。
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(_APP_ID_OFFSET + 4)
    except OSError:
        return None
    if len(head) < _APP_ID_OFFSET + 4 or not head.startswith(_SQLITE_MAGIC):
        return None
    return int.from_bytes(head[_APP_ID_OFFSET:], "big", signed=True)


def _owned_by(path: Path, owner: int | None) -> bool:
    """``path`` が番号 ``owner`` のデータベースのバックアップとみなせるか。

    ``owner`` が None なら区別しない。番号の無い(0 の)古いバックアップは、どの
    データベースのものともみなす(従来どおり世代に数える)。番号がまだ無い(0 の)
    データベースは、番号の無いバックアップだけを自分のものとみなす。
    """
    if owner is None:
        return True
    found = backup_owner(path)
    return found is None or found == 0 or found == owner


def list_backups(
    dest_dir: str | Path, stem: str, *, owner: int | None = None
) -> list[Path]:
    """``dest_dir`` にある ``stem`` のバックアップ(古い順)。ほかのファイルは含めない。

    ``~`` はホームフォルダ(:func:`backup_database` の保存先と同じ解釈)。``owner``
    (:func:`database_owner` の番号)を渡すと、同じ名前の別のデータベースのバック
    アップを除く(フォルダを複数のプロジェクトで共有したとき — D3-16)。
    """
    folder = Path(dest_dir).expanduser()
    if not folder.is_dir():
        return []
    found = []
    for entry in folder.iterdir():
        key = _parse(entry.name, stem)
        if key is not None and entry.is_file() and _owned_by(entry, owner):
            found.append((key, entry))
    return [entry for _key, entry in sorted(found)]


def latest_backup_time(
    dest_dir: str | Path,
    stem: str,
    *,
    owner: int | None = None,
    not_after: datetime | None = None,
) -> datetime | None:
    """最新のバックアップの日時(ファイル名から。無ければ None)。

    ``not_after`` を渡すと、それより後の日時のバックアップ(時計が進んでいたときに
    作られたもの)を除く。``owner`` は :func:`list_backups` と同じ。
    """
    times = [
        parsed[0]
        for parsed in (
            _parse(entry.name, stem)
            for entry in list_backups(dest_dir, stem, owner=owner)
        )
        if parsed is not None and (not_after is None or parsed[0] <= not_after)
    ]
    return max(times) if times else None


def database_owner(
    path: str | Path, *, assign: bool = False, timeout: float = 0.2
) -> int | None:
    """データベースの番号(application_id)。番号が無ければ 0、読めなければ None。

    ``assign=True`` なら、番号が無いときに乱数の番号を付ける(ヘッダの 4 バイトを
    書くだけで、表やデータには触れない)。ほかの接続のロックは ``timeout`` 秒しか
    待たない(待っている間は Ctrl+C も効かないため)。ロックされている・書き込めない
    ときは付けずに返す(バックアップはこれまでどおり取れ、次の機会に付ける)。
    """
    if not Path(path).is_file():
        return None  # 開くと空のデータベースができてしまう
    try:
        with closing(sqlite3.connect(str(path), timeout=timeout)) as conn:
            (owner,) = conn.execute("PRAGMA application_id").fetchone()
            if owner == 0 and assign:
                new = secrets.randbelow(2**31 - 2) + 1
                conn.execute(f"PRAGMA application_id = {new}")
                owner = new
    except sqlite3.Error as exc:
        logger.debug("データベースの番号を読み書きできません: %s (%s)", path, exc)
        return None
    return int(owner)


def _warn_foreign(folder: Path, stem: str, owner: int) -> None:
    """同じ名前の別のデータベースのバックアップが同じフォルダにあれば 1 回だけ警告する。"""
    key = (str(folder), stem, owner)
    if owner == 0 or key in _foreign_warned:
        return
    foreign = [
        entry for entry in list_backups(folder, stem) if not _owned_by(entry, owner)
    ]
    if foreign:
        _foreign_warned.add(key)
        logger.warning(
            "バックアップの保存先 %s に、別のデータベース(同じ名前 %s)のバックアップが"
            " %d 件あります。世代の整理では消しませんが、MONITOR_BACKUP_DIR はプロジェクト"
            "ごとに別のフォルダにしてください",
            folder,
            stem,
            len(foreign),
        )


def remove_stale_temp_files(
    folder: str | Path, stem: str, *, max_age: float = STALE_TEMP_AGE
) -> list[Path]:
    """強制終了されたバックアップ・復元が残した ``stem`` の一時ファイルを消す。

    ``.<stem>-XXXXXXXX.tmp``(と SQLite の付属ファイル)のうち、``max_age`` 秒以上
    更新されていないものだけを消す(D3-13)。電源断・SIGKILL・Windows のタスクの
    強制終了では後片付けが走らず、DB と同じ大きさになり得るファイルが残り続けていた。
    """
    folder = Path(folder).expanduser()
    if not folder.is_dir():
        return []
    pattern = re.compile(r"\." + re.escape(stem) + _TEMP_RE)
    cutoff = time.time() - max_age
    removed: list[Path] = []
    for entry in folder.iterdir():
        if pattern.fullmatch(entry.name) is None:
            continue
        try:
            if entry.is_file() and entry.stat().st_mtime < cutoff:
                entry.unlink()
                removed.append(entry)
        except OSError as exc:
            logger.warning("一時ファイルを削除できません: %s (%s)", entry, exc)
    for entry in removed:
        logger.info("中断されたバックアップの一時ファイルを削除しました: %s", entry)
    return removed


def _destination(folder: Path, stem: str, now: datetime) -> Path:
    """新しいバックアップのパス。同じ秒に 2 回以上作ったときは連番を付ける。

    連番は同じ秒の既存のバックアップより大きくする(古い連番が世代の整理で消えた
    あとに若い連番を使うと、新しいバックアップが古い順に並んで消されてしまうため)。
    """
    stamp = now.strftime(_STAMP_FORMAT)
    base = f"{stem}-{stamp}"
    same_second = datetime.strptime(stamp, _STAMP_FORMAT)
    used = [
        key[1]
        for key in (_parse(entry.name, stem) for entry in folder.iterdir())
        if key is not None and key[0] == same_second
    ]
    if not used:
        return folder / f"{base}.db"
    return folder / f"{base}-{max(used) + 1}.db"


def _lock_wait(db: Database) -> float:
    """ほかの接続のロックを待つ最長の秒数(DB のロック待ちの設定、最短 1 秒)。"""
    return max(db.sqlite_busy_timeout_ms / 1000.0, 1.0)


def _take_lock_wait(conn: sqlite3.Connection) -> float:
    """接続のロック待ちの設定(秒)を読み、SQLite 自身では待たないようにする。

    SQLite のロック待ちの間は Python に戻らず Ctrl+C も効かないため、待ちは
    :func:`_online_copy` が行う。
    """
    (busy_ms,) = conn.execute("PRAGMA busy_timeout").fetchone()
    conn.execute("PRAGMA busy_timeout = 0")
    return busy_ms / 1000.0


def _online_copy(
    src: sqlite3.Connection, dest: sqlite3.Connection, *, wait: float, what: str
) -> None:
    """オンラインバックアップ API で ``src`` の全ページを ``dest`` へ写す。

    Python の ``Connection.backup()`` は、ほかの接続のロックで進めない間(BUSY・LOCKED)
    いつまでも待ち続け、その間は Ctrl+C も効かない。進み具合の通知のたびに確かめ、
    進めない状態が ``wait`` 秒続いたら :class:`BackupError` で打ち切る(通知の関数が
    動くため、待っている間も Ctrl+C で止められる)。ロックを取り合う側の接続は
    :func:`_take_lock_wait` で SQLite 自身の待ちを止めておくこと(その間は通知が
    呼ばれない)。
    """
    blocked_since: list[float] = []

    def progress(status: int, remaining: int, total: int) -> None:
        if status not in _BUSY_CODES:
            blocked_since.clear()
            return
        now = time.monotonic()
        if not blocked_since:
            blocked_since.append(now)
        elif now - blocked_since[0] > wait:
            raise BackupError(
                f"データベースがほかのプロセスの書き込みでロックされているため、{what}"
                f"できませんでした({wait:.0f} 秒待ちました)"
            )

    src.backup(dest, progress=progress)


def _copy(src: sqlite3.Connection, dest_path: Path) -> None:
    """``src`` の内容を ``dest_path`` へオンラインバックアップで写す(1 ファイルにする)。

    ほかの接続のロックは ``src`` のロック待ちの設定の秒数まで待つ。
    """
    wait = _take_lock_wait(src)
    with closing(sqlite3.connect(str(dest_path))) as dest:
        _online_copy(src, dest, wait=wait, what="バックアップ")
        # コピー先は単体で持ち運べる 1 ファイルにする(-wal / -shm を作らない)。
        dest.execute("PRAGMA journal_mode = DELETE")


def backup_database(
    db: Database,
    dest_dir: str | Path,
    *,
    keep: int = 7,
    now: datetime | None = None,
) -> Path:
    """SQLite の DB を ``dest_dir`` へバックアップし、作ったファイルのパスを返す。

    稼働中のサーバーがあってもよい(読み取りトランザクションで一貫したコピーを取る)。
    書き終えるまでは一時ファイルに書き、最後に名前を付け替える(途中で止まっても
    壊れたバックアップを残さない)。``keep`` が 1 以上なら、新しい順に ``keep`` 世代
    だけ残して古いバックアップを消す(ほかのファイルには触れない)。
    """
    source = _require_sqlite_file(db)
    if not source.exists():
        raise BackupError(f"データベースファイルがありません: {source}")
    folder = Path(dest_dir).expanduser()
    folder.mkdir(parents=True, exist_ok=True)
    stem = source.stem
    remove_stale_temp_files(folder, stem)
    # データベースに番号を付ける(初回だけ。バックアップのヘッダにもそのまま写る)。
    # 同じフォルダにある同じ名前の別のデータベースのバックアップを、世代の整理・
    # 前回の日時で区別するため(D3-16)。
    database_owner(source, assign=True)
    target = _destination(folder, stem, now or datetime.now())

    fd, tmp_name = tempfile.mkstemp(dir=folder, prefix=f".{stem}-", suffix=".tmp")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        with closing(sqlite3.connect(str(source), timeout=_lock_wait(db))) as src:
            _copy(src, tmp)
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            tmp.unlink()
    logger.info("バックアップを作成しました: %s", target)
    owner = backup_owner(target) or 0  # 写した時点のデータベースの番号
    if keep >= 1:
        # 作ったばかりのバックアップは必ず残す(時計の巻き戻りなどで名前の順が
        # 前後しても消さない)。ほかは新しい順に keep - 1 世代を残す。
        others = [
            entry
            for entry in list_backups(folder, stem, owner=owner)
            if entry != target
        ]
        for old in others[: max(len(others) - (keep - 1), 0)]:
            try:
                old.unlink()
                logger.info("古いバックアップを削除しました: %s", old)
            except OSError as exc:
                logger.warning("古いバックアップを削除できません: %s (%s)", old, exc)
    _warn_foreign(folder, stem, owner)
    return target


def validate_backup(src: str | Path) -> Path:
    """``src`` が復元に使える SQLite のファイルか確かめ、そのパスを返す。

    ファイルが無い・SQLite のデータベースとして読めない・テーブルが 1 つも無い
    ときは :class:`BackupError`。SQLite は空のファイルを空のデータベースとして開くため、
    コピーに失敗した 0 バイトのファイルで復元すると、データがすべて消えてしまう。
    """
    source = Path(src).expanduser()
    if not source.is_file():
        raise BackupError(f"バックアップファイルが見つかりません: {source}")
    stat = source.stat()
    key = (str(source.resolve()), stat.st_size, stat.st_mtime_ns)
    with _validated_lock:
        if key in _validated:
            return source
    with closing(sqlite3.connect(str(source))) as check:
        try:
            (tables,) = check.execute(
                "SELECT count(*) FROM sqlite_master WHERE type = 'table'"
            ).fetchone()
        except sqlite3.DatabaseError as exc:
            raise BackupError(
                f"SQLite のデータベースファイルとして読めません: {source} ({exc})"
            ) from exc
        # 中ほどのページが壊れていても上の確認は通る(D3-12)。全ページを読んで
        # 確かめ、壊れたファイルで健全な DB を置き換えないようにする。
        try:
            result = [row[0] for row in check.execute("PRAGMA quick_check(5)")]
        except sqlite3.DatabaseError as exc:
            result = [str(exc)]
    if not tables:
        raise BackupError(
            f"テーブルが 1 つも無いため、復元に使えません: {source}"
            "(空のファイルや、コピーに失敗したファイルではありませんか?)"
        )
    if result != ["ok"]:
        raise BackupError(
            f"バックアップファイルが壊れているため、復元に使えません: {source}"
            f"({'; '.join(str(r) for r in result[:2])})。別の世代のバックアップを"
            "使ってください"
        )
    with _validated_lock:
        _validated.add(key)
    return source


def save_damaged_copy(
    path: str | Path,
    dest_dir: str | Path,
    *,
    now: datetime | None = None,
) -> Path:
    """壊れて読めない SQLite のファイルを、そのまま(-wal なども)``dest_dir`` へ写す。

    バックアップ API で読めない DB を復元で置き換える前に残しておく(修復ツールで行を
    取り出せることがある)。名前は ``<stem>-damaged-YYYYmmdd-HHMMSS.db`` で、
    バックアップの世代(:func:`list_backups`)には数えない。
    """
    source = Path(path)
    folder = Path(dest_dir).expanduser()
    folder.mkdir(parents=True, exist_ok=True)
    base = f"{source.stem}-damaged-{(now or datetime.now()).strftime(_STAMP_FORMAT)}"
    target = folder / f"{base}.db"
    seq = 2
    while target.exists():  # 同じ秒に 2 回以上
        target = folder / f"{base}-{seq}.db"
        seq += 1
    shutil.copy2(source, target)
    for suffix in ("-wal", "-journal"):  # -shm は WAL から作り直せる
        side = source.with_name(source.name + suffix)
        if side.is_file():
            shutil.copy2(side, target.with_name(target.name + suffix))
    logger.info("壊れたデータベースをそのまま退避しました: %s", target)
    return target


def is_damaged(path: str | Path) -> bool:
    """``path`` が壊れていて SQLite のデータベースとして開けないか(読み取りのみで確かめる)。

    読み取り専用で開くため、ファイルを変えない(通常の接続は閉じるときに -wal を本体へ
    書き戻して消すことがある)。ロックで読めないときは壊れているとみなさない
    (待たずに確かめる)。
    """
    # resolve() は Windows の割り当て済みネットワークドライブを UNC パスに変え、SQLite が
    # その URI(file://server/...)を開けないため、絶対パスにするだけにする。
    path = Path(os.path.abspath(path))
    try:
        uri = f"{path.as_uri()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=0)) as conn:
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
    except sqlite3.DatabaseError as exc:
        return is_corrupt_error(exc)
    return False


def _replace_damaged(source: Path, target: Path) -> None:
    """壊れて開けない ``target`` を、``source`` の内容の新しいファイルで置き換える。

    バックアップ API は壊れたファイルへは書き込めないため、同じフォルダの一時ファイル
    へ写してから名前を付け替える。古い -wal / -shm / -journal は先に消す(残すと、
    次に開いたときに古いファイルの内容が新しいファイルへ書き戻されて壊れる)。
    """
    remove_stale_temp_files(target.parent, target.stem)
    fd, tmp_name = tempfile.mkstemp(
        dir=target.parent, prefix=f".{target.stem}-", suffix=".tmp"
    )
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        with closing(sqlite3.connect(str(source))) as src_conn:
            _copy(src_conn, tmp)
        with suppress(OSError):
            shutil.copymode(target, tmp)  # 元のファイルの権限を引き継ぐ
        for suffix in _SIDE_SUFFIXES:
            side = target.with_name(target.name + suffix)
            if side.exists():
                side.unlink()
        os.replace(tmp, target)
    except OSError as exc:
        raise BackupError(
            f"壊れたデータベースのファイルを置き換えられませんでした: {target}"
            f"({exc.strerror or exc}。ほかのプロセスが開いていないか確かめてください)"
        ) from exc
    finally:
        if tmp.exists():
            tmp.unlink()


def restore_database(src: str | Path, db: Database) -> None:
    """バックアップファイル ``src`` の内容で SQLite の DB を置き換える。

    **サーバーを停止してから実行すること。** 稼働中だと、復元と同時に書き込まれた
    データが失われ、画面のキャッシュも古いまま残る。オンラインバックアップ API で
    書き込むため、復元先が WAL モードでも -wal ファイルと矛盾しない。復元先が壊れて
    開けないときは、新しいファイルに写してから置き換える(壊れたファイルを残したい
    ときは、先に :func:`save_damaged_copy` で写しておく)。
    """
    target = _require_sqlite_file(db)
    source = Path(src).expanduser()
    if not source.is_file():
        raise BackupError(f"バックアップファイルが見つかりません: {source}")
    if target.exists() and source.resolve() == target.resolve():
        raise BackupError("復元元と復元先が同じファイルです")
    validate_backup(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    db.dispose()  # プールに残った接続を閉じる(復元後は新しい接続で読み直す)
    if target.exists() and is_damaged(target):
        _replace_damaged(source, target)
        logger.info(
            "壊れたデータベースを置き換えて復元しました: %s → %s", source, target
        )
        return
    with (
        closing(sqlite3.connect(str(source))) as src_conn,
        closing(sqlite3.connect(str(target), timeout=_lock_wait(db))) as dest,
    ):
        _online_copy(src_conn, dest, wait=_take_lock_wait(dest), what="復元")
    logger.info("データベースを復元しました: %s → %s", source, target)
