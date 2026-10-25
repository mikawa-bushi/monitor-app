"""デプロイ用ファイル(Docker Compose・systemd・Windows)の生成。

``monitor-app deploy`` と ``monitor-app startproject --deploy`` が使う。雛形は
``monitor_app/scaffold/deploy/`` にあり、プロジェクトの場所・いま動いている Python・
ユーザー名・ポートを埋め込んで書き出す。既存のファイルは ``force=True`` でない限り
上書きしない。

- ``docker``  … ``compose.yaml``(公開イメージを使い、このフォルダを /project に
  マウント)と ``.dockerignore``
- ``systemd`` … ``monitor-app.service``(``python -m monitor_app runserver
  --import-csv-if-empty`` を常駐させる)
- ``windows`` … ``start-monitor.bat``(ログを ``logs\\monitor.log`` に追記)と
  ``install-task.ps1``(タスク スケジューラで起動時に開始)

デプロイしたサーバーは LAN から使われることが多いため、プロジェクトの ``.env`` に
API キーが無ければ推測されにくいキーを書き足す(:func:`ensure_api_key`。手元で
``runserver`` / ``serve`` を試すだけならキーは不要のまま)。
"""

from __future__ import annotations

import contextlib
import getpass
import importlib.resources as resources
import json
import os
import re
import shlex
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath

from .security import KeyProblem, api_key_problems, generate_api_key

#: 生成できる対象(この順に生成・表示する)。
TARGETS = ("docker", "systemd", "windows")
#: すべての対象を表す指定。
ALL = "all"

#: 起動コマンドの引数(再起動しても画面・API で入力した行を消さない取り込み方)。
RUNSERVER_ARGS = ("-m", "monitor_app", "runserver", "--import-csv-if-empty")

# IANA のタイムゾーン名として受け付ける文字(YAML にそのまま埋め込むため厳しめにする)。
_TZ_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_+\-]*(/[A-Za-z0-9_+\-]+)*$")


class DeployError(ValueError):
    """デプロイ用ファイルを生成できない(対象名の誤り・使えないパスなど)。"""


@dataclass(frozen=True)
class DeployFile:
    """書き出す 1 ファイル。"""

    #: 対象(``docker`` / ``systemd`` / ``windows``)。
    target: str
    #: 書き出す先(絶対パス)。
    path: Path
    #: 内容(改行は ``\\n``。書き出すときに ``newline`` へ変換する)。
    content: str
    encoding: str = "utf-8"
    newline: str = "\n"


def parse_targets(values: Iterable[str]) -> list[str]:
    """``["docker,systemd"]`` や ``["all"]`` を、重複のない対象名のリストにする。

    空なら ``all`` とみなす。並びは :data:`TARGETS` の順。未知の名前は
    :class:`DeployError`。
    """
    names = [
        part.strip().lower()
        for value in values
        for part in value.split(",")
        if part.strip()
    ]
    unknown = sorted({n for n in names if n not in (*TARGETS, ALL)})
    if unknown:
        raise DeployError(
            f"不明な対象です: {', '.join(unknown)}"
            f"({' / '.join((*TARGETS, ALL))} から選んでください)"
        )
    if not names or ALL in names:
        return list(TARGETS)
    return [t for t in TARGETS if t in names]


def native_target(platform: str | None = None) -> str | None:
    """この OS で常駐させる対象(Linux: ``systemd``、Windows: ``windows``、ほか: None)。"""
    platform = platform or sys.platform
    if platform.startswith("linux"):
        return "systemd"
    if platform == "win32":
        return "windows"
    return None


def default_targets(platform: str | None = None) -> list[str]:
    """対象を指定しなかったときに生成する対象(Docker と、この OS 向けのもの)。"""
    native = native_target(platform)
    return [t for t in TARGETS if t in ("docker", native)]


def foreign_targets(targets: Iterable[str], platform: str | None = None) -> list[str]:
    """この OS 向けではない対象(この PC の Python のパスを書くため、そのままでは動かない)。"""
    native = native_target(platform)
    return [t for t in targets if t in ("systemd", "windows") and t != native]


def _template(name: str) -> str:
    path = resources.files("monitor_app") / "scaffold" / "deploy" / name
    return path.read_text(encoding="utf-8")


def _current_user() -> str:
    """ユニットの User=。sudo で実行されたら元のユーザーにする。"""
    if hasattr(os, "geteuid") and os.geteuid() == 0 and os.environ.get("SUDO_USER"):
        return os.environ["SUDO_USER"]
    try:
        import pwd

        return pwd.getpwuid(os.geteuid()).pw_name
    except (ImportError, KeyError, AttributeError):  # Windows・名前の無い uid
        return getpass.getuser()


def local_timezone_name() -> str | None:
    """このマシンのタイムゾーン名(例: ``Asia/Tokyo``)。分からなければ None。

    環境変数 ``TZ``、なければ ``/etc/localtime`` のリンク先から求める(Linux・macOS)。
    """
    candidates = [os.environ.get("TZ", "").strip()]
    try:
        target = os.path.realpath("/etc/localtime")
    except OSError:  # pragma: no cover - realpath は通常失敗しない
        target = ""
    if "zoneinfo/" in target:
        candidates.append(target.split("zoneinfo/", 1)[1])
    for name in candidates:
        for prefix in ("posix/", "right/"):
            name = name.removeprefix(prefix)
        if name and _TZ_NAME.match(name) and name not in ("localtime",):
            return name
    return None


def is_ephemeral_python(executable: str | None = None) -> bool:
    """``uvx`` などが作る一時的な環境の Python か(後で消えるためユニットに書けない)。"""
    parts = Path(executable or sys.executable).parts
    return any(p.startswith(("archive-v", "builds-v")) for p in parts)


# ---------------------------------------------------------------------------
# 対象ごとの内容
# ---------------------------------------------------------------------------
def _docker_config_arg(config: str) -> str:
    """コンテナ内(/project が作業フォルダ)で使う設定ファイルのパス。"""
    path = PurePath(config)
    if path.is_absolute() or PurePosixPath(config).is_absolute() or ".." in path.parts:
        raise DeployError(
            "Docker では、設定ファイルをプロジェクトのフォルダからの相対パスで"
            f"指定してください: {config}"
        )
    return path.as_posix()


def _docker_files(
    project: Path, port: int | None, config: str | None = None
) -> list[DeployFile]:
    compose = _template("compose.yaml")
    if config is not None:
        # エントリポイントはオプションだけの引数を runserver に渡す。
        command = json.dumps(["--config", _docker_config_arg(config)])
        image_line = next(
            line for line in compose.splitlines() if line.strip().startswith("image:")
        )
        compose = compose.replace(
            image_line, f"{image_line}\n    command: {command}", 1
        )
    if port is not None:
        compose = compose.replace("${MONITOR_PORT:-9990}", f"${{MONITOR_PORT:-{port}}}")
        compose = compose.replace("(既定 9990)", f"(既定 {port})")
        compose = compose.replace("http://localhost:9990/", f"http://localhost:{port}/")
    tz = local_timezone_name()
    if tz:
        compose = compose.replace("TZ: ${TZ:-UTC}", f"TZ: ${{TZ:-{tz}}}")
    return [
        DeployFile("docker", project / "compose.yaml", compose),
        DeployFile("docker", project / ".dockerignore", _template("dockerignore")),
    ]


def _systemd_escape(value: str) -> str:
    """systemd の指定子(``%``)を無効にする(WorkingDirectory= などの値)。"""
    return value.replace("%", "%%")


def _systemd_arg(arg: str) -> str:
    """ExecStart= の 1 引数。``%`` と ``$`` を無効にし、必要なら二重引用符で囲む。"""
    escaped = _systemd_escape(arg).replace("$", "$$")
    if arg and not any(c.isspace() or c in "\"'\\;" for c in arg):
        return escaped
    escaped = escaped.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _systemd_files(
    project: Path, port: int | None, python: str, config: str | None = None
) -> list[DeployFile]:
    # Linux の絶対パス(/...)か、この OS の絶対パス(Windows で生成した場合)。
    if not (PurePosixPath(python).is_absolute() or Path(python).is_absolute()):
        raise DeployError(f"Python の実行ファイルの絶対パスが分かりません: {python!r}")
    for value in (str(project), python, config or ""):
        if any(c in value for c in "\r\n"):
            raise DeployError(f"改行を含むパスはユニットに書けません: {value!r}")
    args = [python, *RUNSERVER_ARGS]
    if port is not None:
        args += ["--port", str(port)]
    if config is not None:
        args += ["--config", config]
    unit = project / "monitor-app.service"
    content = (
        _template("monitor-app.service")
        .replace("@UNIT_FILE@", shlex.quote(str(unit)))
        .replace("@DESCRIPTION@", _systemd_escape(str(project)))
        .replace("@USER@", _current_user())
        .replace("@WORKING_DIRECTORY@", _systemd_escape(str(project)))
        .replace("@EXEC_START@", " ".join(_systemd_arg(a) for a in args))
    )
    return [DeployFile("systemd", unit, content)]


def _bat_python(project: Path, python: str) -> str:
    """.bat に書く Python のパス。プロジェクト内(.venv など)なら ``%~dp0`` 基準にする。

    フォルダごと移動・コピーしても動き、別のユーザー(SYSTEM)で実行しても同じ場所を指す。
    """
    exe, root = PureWindowsPath(python), PureWindowsPath(project)
    try:
        relative = exe.relative_to(root)
    except ValueError:
        return python.replace("%", "%%")
    return "%~dp0" + str(relative).replace("%", "%%")


def _windows_files(
    project: Path, port: int | None, python: str, config: str | None = None
) -> list[DeployFile]:
    bat = _template("start-monitor.bat").replace(
        "@PYTHON@", _bat_python(project, python)
    )
    args = f" --port {port}" if port is not None else ""
    if config is not None:
        if any(c in config for c in '"\r\n'):
            raise DeployError(f"この設定ファイルのパスは .bat に書けません: {config!r}")
        args += ' --config "' + config.replace("%", "%%") + '"'
    bat = bat.replace("@ARGS@", args)
    if bat.isascii():
        bat, encoding = bat.replace("@CHCP@", ""), "ascii"
    else:
        # 日本語などを含むパスは、コードページを UTF-8 に切り替えてから読ませる。
        bat, encoding = bat.replace("@CHCP@", "chcp 65001 >nul\n"), "utf-8"
    return [
        DeployFile(
            "windows",
            project / "start-monitor.bat",
            bat,
            encoding=encoding,
            newline="\r\n",
        ),
        # PowerShell 5.1 は BOM の無いスクリプトを ANSI(cp932 など)として読むため BOM を付ける。
        DeployFile(
            "windows",
            project / "install-task.ps1",
            _template("install-task.ps1"),
            encoding="utf-8-sig",
            newline="\r\n",
        ),
    ]


def plan_deploy_files(
    project_dir: str | os.PathLike[str],
    targets: Iterable[str],
    *,
    port: int | None = None,
    python: str | None = None,
    config: str | None = None,
) -> list[DeployFile]:
    """書き出すファイルと内容を決める(まだ書かない)。

    ``python`` は起動に使う Python(既定: いま動いている ``sys.executable``)。
    ``config`` は runserver の ``--config`` に渡す設定ファイル(プロジェクトのフォルダ
    からの相対パスか絶対パス。既定: 渡さない = ``config.py``)。
    """
    project = Path(project_dir).expanduser().resolve()
    if port is not None and not 1 <= port <= 65535:
        raise DeployError(f"ポート番号は 1〜65535 です: {port}")
    python = python or sys.executable
    files: list[DeployFile] = []
    for target in parse_targets(targets):
        if target == "docker":
            files += _docker_files(project, port, config)
        elif target == "systemd":
            files += _systemd_files(project, port, python, config)
        else:
            files += _windows_files(project, port, python, config)
    return files


def write_deploy_file(file: DeployFile) -> None:
    file.path.parent.mkdir(parents=True, exist_ok=True)
    with file.path.open("w", encoding=file.encoding, newline=file.newline) as fh:
        fh.write(file.content)


def write_deploy_files(
    files: Iterable[DeployFile], *, force: bool = False
) -> tuple[list[DeployFile], list[DeployFile]]:
    """``files`` を書き出し、``(書いたファイル, 既にあるため飛ばしたファイル)`` を返す。

    既にあるファイルは ``force=True`` でない限り上書きしない。書き込みに失敗したら
    その ``OSError`` を送出する(``filename`` は書けなかったファイルのパス)。
    ``monitor-app deploy`` と :func:`render_deploy_files` が共有する。
    """
    written: list[DeployFile] = []
    skipped: list[DeployFile] = []
    for file in files:
        if file.path.exists() and not force:
            skipped.append(file)
            continue
        try:
            write_deploy_file(file)
        except OSError as exc:
            exc.filename = str(file.path)
            raise
        written.append(file)
    return written, skipped


def render_deploy_files(
    project_dir: str | os.PathLike[str],
    targets: Iterable[str],
    *,
    force: bool = False,
    port: int | None = None,
    config: str | None = None,
) -> list[Path]:
    """デプロイ用ファイルを ``project_dir`` に書き出し、書いたファイルのパスを返す。

    既にあるファイルは ``force=True`` でない限り上書きせずに飛ばす(戻り値に含めない)。
    API キーは書かない(:func:`ensure_api_key` を別に呼ぶ)。
    """
    files = plan_deploy_files(project_dir, targets, port=port, config=config)
    written, _skipped = write_deploy_files(files, force=force)
    return [file.path for file in written]


# ---------------------------------------------------------------------------
# API キー(.env)
# ---------------------------------------------------------------------------
#: API キーの変数名。
API_KEY_ENV = "MONITOR_API_KEY"
#: 生成したキーの前に書く説明(新しく作る .env の先頭にも書く)。
_KEY_COMMENT = (
    "# 書き込み(画面での追加・変更・削除、API、CSV の取り込み)に必要な API キー\n"
    "# (monitor-app deploy が生成)。画面ではログインでこのキーを入力し、API では\n"
    "# X-API-Key ヘッダに付ける。閲覧も保護するなら MONITOR_PROTECT_READS=true。\n"
)
_NEW_ENV_HEADER = (
    "# monitor-app の実行時設定(MONITOR_*)。ほかの設定は .env.example を参照。\n"
)


@dataclass(frozen=True)
class ApiKeyResult:
    """:func:`ensure_api_key` の結果。"""

    #: プロジェクトの .env。
    path: Path
    #: いまキーを生成して書いたか(False なら既に設定されていた)。
    generated: bool
    #: 既に設定されていたキーの問題(説明用の値・短すぎるなど)。
    problems: list[KeyProblem] = field(default_factory=list)


def _configured_api_key(path: Path) -> str | None:
    from dotenv import dotenv_values

    if not path.is_file():
        return None
    try:
        value = dotenv_values(path, encoding="utf-8").get(API_KEY_ENV)
    except (OSError, UnicodeDecodeError) as exc:
        raise DeployError(f".env を読めません: {path}({exc})") from exc
    return value if value and value.strip() else None


def _give_to_sudo_user(path: Path) -> None:
    """sudo で実行したときは、作った .env を元のユーザーのものにする(サービスが読めるように)。"""
    uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
    if not (hasattr(os, "geteuid") and os.geteuid() == 0 and uid and gid):
        return
    with contextlib.suppress(OSError, ValueError):
        os.chown(path, int(uid), int(gid))


def ensure_api_key(project_dir: str | os.PathLike[str]) -> ApiKeyResult:
    """プロジェクトの ``.env`` に API キーが無ければ、推測されにくいキーを書き足す。

    ``.env`` が無ければ作る(所有者だけが読み書きできる権限)。既にキーがあれば変えず、
    説明用の値(``change-me`` など)や短すぎるキーなら ``problems`` で知らせる。
    空の ``MONITOR_API_KEY=`` は未設定とみなし、後ろにキーを書き足す(後の行が優先)。
    """
    path = Path(project_dir).expanduser().resolve() / ".env"
    existing = _configured_api_key(path)
    if existing is not None:
        return ApiKeyResult(path, False, api_key_problems(existing))
    line = f"{_KEY_COMMENT}{API_KEY_ENV}={generate_api_key()}\n"
    try:
        if path.exists():
            text = path.read_bytes()
            separator = "" if not text or text.endswith(b"\n") else "\n"
            with path.open("a", encoding="utf-8", newline="\n") as fh:
                fh.write(f"{separator}\n{line}")
        else:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(f"{_NEW_ENV_HEADER}{line}")
            _give_to_sudo_user(path)
    except OSError as exc:
        raise DeployError(
            f"API キーを書き込めません: {path}({exc.strerror or exc})"
        ) from exc
    return ApiKeyResult(path, True)
