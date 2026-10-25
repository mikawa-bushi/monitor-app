"""実行時設定。環境変数 ``MONITOR_*`` または ``.env`` で与える。

DB 接続情報やポートなど「環境ごとに変わる値」をここに集約し、宣言的設定
(:class:`~monitor_app.settings.declarative.MonitorConfig`)から切り離す。
これにより DB パスワードを ``config.py`` に直書きせずに済む。
"""

from __future__ import annotations

import difflib
import os
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Literal, NamedTuple

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: SQLite の PRAGMA journal_mode に指定できる値。
SQLITE_JOURNAL_MODES = ("wal", "delete", "truncate", "persist", "memory", "off")
#: SQLite の PRAGMA synchronous に指定できる値。
SQLITE_SYNCHRONOUS_MODES = ("off", "normal", "full", "extra")
#: ログのレベルに指定できる値(大文字・小文字は問わない。詳しい順に debug・trace まで)。
LOG_LEVELS = ("critical", "error", "warning", "info", "debug", "trace")
#: 従来どおり受け付ける Python の logging の別名(warning・critical・trace と同じ)。
_LOG_LEVEL_ALIASES = ("warn", "fatal", "notset")

#: AppSettings のフィールド以外で monitor-app(と配布するテンプレート)が読む変数。
#: - MONITOR_CONFIG_PATH … app_factory が読む config.py の場所(runserver が設定する)
#: - MONITOR_DEBUG … CLI のエラーでトレースバックを表示する
#: - MONITOR_IMAGE / MONITOR_IMAGE_TAG … Docker Compose のテンプレートが使うイメージの
#:   名前とタグ
#: - MONITOR_PROJECT_DIR / MONITOR_DEMO_DIR … Docker イメージの起動スクリプトが使う
#:   プロジェクトと雛形のフォルダ
EXTRA_ENV_KEYS = frozenset(
    {
        "MONITOR_CONFIG_PATH",
        "MONITOR_DEBUG",
        "MONITOR_IMAGE",
        "MONITOR_IMAGE_TAG",
        "MONITOR_PROJECT_DIR",
        "MONITOR_DEMO_DIR",
    }
)
#: この接頭辞で始まる変数は未知として扱わない(テスト用の DB 接続先など)。
EXTRA_ENV_PREFIXES = ("MONITOR_TEST_",)
#: ``MONITOR_`` で始まらないキーを「綴りの誤り」とみなす類似度(.env のみ対象)。
_NEAR_MISS_CUTOFF = 0.85
#: 未知の ``MONITOR_*`` の綴りの誤りとみなす類似度。共通の接頭辞 ``MONITOR_`` を除いた
#: 名前どうしで比べる(接頭辞まで比べると、MONITOR_NAME → MONITOR_API_KEY のように
#: 無関係な変数にも提案してしまうため)。
_SUFFIX_CUTOFF = 0.7
#: 似ていないがよく使われる別名(接頭辞を除いた名前 → 正しい変数名)。
_ENV_ALIASES = {
    "DB_URL": "MONITOR_DATABASE_URL",
    "DB": "MONITOR_DATABASE_URL",
    "DATABASE": "MONITOR_DATABASE_URL",
}


class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MONITOR_", env_file=".env", extra="ignore"
    )

    #: SQLAlchemy 接続 URL。例:
    #:   sqlite:///instances/database.db
    #:   mysql+pymysql://user:pass@localhost:3306/monitor_app
    #:   postgresql+pg8000://user:pass@localhost:5432/monitor_app
    database_url: str = "sqlite:///instances/database.db"

    # --- SQLite のチューニング(ファイル DB のみ。MySQL/PostgreSQL には効かない)---
    #: ジャーナルモード。wal は読み取りと書き込みが互いを待たず、書き込みごとの
    #: fsync も大幅に減る。ネットワーク共有(SMB/NFS)上の DB では delete を指定する。
    sqlite_journal_mode: str = "wal"
    #: 同期モード。wal + normal は電源断で直近のコミットが失われ得るが DB は壊れない。
    #: 厳密な永続性が必要なら full。
    sqlite_synchronous: str = "normal"
    #: ロック待ちの上限(ミリ秒)。
    sqlite_busy_timeout_ms: int = 5000
    #: サーバー DB(MySQL/PostgreSQL)に接続できるまで起動時に待つ最大秒数。
    #: Docker Compose で DB より先にアプリが起動した場合に備える。0 で待たない。
    db_wait_timeout: float = 15.0

    host: str = "127.0.0.1"
    port: int = 9990

    #: 設定されている場合のみ API キー認証が有効になる。
    api_key: str | None = None
    #: True なら読み取り系エンドポイントも API キーで保護する。
    protect_reads: bool = False

    #: CORS を許可するオリジン。空ならクロスオリジンを許可しない(同一オリジンのみ)。
    cors_origins: list[str] = Field(default_factory=list)

    #: ログのレベル(critical / error / warning / info / debug / trace)。
    log_level: str = "INFO"
    csv_dir: Path = Path("csv")
    #: CSV 取り込み時の文字エンコーディング。既定の "auto" は UTF-8(BOM の有無は
    #: 問わない)として読み、読めなければ ``csv_encoding_fallback`` の文字コード
    #: (既定: cp932 = Excel の「CSV(コンマ区切り)」)で読み直す。"utf-8-sig" や
    #: "cp932" などを指定すると、その文字コードだけで読む(自動判別しない)。
    csv_encoding: str = "auto"
    #: ``csv_encoding="auto"`` で UTF-8 として読めなかったときに順に試す文字コード
    #: (カンマ区切り)。
    csv_encoding_fallback: str = "cp932"

    # --- ビュー共有キャッシュ(#18)---
    #: ビュー結果を共有キャッシュする TTL(ミリ秒)。SSE/ダッシュボードの
    #: 多重クエリを抑える。書き込み時は無効化される。0 でキャッシュ無効。
    view_cache_ttl_ms: int = 1000
    #: データが変わっていない(変更を検知できる)間、キャッシュを使い続ける最大時間
    #: (ミリ秒)。``now`` などの時刻依存の SQL があっても表示が古くなりすぎない上限。
    view_cache_max_age_ms: int = 15000
    #: ビューのキャッシュが使うメモリのおおよその上限(MB)。超えたら最近使っていない
    #: 結果から捨てる。0 以下で上限なし(件数の上限 256 件だけ)。
    view_cache_max_mb: int = 256

    # --- データ取り込み(フェーズ1・C)---
    #: csv/ フォルダを監視し、変更されたファイルを自動で取り込む。
    ingest_watch: bool = False
    #: フォルダ監視のポーリング間隔(秒)。
    ingest_interval: float = 5.0
    #: 自動取り込みのモード。replace=置換 / append=追記(時系列向き)。
    ingest_mode: Literal["replace", "append"] = "replace"

    # --- 監査ログ(フェーズ4・G)---
    audit_enabled: bool = False

    # --- バックアップ・保持期間 ---
    #: 定期バックアップの保存先ディレクトリ(SQLite のみ)。未設定なら定期実行しない。
    backup_dir: Path | None = None
    #: 定期バックアップの間隔(時間)。0 以下で無効。
    backup_interval_hours: float = 24.0
    #: 残すバックアップの世代数。
    backup_keep: int = 7
    #: ``TableDef.retention`` による古い行の削除を実行する間隔(分)。
    retention_interval_minutes: float = 10.0

    # --- Web 配信(リバースプロキシ・圧縮・ログ)---
    #: リバースプロキシ配下で公開するときのサブパス(例: ``/monitor``)。
    root_path: str = ""
    #: プロキシヘッダ(X-Forwarded-*)を信頼する送信元 IP(カンマ区切り、``*`` で全て)。
    #: 未設定なら uvicorn の既定(環境変数 FORWARDED_ALLOW_IPS、なければ 127.0.0.1)。
    forwarded_allow_ips: str | None = None
    #: 大きなレスポンス(ビューの JSON・静的ファイル)を gzip 圧縮する。
    compress_responses: bool = True
    #: uvicorn のアクセスログ(1 リクエスト 1 行)を出す。
    access_log: bool = True
    #: ログの形式。text(人が読む)/ json(ログ収集基盤向け)。
    log_format: Literal["text", "json"] = "text"

    # --- 通知チャネル(フェーズ1・A)。未設定のチャネルは無効 ---
    webhook_url: str | None = None  # Slack/Teams/汎用 JSON POST
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    alert_email_to: str | None = None  # カンマ区切り可
    #: SMTP の暗号化方式。starttls(587 番)/ ssl(SMTPS・465 番)/ none(社内リレー等)。
    smtp_security: Literal["starttls", "ssl", "none"] = "starttls"
    #: 通知の送信に失敗したときの再試行回数(間隔は指数的に伸ばす)。
    notify_retries: int = 2
    #: SMTP(starttls / ssl)でサーバーの証明書を検証する。社内の自己署名証明書の
    #: 中継サーバーなら、CA を SSL_CERT_FILE で指定するか false にする。
    smtp_tls_verify: bool = True

    # --- 公開範囲・API ドキュメント ---
    #: 受け付ける Host ヘッダの名前(カンマ区切り。``*.example.com`` でサブドメイン、
    #: ``*`` で確かめない)。未設定なら、127.0.0.1 などこの PC だけで待ち受けるときに
    #: localhost・IP アドレス以外の名前を断る(DNS リバインディング対策)。
    allowed_hosts: str | None = None
    #: API ドキュメント(/docs・/redoc・/openapi.json)を公開する。
    #: ``protect_reads`` が有効ならログインが必要。
    api_docs: bool = True

    @field_validator("api_key", mode="before")
    @classmethod
    def _blank_api_key_is_unset(cls, value: object) -> object:
        # 空の MONITOR_API_KEY(.env の「MONITOR_API_KEY=」や Compose の未設定の変数)は
        # 未設定と同じにする。空のキーは認証が有効に見えても、誰でも作れるセッション
        # Cookie が通ってしまうため(#D6-01)。
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("sqlite_journal_mode", "sqlite_synchronous", mode="before")
    @classmethod
    def _lower(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("sqlite_journal_mode")
    @classmethod
    def _check_journal_mode(cls, value: str) -> str:
        if value not in SQLITE_JOURNAL_MODES:
            raise ValueError(
                f"sqlite_journal_mode は {', '.join(SQLITE_JOURNAL_MODES)} のいずれかです"
            )
        return value

    @field_validator("sqlite_synchronous")
    @classmethod
    def _check_synchronous(cls, value: str) -> str:
        if value not in SQLITE_SYNCHRONOUS_MODES:
            raise ValueError(
                "sqlite_synchronous は "
                f"{', '.join(SQLITE_SYNCHRONOUS_MODES)} のいずれかです"
            )
        return value

    @field_validator("log_level")
    @classmethod
    def _check_log_level(cls, value: str) -> str:
        # 不正な名前はログの設定(dictConfig)や uvicorn の起動で長いトレースバックに
        # なるため、ここで短いメッセージにする(書かれた大文字・小文字はそのまま使う)。
        if value.strip().lower() not in LOG_LEVELS + _LOG_LEVEL_ALIASES:
            raise ValueError(f"log_level は {', '.join(LOG_LEVELS)} のいずれかです")
        return value.strip()

    @field_validator("root_path")
    @classmethod
    def _normalize_root_path(cls, value: str) -> str:
        # "/monitor/" や "monitor" も受け付け、"/monitor" 形式(末尾スラッシュなし)に揃える。
        value = value.strip().rstrip("/")
        if value and not value.startswith("/"):
            value = "/" + value
        return value


# ---------------------------------------------------------------------------
# プロジェクトのフォルダの .env(作業フォルダの外の config.py を使うとき)
# ---------------------------------------------------------------------------
def same_path(a: Path, b: Path) -> bool:
    """同じファイル・フォルダか(無いものは絶対パスの表記で比べる)。"""
    try:
        return a.samefile(b)
    except OSError:
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(
            os.path.abspath(b)
        )


def project_env_files(
    config_path: str | os.PathLike | None,
) -> tuple[Path, ...] | None:
    """config.py のフォルダの ``.env`` も読むときの ``_env_file``(DX-06)。

    ``--config`` などで作業フォルダの外の config.py を使うとき、そのフォルダの .env が
    作業フォルダの .env と別のファイルなら ``(<config.py のフォルダ>/.env, .env)`` を
    返す(後ろのファイルほど優先するため、作業フォルダの .env が優先。環境変数は
    どちらよりも優先)。読み足すファイルが無ければ None(既定の .env だけを読む)。
    """
    if config_path is None:
        return None
    defaults = _default_env_files()
    project_env = Path(os.path.abspath(Path(config_path).expanduser())).parent / ".env"
    if not project_env.is_file():
        return None
    if any(same_path(project_env, default) for default in defaults):
        return None
    return (project_env, *defaults)


def load_app_settings(
    config_path: str | os.PathLike | None = None, **values: object
) -> AppSettings:
    """実行時設定を読み込む(config.py のフォルダの .env も読む。:func:`project_env_files`)。

    ``values`` は AppSettings にそのまま渡す値(.env・環境変数より優先)。
    """
    env_files = project_env_files(config_path)
    if env_files is None:
        return AppSettings(**values)
    return AppSettings(_env_file=env_files, **values)


# ---------------------------------------------------------------------------
# 未知の MONITOR_* 変数(綴りの誤り)の検出
# ---------------------------------------------------------------------------
class UnknownEnvKey(NamedTuple):
    """設定として読まれない ``MONITOR_*`` 変数(綴りの誤りの可能性)。"""

    key: str
    #: 見つかった場所。``"環境変数"`` または .env ファイルのパス。
    source: str
    #: 最も近い正しい変数名(見つからなければ None)。
    suggestion: str | None


def known_env_keys() -> frozenset[str]:
    """monitor-app が読む ``MONITOR_*`` 変数名(大文字)。"""
    prefix = str(AppSettings.model_config.get("env_prefix") or "").upper()
    names = {f"{prefix}{name}".upper() for name in AppSettings.model_fields}
    return frozenset(names | EXTRA_ENV_KEYS)


def _default_env_files() -> list[Path]:
    env_file = AppSettings.model_config.get("env_file")
    if env_file is None:
        return []
    if isinstance(env_file, (str, os.PathLike)):
        return [Path(env_file)]
    return [Path(p) for p in env_file]


def _read_env_file(path: Path) -> list[str]:
    """.env のキー一覧(読めないファイルは空)。"""
    from dotenv import dotenv_values

    encoding = AppSettings.model_config.get("env_file_encoding") or "utf-8"
    try:
        if not path.is_file():
            return []
        return [key for key in dotenv_values(path, encoding=encoding) if key]
    except (OSError, UnicodeDecodeError):
        return []


def find_unknown_env_keys(
    environ: Mapping[str, str] | None = None,
    env_files: Iterable[str | os.PathLike] | None = None,
) -> list[UnknownEnvKey]:
    """環境変数と .env にある、設定として読まれない ``MONITOR_*`` を列挙する。

    pydantic-settings は未知の変数を黙って無視するため、``MONITOR_API_KYE`` のような
    綴りの誤りで認証が無効のまま起動してしまう(#CRIT-04)。それを見つけて正しい
    変数名を提案する。.env では ``MONTOR_API_KEY`` のように接頭辞の綴りを誤った
    キーも対象にする(環境変数は他のツールの変数が多いため ``MONITOR_`` 始まりのみ)。

    ``environ`` の既定は ``os.environ``、``env_files`` の既定は AppSettings が読む
    ``.env``(カレントディレクトリからの相対パス)。
    """
    known = known_env_keys()
    candidates = sorted(known)
    prefix = str(AppSettings.model_config.get("env_prefix") or "").upper()
    found: list[UnknownEnvKey] = []

    by_suffix = {
        name[len(prefix) :]: name for name in candidates if name.startswith(prefix)
    }

    def suggest(key: str) -> str | None:
        suffix = key.upper()[len(prefix) :]
        if suffix in _ENV_ALIASES:
            return _ENV_ALIASES[suffix]
        matches = difflib.get_close_matches(
            suffix, list(by_suffix), n=1, cutoff=_SUFFIX_CUTOFF
        )
        return by_suffix[matches[0]] if matches else None

    def unknown(key: str) -> bool:
        upper = key.upper()
        return (
            upper.startswith(prefix)
            and upper not in known
            and not upper.startswith(EXTRA_ENV_PREFIXES)
        )

    environ = os.environ if environ is None else environ
    for key in sorted(environ):
        if unknown(key):
            found.append(UnknownEnvKey(key, "環境変数", suggest(key)))

    files = _default_env_files() if env_files is None else [Path(p) for p in env_files]
    for path in files:
        for key in _read_env_file(path):
            if unknown(key):
                found.append(UnknownEnvKey(key, str(path), suggest(key)))
            elif not key.upper().startswith(prefix):
                near = difflib.get_close_matches(
                    key.upper(), candidates, n=1, cutoff=_NEAR_MISS_CUTOFF
                )
                if near:
                    found.append(UnknownEnvKey(key, str(path), near[0]))
    return found


# ---------------------------------------------------------------------------
# 実行時設定の値の誤り(MONITOR_PORT=abc など)の短い説明
# ---------------------------------------------------------------------------
#: 値を表示しない設定(パスワード・キー・接続先 URL などの秘密を含み得る)。
_SECRET_FIELD = re.compile(r"pass|pwd|secret|token|key|url", re.IGNORECASE)
#: 1 つのメッセージに並べる誤りの数の上限。
_MAX_LISTED_ERRORS = 3
#: 表示する値の長さの上限。
_MAX_SHOWN_VALUE = 40
#: pydantic のエラーの種類 → 直し方。
_ERROR_REASONS = {
    "int_parsing": "整数を指定してください",
    "int_from_float": "整数を指定してください",
    "int_type": "整数を指定してください",
    "float_parsing": "数値を指定してください",
    "float_type": "数値を指定してください",
    "bool_parsing": "true / false のいずれかを指定してください",
    "bool_type": "true / false のいずれかを指定してください",
    "missing": "値を指定してください",
}


def _env_name(field: str) -> str:
    prefix = str(AppSettings.model_config.get("env_prefix") or "")
    return f"{prefix}{field}".upper()


def _list_hint(field: str) -> str:
    """JSON で書く必要のある設定(リストなど)の例。"""
    annotation = str(AppSettings.model_fields[field].annotation)
    if "list" in annotation.lower():
        return '(リストは JSON 形式で指定します。例: ["http://a.example", "http://b.example"])'
    return "(JSON 形式で指定してください)"


def _describe_validation_error(error: Mapping) -> str:
    loc = error.get("loc") or ()
    field = str(loc[0]) if loc else ""
    name = _env_name(field) if field in AppSettings.model_fields else "MONITOR_*"
    kind = str(error.get("type", ""))
    ctx = error.get("ctx") or {}
    if kind == "literal_error":
        allowed = re.findall(r"'([^']*)'", str(ctx.get("expected", "")))
        reason = (
            f"{' / '.join(allowed)} のいずれかを指定してください"
            if allowed
            else "指定できる値ではありません"
        )
    elif kind == "value_error" and ctx.get("error") is not None:
        reason = str(ctx["error"])
    else:
        reason = _ERROR_REASONS.get(kind, "値の形式が正しくありません")
    value = error.get("input")
    if isinstance(value, str) and not _SECRET_FIELD.search(field):
        if len(value) > _MAX_SHOWN_VALUE:
            value = value[: _MAX_SHOWN_VALUE - 3] + "..."
        return f"{name}='{value}'({reason})"
    return f"{name}({reason})"


def settings_error_message(exc: BaseException) -> str | None:
    """実行時設定(:class:`AppSettings`)を作るときの値の誤りを、1 行の説明にする。

    ``MONITOR_PORT=abc`` や ``MONITOR_LOG_FORMAT=xml`` のような誤りで、pydantic の
    長いエラーやトレースバックの代わりに、変数名と指定できる値を示す。対象外の例外なら
    None。値も示すが、秘密を含み得る設定(パスワード・キー・URL)の値は出さない。
    """
    from pydantic import ValidationError

    if isinstance(exc, ValidationError):
        if exc.title != AppSettings.__name__:
            return None
        errors = exc.errors()
        parts = [_describe_validation_error(e) for e in errors[:_MAX_LISTED_ERRORS]]
        if len(errors) > _MAX_LISTED_ERRORS:
            parts.append(f"ほか {len(errors) - _MAX_LISTED_ERRORS} 件")
        return "実行時設定(環境変数・.env)の値が正しくありません: " + "、".join(parts)
    # pydantic-settings の SettingsError(版によって置き場所が違うため名前で判定する):
    # リストなどの値を JSON として読めないとき。
    kind = type(exc)
    if kind.__name__ == "SettingsError" and kind.__module__.startswith(
        "pydantic_settings"
    ):
        match = re.search(r'field "([^"]+)"', str(exc))
        if match is None or match.group(1) not in AppSettings.model_fields:
            return f"実行時設定(環境変数・.env)の値を読み取れません: {exc}"
        field = match.group(1)
        return (
            f"実行時設定(環境変数・.env)の値を読み取れません: {_env_name(field)}"
            f"{_list_hint(field)}"
        )
    return None
