"""コンテナの堅牢化: エントリポイントの権限の扱い・HEALTHCHECK・イメージと Compose の設定。

- 所有者へ切り替えるときも、root のまま動かすときも権限(capability)を捨て、
  no_new_privs を付ける。root の所有のフォルダ(rootful Docker で -v に存在しない
  フォルダを指定した場合など)では、直し方を表示する
- --cap-drop ALL で切り替えられず書き込めないときは、必要な権限を示して止まる
- ``docker run <image> --version`` はバージョンを表示し、``--help`` ではデモを作らない
- HEALTHCHECK は HTTP_PROXY を使わず、root なら nobody に切り替えてから確かめる
- イメージのスクリプトは手元の umask に左右されない権限で、setuid のプログラムは無い

エントリポイントは ``id``・``stat``・``setpriv``・``chown``・``uname`` を偽物に置き換えて
確かめる(本物の切り替えは Docker で確かめる。CI の docker ジョブ)。
"""

from __future__ import annotations

import http.server
import importlib.util
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parent.parent
ENTRYPOINT = ROOT / "docker" / "entrypoint.sh"
HEALTHCHECK = ROOT / "docker" / "healthcheck.py"

posix_only = pytest.mark.skipif(
    os.name == "nt" or shutil.which("sh") is None or os.geteuid() == 0,
    reason="POSIX の sh を一般ユーザーで実行できる環境だけで確かめる",
)

FAKE_MONITOR_APP = """#!/bin/sh
echo "ARGS: $*"
echo "UID: $(id -u)"
"""
FAKE_ID = """#!/bin/sh
case $1 in
    -u) echo "${FAKE_UID:-0}" ;;
    -g) echo "${FAKE_GID:-0}" ;;
    *) exec /usr/bin/id "$@" ;;
esac
"""
FAKE_STAT = """#!/bin/sh
owner=${FAKE_OWNER:-}
[ -n "$owner" ] || owner=$(ls -ldn "$3" | awk '{print $3 ":" $4}')
case $2 in
    '%u:%g') echo "$owner" ;;
    '%u') echo "${owner%%:*}" ;;
esac
"""
# 偽の setpriv: 渡されたオプションを記録する。FAKE_NO_SETPCAP なら境界(bounding set)の
# 変更を、FAKE_NO_SETUID なら uid の切り替えを、FAKE_SETPRIV_FAIL ならすべてを失敗させる。
FAKE_SETPRIV = """#!/bin/sh
uid=
gid=
flags=
while [ "$#" -gt 0 ]; do
    case $1 in
        --reuid=*) uid=${1#--reuid=} ;;
        --regid=*) gid=${1#--regid=} ;;
        --clear-groups) ;;
        --bounding-set=*)
            [ -z "${FAKE_NO_SETPCAP:-}" ] || exit 1
            flags="$flags $1" ;;
        --no-new-privs | --inh-caps=*) flags="$flags $1" ;;
        --) shift; break ;;
        *) break ;;
    esac
    shift
done
[ -z "${FAKE_SETPRIV_FAIL:-}" ] || exit 127
if [ -n "$uid" ] && [ -n "${FAKE_NO_SETUID:-}" ]; then
    exit 1
fi
echo "setpriv uid=$uid flags=${flags# } cmd=$1" >> "$FAKE_LOG"
if [ -n "$uid" ]; then
    FAKE_UID=$uid FAKE_GID=$gid exec "$@"
fi
exec "$@"
"""
FAKE_CHOWN = """#!/bin/sh
echo "chown $*" >> "$FAKE_LOG"
"""
FAKE_UNAME = """#!/bin/sh
echo "${FAKE_KERNEL:-6.8.0-45-generic}"
"""
ROOTFUL_MAP = "         0          0 4294967295\n"
ROOTLESS_MAP = "         0       1000          1\n         1     100000      65536\n"
HARDENED = "--bounding-set=-all --inh-caps=-all --no-new-privs"


@pytest.fixture
def env(tmp_path) -> dict[str, str]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, script in (
        ("monitor-app", FAKE_MONITOR_APP),
        ("id", FAKE_ID),
        ("stat", FAKE_STAT),
        ("setpriv", FAKE_SETPRIV),
        ("chown", FAKE_CHOWN),
        ("uname", FAKE_UNAME),
    ):
        (bindir / name).write_text(script, encoding="utf-8")
        (bindir / name).chmod(0o755)
    demo = tmp_path / "demo"
    (demo / "csv").mkdir(parents=True)
    (demo / "config.py").write_text("# demo\n", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    uid_map = tmp_path / "uid_map"
    uid_map.write_text(ROOTFUL_MAP, encoding="utf-8")
    log = tmp_path / "fake.log"
    log.write_text("", encoding="utf-8")
    base = {k: v for k, v in os.environ.items() if not k.startswith("MONITOR_")}
    return base | {
        "PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
        "MONITOR_PROJECT_DIR": str(project),
        "MONITOR_DEMO_DIR": str(demo),
        "MONITOR_TEST_UID_MAP": str(uid_map),
        "FAKE_LOG": str(log),
    }


@pytest.fixture
def project(env) -> Path:
    path = Path(env["MONITOR_PROJECT_DIR"])
    (path / "config.py").write_text("# mine\n", encoding="utf-8")
    (path / "instances").mkdir()
    return path


def run(env: dict[str, str], *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["sh", str(ENTRYPOINT), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def log(env: dict[str, str]) -> list[str]:
    return Path(env["FAKE_LOG"]).read_text(encoding="utf-8").splitlines()


# ---------------------------------------------------------------------------
# エントリポイント: 権限
# ---------------------------------------------------------------------------
@posix_only
def test_switching_to_the_owner_drops_capabilities(env, project):
    env = env | {"FAKE_OWNER": "1234:2345"}
    proc = run(env, "monitor-app", "runserver")
    assert proc.returncode == 0, proc.stderr
    assert log(env) == [
        f"setpriv uid=1234 flags={HARDENED} cmd=true",
        f"setpriv uid=1234 flags={HARDENED} cmd=/bin/sh",
    ]
    assert "UID: 1234" in proc.stdout


@posix_only
def test_without_setpcap_the_switch_still_sets_no_new_privs(env, project):
    """--cap-drop ALL --cap-add SETUID --cap-add SETGID(Compose の既定)の場合。"""
    env = env | {"FAKE_OWNER": "1234:1234", "FAKE_NO_SETPCAP": "1"}
    proc = run(env, "monitor-app", "runserver")
    assert proc.returncode == 0, proc.stderr
    assert (
        log(env)[-1]
        == "setpriv uid=1234 flags=--inh-caps=-all --no-new-privs cmd=/bin/sh"
    )
    assert "UID: 1234" in proc.stdout


@posix_only
def test_root_owned_folder_keeps_uid_0_without_capabilities_and_warns(env, project):
    env = env | {"FAKE_OWNER": "0:0"}
    proc = run(env, "monitor-app", "runserver")
    assert proc.returncode == 0, proc.stderr
    # 権限を捨てて 1 回だけ実行し直す(実行し直した後は繰り返さない)
    assert log(env) == [
        f"setpriv uid= flags={HARDENED} cmd=true",
        f"setpriv uid= flags={HARDENED} cmd=/bin/sh",
    ]
    assert "UID: 0" in proc.stdout and "ARGS: runserver" in proc.stdout
    warnings = [line for line in proc.stderr.splitlines() if "root のまま" in line]
    assert len(warnings) == 1
    assert "sudo chown -R $(id -u):$(id -g)" in warnings[0]
    assert "_MONITOR_APP_ROOT_LIMITED" not in proc.stdout


@posix_only
@pytest.mark.parametrize(
    ("uid_map", "kernel"),
    [
        (ROOTLESS_MAP, "6.8.0-45-generic"),  # rootless Docker・Podman
        (ROOTFUL_MAP, "6.10.14-linuxkit"),  # Docker Desktop(macOS・Windows)
        (ROOTFUL_MAP, "5.15.167.4-microsoft-standard-WSL2"),
    ],
)
def test_root_owned_folder_is_normal_for_rootless_and_desktop(
    env, project, uid_map, kernel
):
    Path(env["MONITOR_TEST_UID_MAP"]).write_text(uid_map, encoding="utf-8")
    env = env | {"FAKE_OWNER": "0:0", "FAKE_KERNEL": kernel}
    proc = run(env, "monitor-app", "runserver")
    assert proc.returncode == 0, proc.stderr
    assert "root のまま" not in proc.stderr
    assert log(env)[-1] == f"setpriv uid= flags={HARDENED} cmd=/bin/sh"


@posix_only
def test_cap_drop_all_on_an_unwritable_folder_names_the_missing_capabilities(
    env, project
):
    env = env | {
        "FAKE_OWNER": "1234:1234",
        "FAKE_NO_SETUID": "1",
        "FAKE_NO_SETPCAP": "1",
    }
    project.chmod(0o555)
    try:
        proc = run(env, "monitor-app", "runserver")
    finally:
        project.chmod(0o755)
    assert proc.returncode == 1
    lines = [line for line in proc.stderr.splitlines() if line]
    assert len(lines) == 3, lines
    assert "SETUID・SETGID" in lines[0]
    assert "--cap-add SETUID --cap-add SETGID --cap-add CHOWN" in lines[1]
    assert "--user 1234:1234" in lines[2]
    assert "ARGS" not in proc.stdout


@posix_only
def test_cap_drop_all_on_a_writable_folder_warns_and_limits_root(env, project):
    env = env | {
        "FAKE_OWNER": "1234:1234",
        "FAKE_NO_SETUID": "1",
        "FAKE_NO_SETPCAP": "1",
    }
    proc = run(env, "monitor-app", "runserver")
    assert proc.returncode == 0, proc.stderr
    assert "--cap-add SETUID --cap-add SETGID" in proc.stderr
    assert (
        log(env)[-1] == "setpriv uid= flags=--inh-caps=-all --no-new-privs cmd=/bin/sh"
    )
    assert "UID: 0" in proc.stdout


@posix_only
def test_instances_owned_by_the_user_are_left_alone_and_chown_skips_links(env):
    project = Path(env["MONITOR_PROJECT_DIR"])
    (project / "config.py").write_text("# mine\n", encoding="utf-8")
    (project / "instances").mkdir()
    env = env | {"FAKE_OWNER": "1234:1234"}
    # instances も 1234 に見えるため chown は不要
    proc = run(env, "monitor-app", "runserver")
    assert proc.returncode == 0, proc.stderr
    assert not any(line.startswith("chown") for line in log(env))
    text = ENTRYPOINT.read_text(encoding="utf-8")
    assert 'chown -h "$owner" "$PROJECT_DIR/instances"' in text


# ---------------------------------------------------------------------------
# エントリポイント: 引数
# ---------------------------------------------------------------------------
@posix_only
@pytest.mark.parametrize("flag", ["--version", "-V"])
def test_version_alone_prints_the_version(env, flag):
    env = env | {"FAKE_UID": "1000"}
    proc = run(env, flag)
    assert proc.returncode == 0, proc.stderr
    assert "ARGS: --version" in proc.stdout
    assert not (Path(env["MONITOR_PROJECT_DIR"]) / "config.py").exists()


@posix_only
def test_version_with_other_options_still_goes_to_runserver(env):
    env = env | {"FAKE_UID": "1000"}
    proc = run(env, "--port", "8000")
    assert "ARGS: runserver --import-csv-if-empty --port 8000" in proc.stdout


@posix_only
@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_help_does_not_write_the_demo_into_the_folder(env, flag):
    env = env | {"FAKE_UID": "1000"}
    proc = run(env, flag)
    assert proc.returncode == 0, proc.stderr
    assert f"ARGS: runserver --import-csv-if-empty {flag}" in proc.stdout
    assert not (Path(env["MONITOR_PROJECT_DIR"]) / "config.py").exists()
    assert "デモプロジェクト" not in proc.stderr


# ---------------------------------------------------------------------------
# HEALTHCHECK
# ---------------------------------------------------------------------------
@pytest.fixture
def health_server():
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


@pytest.mark.parametrize("variable", ["HTTP_PROXY", "http_proxy", "ALL_PROXY"])
def test_healthcheck_ignores_proxy_settings(health_server, variable):
    environ = {
        "PATH": os.environ.get("PATH", ""),
        "MONITOR_PORT": str(health_server),
        variable: "http://127.0.0.1:9",  # 何も待ち受けていないプロキシ
    }
    if os.name == "nt":
        environ["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", "")
    proc = subprocess.run(
        [sys.executable, "-I", str(HEALTHCHECK)],
        env=environ,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr


def _load_healthcheck():
    spec = importlib.util.spec_from_file_location(
        "monitor_healthcheck_hard", HEALTHCHECK
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_healthcheck_drops_root_to_nobody(monkeypatch):
    module = _load_healthcheck()
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(module.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(
        module.os, "setgroups", lambda g: calls.append(("groups", g)), raising=False
    )
    monkeypatch.setattr(
        module.os, "setgid", lambda g: calls.append(("gid", g)), raising=False
    )
    monkeypatch.setattr(
        module.os, "setuid", lambda u: calls.append(("uid", u)), raising=False
    )
    module.drop_privileges()
    assert calls == [("groups", []), ("gid", 65534), ("uid", 65534)]


def test_healthcheck_keeps_going_when_it_cannot_switch(monkeypatch):
    module = _load_healthcheck()

    def deny(*_args):
        raise PermissionError("no CAP_SETUID")

    monkeypatch.setattr(module.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(module.os, "setgroups", deny, raising=False)
    module.drop_privileges()  # 例外にしない


def test_healthcheck_does_nothing_for_other_users(monkeypatch):
    module = _load_healthcheck()
    monkeypatch.setattr(module.os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(
        module.os, "setuid", lambda _u: pytest.fail("切り替えない"), raising=False
    )
    module.drop_privileges()


# ---------------------------------------------------------------------------
# イメージ・Compose の設定(リポジトリだけにあるファイル。sdist からの実行では飛ばす)
# ---------------------------------------------------------------------------
def _repo_file(relative: str) -> Path:
    path = ROOT / relative
    if not path.is_file():
        pytest.skip(f"{relative} が無い(sdist などリポジトリの外で実行している)")
    return path


def test_dockerfile_fixes_modes_strips_setuid_and_isolates_the_healthcheck():
    text = _repo_file("Dockerfile").read_text(encoding="utf-8")
    assert "chmod 0755 /usr/local/bin/monitor-app-entrypoint" in text
    assert "chmod 0644 /opt/monitor-app/healthcheck.py" in text
    assert "find / -xdev -perm /6000 -type f -exec chmod a-s {} +" in text
    assert 'CMD ["python", "-I", "/opt/monitor-app/healthcheck.py"]' in text
    # デモの .env(生成した API キー)をイメージに焼き込まない
    assert "rm -f /opt/monitor-app/demo/.env" in text


@pytest.mark.parametrize(
    "compose", ["compose.yaml", "monitor_app/scaffold/deploy/compose.yaml"]
)
def test_compose_files_drop_capabilities(compose):
    text = _repo_file(compose).read_text(encoding="utf-8")
    service = yaml.safe_load(text)["services"]["monitor-app"]
    assert service["cap_drop"] == ["ALL"]
    assert set(service["cap_add"]) == {"SETUID", "SETGID", "CHOWN"}
    assert service["security_opt"] == ["no-new-privileges:true"]
    assert service["healthcheck"]["test"][:3] == ["CMD", "python", "-I"]
