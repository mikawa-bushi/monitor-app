"""コンテナのエントリポイント(docker/entrypoint.sh)のテスト。

一般ユーザーで動く部分(デモの作成・config.py の確認・SQLite の保存先の書き込み確認・
コマンドの組み立て)を、プロジェクトとデモの場所を環境変数で一時フォルダに向けて確かめる。
``monitor-app`` は引数を表示するだけの偽物に置き換える。

root で起動したときの権限の切り替えは、``id``・``stat``・``setpriv``・``chown`` を
偽物に置き換えて確かめる(偽の ``setpriv`` は uid を変えずに、偽の ``id`` が返す uid を
切り替えてから続きを実行する)。本物の切り替えは Docker で確かめる(CI の docker ジョブ)。
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ENTRYPOINT = Path(__file__).parent.parent / "docker" / "entrypoint.sh"

pytestmark = pytest.mark.skipif(
    os.name == "nt" or shutil.which("sh") is None or os.geteuid() == 0,
    reason="POSIX の sh を一般ユーザーで実行できる環境だけで確かめる",
)

FAKE_MONITOR_APP = """#!/bin/sh
echo "ARGS: $*"
echo "CWD: $(pwd)"
echo "HOME: $HOME"
echo "UID: $(id -u)"
"""

# root を装う偽のコマンド。FAKE_UID が無ければ root(0)。
FAKE_ID = """#!/bin/sh
case $1 in
    -u) echo "${FAKE_UID:-0}" ;;
    -g) echo "${FAKE_GID:-0}" ;;
    *) exec /usr/bin/id "$@" ;;
esac
"""
# stat -c FORMAT PATH: 所有者は FAKE_OWNER(instances は FAKE_INSTANCES_OWNER)、
# 無ければ本物の所有者。
FAKE_STAT = """#!/bin/sh
format=$2
path=$3
case $path in
    */instances) owner=${FAKE_INSTANCES_OWNER:-} ;;
    *) owner=${FAKE_OWNER:-} ;;
esac
[ -n "$owner" ] || owner=$(ls -ldn "$path" | awk '{print $3 ":" $4}')
case $format in
    '%u:%g') echo "$owner" ;;
    '%u') echo "${owner%%:*}" ;;
    *) exit 2 ;;
esac
"""
FAKE_SETPRIV = """#!/bin/sh
uid=
gid=
while [ "$#" -gt 0 ]; do
    case $1 in
        --reuid=*) uid=${1#--reuid=} ;;
        --regid=*) gid=${1#--regid=} ;;
        --clear-groups | --no-new-privs | --inh-caps=* | --bounding-set=*) ;;
        --) shift; break ;;
        *) break ;;
    esac
    shift
done
if [ -n "${FAKE_SETPRIV_FAIL:-}" ]; then
    echo "setpriv: setresuid failed: Operation not permitted" >&2
    exit 127
fi
echo "setpriv $uid:$gid $1" >> "$FAKE_LOG"
FAKE_UID=$uid FAKE_GID=$gid exec "$@"
"""
FAKE_CHOWN = """#!/bin/sh
echo "chown $*" >> "$FAKE_LOG"
"""


def mountinfo(path: Path, *mounts: Path) -> Path:
    """/proc/self/mountinfo の形の一覧(ルートと ``mounts`` がマウントされている)。"""
    lines = ["22 1 8:1 / / rw,relatime shared:1 - ext4 /dev/sda1 rw"]
    lines += [
        f"{30 + n} 22 8:1 /srv/proj {mount} rw,relatime - ext4 /dev/sda1 rw"
        for n, mount in enumerate(mounts)
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def env(tmp_path) -> dict[str, str]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "monitor-app"
    fake.write_text(FAKE_MONITOR_APP, encoding="utf-8")
    fake.chmod(0o755)
    demo = tmp_path / "demo"
    (demo / "csv").mkdir(parents=True)
    (demo / "instances").mkdir()
    (demo / "config.py").write_text("# demo\n", encoding="utf-8")
    (demo / ".gitignore").write_text(".env\n", encoding="utf-8")
    (demo / "csv" / "users.csv").write_text("id,name\n1,a\n", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    base = {k: v for k, v in os.environ.items() if not k.startswith("MONITOR_")}
    return base | {
        "PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
        "MONITOR_PROJECT_DIR": str(project),
        "MONITOR_DEMO_DIR": str(demo),
        "MONITOR_TEST_MOUNTINFO": str(mountinfo(tmp_path / "mountinfo", project)),
    }


@pytest.fixture
def not_mounted(env, tmp_path) -> dict[str, str]:
    """-v を付けずに起動したコンテナ(/project はイメージの空のフォルダ)。"""
    other = mountinfo(tmp_path / "mountinfo-bare", tmp_path / "elsewhere")
    return env | {"MONITOR_TEST_MOUNTINFO": str(other)}


@pytest.fixture
def project(env) -> Path:
    return Path(env["MONITOR_PROJECT_DIR"])


@pytest.fixture
def existing(project) -> Path:
    """config.py と instances/ のある既存のプロジェクト。"""
    (project / "config.py").write_text("# mine\n", encoding="utf-8")
    (project / "instances").mkdir()
    return project


@pytest.fixture
def read_only():
    """フォルダやファイルを読み取り専用にし、テストの後で元に戻す。"""
    changed: list[tuple[Path, int]] = []

    def make(path: Path) -> Path:
        changed.append((path, stat.S_IMODE(path.stat().st_mode)))
        path.chmod(0o555 if path.is_dir() else 0o444)
        return path

    yield make
    for path, mode in reversed(changed):
        path.chmod(mode)


@pytest.fixture
def as_root(env, tmp_path) -> dict[str, str]:
    """root で起動したように見せる環境(偽の id・stat・setpriv・chown を PATH の先頭に置く)。"""
    bindir = tmp_path / "bin"
    for name, script in (
        ("id", FAKE_ID),
        ("stat", FAKE_STAT),
        ("setpriv", FAKE_SETPRIV),
        ("chown", FAKE_CHOWN),
    ):
        (bindir / name).write_text(script, encoding="utf-8")
        (bindir / name).chmod(0o755)
    log = tmp_path / "fake.log"
    log.write_text("", encoding="utf-8")
    return env | {"FAKE_LOG": str(log)}


def _fake_log(env: dict[str, str]) -> list[str]:
    return Path(env["FAKE_LOG"]).read_text(encoding="utf-8").splitlines()


def run(env: dict[str, str], *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["sh", str(ENTRYPOINT), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _error_lines(proc: subprocess.CompletedProcess) -> list[str]:
    return [line for line in proc.stderr.splitlines() if line]


def test_empty_project_gets_the_demo_and_runs_the_default_command(env, project):
    proc = run(env, "monitor-app", "runserver", "--import-csv-if-empty")
    assert proc.returncode == 0, proc.stderr
    assert "ARGS: runserver --import-csv-if-empty" in proc.stdout
    assert f"CWD: {project}" in proc.stdout
    assert (project / "config.py").read_text(encoding="utf-8") == "# demo\n"
    assert (project / ".gitignore").exists()  # ドットファイルもコピーする
    assert (project / "csv" / "users.csv").exists()
    assert "デモプロジェクト" in proc.stderr


def test_options_only_are_passed_to_runserver(env):
    proc = run(env, "--port", "8000")
    assert proc.returncode == 0, proc.stderr
    assert "ARGS: runserver --import-csv-if-empty --port 8000" in proc.stdout


def test_no_arguments_run_the_default_server(env):
    proc = run(env)
    assert proc.returncode == 0, proc.stderr
    assert "ARGS: runserver --import-csv-if-empty" in proc.stdout


def test_existing_project_is_used_as_is(env, existing):
    proc = run(env, "monitor-app", "runserver")
    assert proc.returncode == 0, proc.stderr
    assert (existing / "config.py").read_text(encoding="utf-8") == "# mine\n"
    assert not (existing / "csv").exists()  # デモは作らない
    assert "ARGS: runserver" in proc.stdout


def test_non_empty_folder_without_config_is_explained_in_three_lines(env, project):
    (project / "notes.txt").write_text("not a project\n", encoding="utf-8")
    proc = run(env, "monitor-app", "runserver")
    assert proc.returncode == 1
    lines = _error_lines(proc)
    assert len(lines) == 3, lines
    assert lines[0].startswith("エラー:") and "config.py" in lines[0]
    assert "ARGS" not in proc.stdout  # サーバーは起動しない
    assert not (project / "config.py").exists()


def test_read_only_empty_folder_cannot_get_a_demo(env, project, read_only):
    read_only(project)
    proc = run(env, "monitor-app", "runserver")
    assert proc.returncode == 1
    assert "デモプロジェクトを作れません" in proc.stderr
    assert len(_error_lines(proc)) == 3


def test_unwritable_instances_folder_is_reported(env, existing, read_only):
    read_only(existing / "instances")
    proc = run(env, "monitor-app", "runserver")
    assert proc.returncode == 1
    lines = _error_lines(proc)
    assert len(lines) == 3, lines
    assert f"{existing}/instances" in lines[0]
    assert f"uid={os.getuid()}" in lines[0]
    assert "ARGS" not in proc.stdout


def test_unwritable_database_file_is_reported(env, existing, read_only):
    database = existing / "instances" / "database.db"
    database.write_bytes(b"")
    read_only(database)
    proc = run(env, "monitor-app", "runserver")
    assert proc.returncode == 1
    assert "書き込めません" in proc.stderr


def test_missing_database_folder_is_fine_when_it_can_be_created(env, project):
    (project / "config.py").write_text("# mine\n", encoding="utf-8")  # instances/ なし
    proc = run(env, "monitor-app", "runserver")
    assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize(
    "line",
    [
        "MONITOR_DATABASE_URL=postgresql+psycopg://u:p@db:5432/monitor",
        "MONITOR_DATABASE_URL=sqlite:///:memory:",
        "export MONITOR_DATABASE_URL='mysql+pymysql://u:p@db/m'",
        'MONITOR_DATABASE_URL="sqlite:///elsewhere/app.db"',
        "MONITOR_DATABASE_URL=sqlite:///elsewhere/app.db  # 別フォルダに置く",
    ],
)
def test_database_url_from_dotenv_decides_what_to_check(env, existing, read_only, line):
    read_only(existing / "instances")  # 既定の保存先は使わない設定なら問題にしない
    (existing / ".env").write_text(f"# 設定\n{line}\n", encoding="utf-8")
    proc = run(env, "monitor-app", "runserver")
    assert proc.returncode == 0, proc.stderr


def test_absolute_sqlite_path_in_dotenv_is_checked(env, existing, tmp_path, read_only):
    data = tmp_path / "data"
    data.mkdir()
    read_only(data)
    url = f"sqlite:///{data}/app.db?timeout=5"  # 絶対パスは sqlite:////... になる
    (existing / ".env").write_text(f"MONITOR_DATABASE_URL={url}\n", encoding="utf-8")
    proc = run(env, "monitor-app", "runserver")
    assert proc.returncode == 1
    assert str(data) in proc.stderr


def test_environment_variable_wins_over_dotenv(env, existing, read_only):
    read_only(existing / "instances")
    (existing / ".env").write_text(
        "MONITOR_DATABASE_URL=sqlite:///instances/database.db\n", encoding="utf-8"
    )
    proc = run(env | {"MONITOR_DATABASE_URL": "sqlite://"}, "monitor-app", "runserver")
    assert proc.returncode == 0, proc.stderr


def test_other_commands_skip_the_project_checks(env, project):
    (project / "notes.txt").write_text("x\n", encoding="utf-8")
    proc = run(env, "sh", "-c", "echo hello from $(pwd)")
    assert proc.returncode == 0, proc.stderr
    assert "hello from" in proc.stdout
    assert not (project / "config.py").exists()


def test_bare_monitor_app_subcommands_are_run_with_monitor_app(env, project):
    """コマンドとして無い名前(check など)は monitor-app のサブコマンドとして実行する。"""
    proc = run(env, "check", "--data")
    assert proc.returncode == 0, proc.stderr
    assert "ARGS: check --data" in proc.stdout
    assert not (project / "config.py").exists()  # runserver 以外ではデモを作らない


def test_runserver_given_by_name_gets_the_project_checks(env, project):
    proc = run(env, "runserver", "--import-csv")
    assert proc.returncode == 0, proc.stderr
    assert "ARGS: runserver --import-csv" in proc.stdout
    assert (project / "config.py").exists()  # 空のフォルダにはデモを作る


def test_monitor_app_given_by_path_gets_the_project_checks(env, project, tmp_path):
    proc = run(env, str(tmp_path / "bin" / "monitor-app"), "runserver")
    assert proc.returncode == 0, proc.stderr
    assert (project / "config.py").exists()


def test_lost_and_found_does_not_count_as_content(env, project):
    """ext4 のボリューム(Kubernetes の PV など)が最初から持つ lost+found は無視する。"""
    (project / "lost+found").mkdir()
    proc = run(env, "monitor-app", "runserver")
    assert proc.returncode == 0, proc.stderr
    assert (project / "config.py").exists()
    assert (project / "lost+found").is_dir()


@pytest.mark.parametrize("style", ["separate", "equals"])
def test_config_option_names_the_project_file(env, project, style):
    (project / "prod.py").write_text("# prod\n", encoding="utf-8")
    option = ["--config", "prod.py"] if style == "separate" else ["--config=prod.py"]
    proc = run(env, "monitor-app", "runserver", *option)
    assert proc.returncode == 0, proc.stderr  # config.py が無くても止めない
    assert not (project / "config.py").exists()  # デモも作らない


def test_missing_config_option_file_is_left_to_runserver(env, project):
    (project / "notes.txt").write_text("x\n", encoding="utf-8")
    proc = run(env, "--config", "missing.py")
    assert (
        proc.returncode == 0
    ), proc.stderr  # runserver 自身が「見つかりません」と表示する
    assert "ARGS: runserver --import-csv-if-empty --config missing.py" in proc.stdout
    assert not (project / "config.py").exists()


def test_read_only_folder_owned_by_the_user_gets_a_mount_hint(env, existing, read_only):
    """所有者として動いているのに書けない(:ro でのマウントなど)なら --user の案内はしない。"""
    read_only(existing / "instances")
    lines = _error_lines(run(env, "monitor-app", "runserver"))
    assert len(lines) == 3, lines
    assert ":ro" in lines[1]
    assert "--user" not in lines[1]


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_unwritable_sqlite_side_files_are_reported(env, existing, read_only, suffix):
    (existing / "instances" / "database.db").write_bytes(b"")
    side = existing / "instances" / f"database.db{suffix}"
    side.write_bytes(b"")
    read_only(side)
    proc = run(env, "monitor-app", "runserver")
    assert proc.returncode == 1
    assert f"database.db{suffix}" in _error_lines(proc)[0]


def test_sqlite_urls_with_a_driver_are_checked(env, existing, read_only):
    read_only(existing / "instances")
    url = "sqlite+pysqlite:///instances/database.db"
    proc = run(env | {"MONITOR_DATABASE_URL": url}, "monitor-app", "runserver")
    assert proc.returncode == 1
    assert "書き込めません" in proc.stderr


# ---------------------------------------------------------------------------
# root で起動したとき(偽のコマンドで確かめる)
# ---------------------------------------------------------------------------
def test_root_switches_to_the_owner_of_the_project(as_root, existing):
    env = as_root | {"FAKE_OWNER": "1234:2345"}
    proc = run(env, "monitor-app", "runserver", "--import-csv-if-empty")
    assert proc.returncode == 0, proc.stderr
    # 切り替えられるかを true で確かめてから、このスクリプトを実行し直す
    assert _fake_log(env) == ["setpriv 1234:2345 true", "setpriv 1234:2345 /bin/sh"]
    assert "UID: 1234" in proc.stdout
    # root のホーム(/root)は読めないため、プロジェクトをホームにする
    assert f"HOME: {existing}" in proc.stdout
    assert "ARGS: runserver --import-csv-if-empty" in proc.stdout


def test_root_owned_project_stays_root(as_root, existing):
    env = as_root | {"FAKE_OWNER": "0:0"}
    proc = run(env, "monitor-app", "runserver")
    assert proc.returncode == 0, proc.stderr
    # uid は変えずに、権限(capability)を捨ててから実行し直す(#D6-04)
    assert _fake_log(env) == ["setpriv : true", "setpriv : /bin/sh"]
    assert "UID: 0" in proc.stdout


def test_empty_root_owned_instances_volume_is_given_to_the_owner(as_root, project):
    (project / "config.py").write_text("# mine\n", encoding="utf-8")
    (project / "instances").mkdir()
    env = as_root | {"FAKE_OWNER": "1234:1234", "FAKE_INSTANCES_OWNER": "0:0"}
    proc = run(env, "monitor-app", "runserver")
    assert proc.returncode == 0, proc.stderr
    # -h: 確かめた後にリンクへすり替えられても、リンク先の所有者は変えない(#D6-16)
    assert _fake_log(env)[0] == f"chown -h 1234:1234 {project}/instances"


def test_instances_with_content_are_not_touched(as_root, existing):
    (existing / "instances" / "database.db").write_bytes(b"")
    env = as_root | {"FAKE_OWNER": "1234:1234", "FAKE_INSTANCES_OWNER": "0:0"}
    proc = run(env, "monitor-app", "runserver")
    assert proc.returncode == 0, proc.stderr
    assert not any(line.startswith("chown") for line in _fake_log(env))


def test_without_setuid_capability_it_warns_and_continues(as_root, existing):
    """--cap-drop ALL などで切り替えられなければ、警告して root のまま続ける。"""
    env = as_root | {"FAKE_OWNER": "1234:1234", "FAKE_SETPRIV_FAIL": "1"}
    proc = run(env, "monitor-app", "runserver")
    assert proc.returncode == 0, proc.stderr
    assert "切り替えられない" in proc.stderr
    assert "UID: 0" in proc.stdout
    assert "ARGS: runserver" in proc.stdout


def test_other_commands_also_run_as_the_owner(as_root, existing):
    env = as_root | {"FAKE_OWNER": "1234:1234"}
    proc = run(env, "check")
    assert proc.returncode == 0, proc.stderr
    assert "UID: 1234" in proc.stdout
    assert "ARGS: check" in proc.stdout


def test_instances_symlink_is_not_chowned(as_root, project, tmp_path):
    """instances がリンクなら、リンク先(コンテナ内のフォルダ)の所有者を変えない。"""
    (project / "config.py").write_text("# mine\n", encoding="utf-8")
    target = tmp_path / "elsewhere"
    target.mkdir()
    (project / "instances").symlink_to(target, target_is_directory=True)
    env = as_root | {"FAKE_OWNER": "1234:1234", "FAKE_INSTANCES_OWNER": "0:0"}
    proc = run(env, "monitor-app", "runserver")
    assert proc.returncode == 0, proc.stderr
    assert not any(line.startswith("chown") for line in _fake_log(env))


# ls -ldn PATH: instances の所有者だけを FAKE_LS_INSTANCES_UID に見せる。
FAKE_LS = """#!/bin/sh
last=
for arg in "$@"; do last=$arg; done
case $last in
    instances | */instances)
        if [ -n "${FAKE_LS_INSTANCES_UID:-}" ]; then
            echo "dr-xr-xr-x 2 $FAKE_LS_INSTANCES_UID $FAKE_LS_INSTANCES_UID 40 Jan 1 00:00 $last"
            exit 0
        fi
        ;;
esac
exec /bin/ls "$@"
"""


def _install(env: dict[str, str], name: str, script: str) -> None:
    path = Path(env["PATH"].split(os.pathsep)[0]) / name
    path.write_text(script, encoding="utf-8")
    path.chmod(0o755)


def test_contents_owned_by_someone_else_get_a_chown_hint(env, existing, read_only):
    """フォルダの所有者として動いているが、中の instances/ が別の所有者(以前 root で
    作られたなど)なら、所有者をそろえる chown を具体的な uid:gid で案内する。"""
    _install(env, "ls", FAKE_LS)
    read_only(existing / "instances")
    proc = run(env | {"FAKE_LS_INSTANCES_UID": "0"}, "monitor-app", "runserver")
    lines = _error_lines(proc)
    assert proc.returncode == 1 and len(lines) == 3, lines
    assert f"sudo chown -R {os.getuid()}:{os.getgid()}" in lines[1]
    assert "--user" not in lines[1]


def test_running_as_another_user_gets_the_user_hint(env, existing, read_only):
    """--user でフォルダの所有者と違うユーザーを指定したときは --user を外す案内。"""
    _install(env, "id", FAKE_ID)
    read_only(existing / "instances")
    proc = run(
        env | {"FAKE_UID": "4321", "FAKE_GID": "4321"}, "monitor-app", "runserver"
    )
    lines = _error_lines(proc)
    assert proc.returncode == 1 and len(lines) == 3, lines
    assert "uid=4321" in lines[0]
    assert "--user" in lines[1]


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (("monitor-app", "runserver", "--import-csv-if-empty"), "ARGS: demo"),
        ((), "ARGS: demo"),
        (("--port", "8000"), "ARGS: demo --port 8000"),
        (("--host=0.0.0.0", "--port=8000"), "ARGS: demo --host=0.0.0.0 --port=8000"),
        (("monitor-app", "runserver", "--host", "::"), "ARGS: demo --host ::"),
    ],
)
def test_nothing_mounted_runs_the_live_demo(not_mounted, project, args, expected):
    proc = run(not_mounted, *args)
    assert proc.returncode == 0, proc.stderr
    assert expected in proc.stdout.splitlines()
    assert list(project.iterdir()) == []
    assert "デモを起動します" in proc.stderr


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (("--import-csv",), "ARGS: runserver --import-csv-if-empty --import-csv"),
        (("--reload",), "ARGS: runserver --import-csv-if-empty --reload"),
        (("monitor-app", "runserver", "--port"), "ARGS: runserver --port"),
    ],
)
def test_runserver_only_options_keep_the_starter_project(
    not_mounted, project, args, expected
):
    proc = run(not_mounted, *args)
    assert proc.returncode == 0, proc.stderr
    assert expected in proc.stdout.splitlines()
    assert (project / "config.py").read_text(encoding="utf-8") == "# demo\n"


def test_nothing_mounted_with_a_config_file_runs_runserver(not_mounted, project):
    (project / "config.py").write_text("# mine\n", encoding="utf-8")
    proc = run(not_mounted)
    assert proc.returncode == 0, proc.stderr
    assert "ARGS: runserver --import-csv-if-empty" in proc.stdout
    assert (project / "config.py").read_text(encoding="utf-8") == "# mine\n"


def test_nothing_mounted_but_named_config_runs_runserver(not_mounted, project):
    proc = run(not_mounted, "monitor-app", "runserver", "--config", "other.py")
    assert proc.returncode == 0, proc.stderr
    assert "ARGS: runserver --config other.py" in proc.stdout
    assert list(project.iterdir()) == []


def test_nothing_mounted_help_and_other_commands_are_unchanged(not_mounted, project):
    proc = run(not_mounted, "monitor-app", "runserver", "--help")
    assert "ARGS: runserver --help" in proc.stdout
    proc = run(not_mounted, "check")
    assert "ARGS: check" in proc.stdout
    proc = run(not_mounted, "demo", "--port", "8000")
    assert "ARGS: demo --port 8000" in proc.stdout
    assert list(project.iterdir()) == []


def test_mounted_empty_folder_still_gets_the_starter_project(env, project):
    proc = run(env)
    assert proc.returncode == 0, proc.stderr
    assert "ARGS: runserver --import-csv-if-empty" in proc.stdout
    assert (project / "config.py").read_text(encoding="utf-8") == "# demo\n"


def test_root_without_a_mount_drops_privileges_then_runs_the_demo(
    as_root, not_mounted, project
):
    env = as_root | {
        "MONITOR_TEST_MOUNTINFO": not_mounted["MONITOR_TEST_MOUNTINFO"],
        "FAKE_OWNER": "1000:1000",
    }
    proc = run(env, "monitor-app", "runserver", "--import-csv-if-empty")
    assert proc.returncode == 0, proc.stderr
    assert _fake_log(env) == ["setpriv 1000:1000 true", "setpriv 1000:1000 /bin/sh"]
    assert "UID: 1000" in proc.stdout
    assert "ARGS: demo" in proc.stdout.splitlines()
    assert list(project.iterdir()) == []
