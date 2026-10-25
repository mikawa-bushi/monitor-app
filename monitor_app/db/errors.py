"""DB ドライバの例外を「利用者にとっての意味」で分類する。

同じ失敗でもドライバごとに例外クラスが違う(pg8000 は外部キー違反や文字列長超過を
``ProgrammingError`` で送出し、psycopg は ``IntegrityError`` / ``DataError`` を使う)。
そのため例外クラスではなく SQLSTATE(PostgreSQL)・エラー番号(MySQL)・メッセージ
(SQLite)で判定する(CRIT-01)。HTTP の例外ハンドラ・CLI のエラー表示・起動時の
DB 待機が共有する。

ここで作るメッセージは利用者にそのまま見せてよい定型文だけで構成し、SQL 文や
パラメータ(入力値)は含めない。
"""

from __future__ import annotations

import re

from sqlalchemy.exc import DataError, IntegrityError

#: 分類の結果。
CONFLICT = "conflict"  # 一意・外部キー・NOT NULL などの制約違反(409)
INVALID = "invalid"  # 値が列の型・長さ・範囲に合わない(422)
SCHEMA = "schema"  # 列・テーブル・主キーが config.py と DB で食い違う(500)
BUSY = "busy"  # ほかの書き込みのロック待ちの時間切れ・デッドロック(503。再送すれば通る)
OTHER = "other"  # それ以外(接続断など。500)

#: スキーマの食い違いを表す SQLSTATE。42703=列なし、42P01=表なし、42P10=ON CONFLICT
#: の対象に一意制約がない(主キーの無い古い表)。42S22/42S02 は MySQL の列なし/表なし。
_SCHEMA_STATES = frozenset({"42703", "42P01", "42P10", "42S22", "42S02"})
#: ロック待ち・競合を表す SQLSTATE。40001=直列化の失敗、40P01=デッドロック、
#: 55P03=ロックを取れない(lock_timeout / NOWAIT)。
_BUSY_STATES = frozenset({"40001", "40P01", "55P03"})

#: MySQL のエラー番号。
_MYSQL_CONFLICT = frozenset(
    {1022, 1048, 1062, 1169, 1216, 1217, 1451, 1452, 1586, 3819}
)
_MYSQL_INVALID = frozenset({1264, 1265, 1292, 1366, 1367, 1406, 1411, 3140})
_MYSQL_SCHEMA = frozenset({1054, 1146})
#: 1205=ロック待ちの時間切れ、1213=デッドロック。
_MYSQL_BUSY = frozenset({1205, 1213})
#: 接続時の恒久的な失敗(待っても直らない): 認証失敗・DB への権限なし・DB が無い。
_MYSQL_PERMANENT_CONNECT = frozenset({1044, 1045, 1049, 1698})

#: SQLite のメッセージ(小文字)に含まれる、スキーマの食い違いを表す語句。
_SQLITE_SCHEMA_PATTERNS = (
    "no such column",
    "no such table",
    "has no column named",
    "does not match any primary key or unique constraint",
)
#: SQLite のメッセージ(小文字)に含まれる、ロック待ちの時間切れを表す語句。
_SQLITE_BUSY_PATTERNS = (
    "database is locked",
    "database table is locked",
    "database is busy",
)

#: psycopg は接続時のエラーに SQLSTATE を付けないため、恒久的な失敗を文言で見分ける。
_PG_PERMANENT_CONNECT_PATTERNS = (
    "password authentication failed",
    "no pg_hba.conf entry",
    "does not exist",  # database "x" / role "x" does not exist
    "authentication failed",
)


def _orig(exc: BaseException) -> BaseException:
    """SQLAlchemy の例外なら元の DBAPI 例外を、それ以外は ``exc`` 自身を返す。"""
    orig = getattr(exc, "orig", None)
    return orig if isinstance(orig, BaseException) else exc


def sqlstate(exc: BaseException) -> str | None:
    """例外から 5 文字の SQLSTATE を取り出す(取り出せなければ None)。

    psycopg/psycopg2 は属性 ``sqlstate``/``pgcode``、pg8000 は ``args[0]`` の dict
    (キー ``C``)、PyMySQL 1.2 以降は属性 ``sqlstate`` に持つ。
    """
    orig = _orig(exc)
    for attr in ("sqlstate", "pgcode"):
        code = getattr(orig, attr, None)
        if isinstance(code, str) and len(code) == 5:
            return code
    args = getattr(orig, "args", ())
    if args and isinstance(args[0], dict):
        code = args[0].get("C")
        if isinstance(code, str) and len(code) == 5:
            return code
    return None


def mysql_errno(exc: BaseException) -> int | None:
    """MySQL(PyMySQL / mysqlclient)のエラー番号。他のドライバでは None。"""
    orig = _orig(exc)
    module = type(orig).__module__ or ""
    if not module.startswith(("pymysql", "MySQLdb", "mysql", "mariadb")):
        return None
    args = getattr(orig, "args", ())
    if args and isinstance(args[0], int):
        return args[0]
    return None


def driver_message(exc: BaseException) -> str:
    """ドライバのエラー文の要点を 1 行で返す(SQL 文やパラメータは含めない)。

    pg8000 はサーバーの応答を dict のまま ``args[0]`` に入れるため、メッセージ
    (キー ``M``)だけを取り出す。それ以外は文字列表現の 1 行目。
    """
    orig = _orig(exc)
    args = getattr(orig, "args", ())
    if args and isinstance(args[0], dict) and args[0].get("M"):
        return str(args[0]["M"]).strip()
    lines = str(orig).strip().splitlines()
    return lines[0] if lines else type(orig).__name__


def _detail(exc: BaseException) -> str:
    """分類と名前の抽出に使う全文(pg8000 は M と D を連結する)。"""
    orig = _orig(exc)
    args = getattr(orig, "args", ())
    if args and isinstance(args[0], dict):
        return " ".join(str(args[0].get(k, "")) for k in ("M", "D")).strip()
    return str(orig)


def classify(exc: BaseException) -> str:
    """DB の例外を :data:`CONFLICT` / :data:`INVALID` / :data:`SCHEMA` / :data:`BUSY` /
    :data:`OTHER` に分類する。"""
    state = sqlstate(exc)
    if state is not None:
        if state.startswith("23"):
            return CONFLICT
        if state.startswith("22"):
            return INVALID
        if state in _SCHEMA_STATES:
            return SCHEMA
        if state in _BUSY_STATES:
            return BUSY
    errno = mysql_errno(exc)
    if errno is not None:
        if errno in _MYSQL_CONFLICT:
            return CONFLICT
        if errno in _MYSQL_INVALID:
            return INVALID
        if errno in _MYSQL_SCHEMA:
            return SCHEMA
        if errno in _MYSQL_BUSY:
            return BUSY
    if isinstance(exc, IntegrityError):
        return CONFLICT
    if isinstance(exc, DataError):
        return INVALID
    text = _detail(exc).lower()
    if any(p in text for p in _SQLITE_SCHEMA_PATTERNS):
        return SCHEMA
    if any(p in text for p in _SQLITE_BUSY_PATTERNS):
        return BUSY
    return OTHER


def is_permanent_connect_error(exc: BaseException) -> bool:
    """接続の失敗が、待っても直らない種類(認証・DB 名の誤り)か。

    起動時の DB 待機はこれを待たずに即座に失敗させる。判定できないものは
    一時的な失敗(DB の起動待ち)とみなす。
    """
    state = sqlstate(exc)
    if state is not None and (state.startswith("28") or state == "3D000"):
        return True
    errno = mysql_errno(exc)
    if errno is not None:
        return errno in _MYSQL_PERMANENT_CONNECT
    text = _detail(exc).lower()
    return any(p in text for p in _PG_PERMANENT_CONNECT_PATTERNS)


# ---------------------------------------------------------------------------
# 利用者向けの定型メッセージ
# ---------------------------------------------------------------------------
_NAME_PATTERNS = (
    re.compile(r"column '([^']+)'"),  # MySQL
    re.compile(r'column "([^"]+)"'),  # PostgreSQL
    re.compile(r"column ([\w.]+) does not exist"),  # PostgreSQL(表名つきの参照)
    re.compile(r"Key \(([^)]+)\)="),  # PostgreSQL の DETAIL(一意・外部キー)
    re.compile(r"for key '([^']+)'"),  # MySQL の一意キー
    re.compile(r"constraint failed: ([\w.]+)"),  # SQLite
    re.compile(r"no such (?:column|table): ([\w.]+)"),  # SQLite
    re.compile(r"has no column named (\w+)"),  # SQLite
    re.compile(r'relation "([^"]+)"'),  # PostgreSQL の表
    re.compile(r"Table '([^']+)'"),  # MySQL の表
)


def _object_name(exc: BaseException) -> str | None:
    """エラー文に含まれる列名・表名(あれば)。値は含まない。"""
    text = _detail(exc)
    for pattern in _NAME_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1)
    return None


def _with_name(message: str, exc: BaseException) -> str:
    name = _object_name(exc)
    return f"{message}({name})" if name else message


def conflict_message(exc: BaseException) -> str:
    """制約違反(409)の利用者向けメッセージ。"""
    state = sqlstate(exc) or ""
    errno = mysql_errno(exc)
    text = _detail(exc).lower()
    if state == "23505" or errno in (1022, 1062, 1169, 1586) or "unique" in text:
        base = "同じキーのレコードが既に存在します(主キーの重複)"
    elif state == "23503" or errno in (1216, 1217, 1451, 1452) or "foreign key" in text:
        base = (
            "外部キー制約に違反しました(参照先のレコードが存在しないか、"
            "ほかのレコードから参照されています)"
        )
    elif state == "23502" or errno == 1048 or "not null" in text:
        base = "必須の列に値がありません"
    elif state == "23514" or errno == 3819 or "check constraint" in text:
        base = "列の制約(CHECK)に違反しました"
    else:
        base = "データベースの制約に違反しました"
    return _with_name(base, exc)


def invalid_message(exc: BaseException) -> str:
    """値の不正(422)の利用者向けメッセージ。"""
    state = sqlstate(exc) or ""
    errno = mysql_errno(exc)
    if state == "22001" or errno == 1406:
        base = "文字列が列の最大長を超えています(str 列は 255 文字まで)"
    elif state == "22003" or errno == 1264:
        base = "数値が列の範囲を超えています"
    elif state in ("22007", "22008") or errno == 1292:
        base = "日付・時刻の値が不正です"
    elif state == "22P02" or errno in (1265, 1366):
        base = "値の形式が列の型と合いません"
    else:
        base = "値がデータベースの列の制限に合いません"
    return _with_name(base, exc)


def busy_message() -> str:
    """ロック待ちの時間切れ(503)の利用者向けメッセージ。"""
    return (
        "データベースがほかの書き込み(CSV の取り込みなど)で使用中です。"
        "少し待ってからもう一度送ってください"
    )


def schema_message(exc: BaseException | None = None) -> str:
    """スキーマの食い違い(500)の利用者向けメッセージ。"""
    base = "データベースの表定義が config.py と一致しません"
    if exc is not None:
        base = _with_name(base, exc)
    return base + "。monitor-app check で差分を確認してください"
