#!/usr/bin/env python3
"""push 前のローカル確認を 1 コマンドで走らせ、結果を 1 つの表にまとめる。

GitHub の CI は Windows・macOS・Python 3.11〜3.14・最低バージョンの依存・PostgreSQL /
MySQL・Docker・PyPI への公開を確かめるが、ブラウザ E2E(tests/e2e/test_browser_ui.py)
は Chrome が無いため常にスキップされる。このスクリプトは CI と同じ速い検査に加えて、
ブラウザ E2E とデモ画面のスクリーンショットを手元で走らせる。

使い方(--quick・--only・--setup-browser はどれか 1 つだけ):
  uv run python scripts/verify.py                    # 既定のステップ(下の表の *)
  uv run python scripts/verify.py --quick            # Chrome の要るステップを除く
  uv run python scripts/verify.py --only lint,tests  # 指定したステップだけ
  uv run python scripts/verify.py --shots DIR        # shots を足し、画像を DIR に上書き保存
  uv run python scripts/verify.py --setup-browser    # Chrome をキャッシュに入れて終わる
  uv run python scripts/verify.py -h                 # この説明とステップの表

Chrome は MONITOR_TEST_CHROME → PATH 上の Chrome / Chromium / chrome-headless-shell →
キャッシュ($XDG_CACHE_HOME/monitor-app/verify。既定は ~/.cache の下)の順に探す。
--setup-browser はキャッシュに chrome-headless-shell と、Linux では足りないライブラリと
日本語フォントを入れる。
選んだステップが 1 つでも失敗すると終了コード 1(Ctrl+C で止めたら 130)を返す。
Chrome が無いなどで走らせられないステップもスキップではなく失敗にする(確かめて
いないものを成功に見せないため)。
"""

import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BROWSER_TESTS = "tests/e2e/test_browser_ui.py"
PYTEST = ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]
#: --setup-browser が使う @puppeteer/browsers の版(2026-10-28 に動作を確認)。
PUPPETEER_BROWSERS = "@puppeteer/browsers@2.10.5"
#: PATH から探す Chrome の実行ファイル名(先に見つかったものを使う)。
CHROME_NAMES = (
    "chrome-headless-shell",
    "chromium",
    "chromium-browser",
    "google-chrome",
    "google-chrome-stable",
    "chrome",
)
#: ldd が見つけられないライブラリ → それを含む Debian / Ubuntu のパッケージ(先に
#: 取れた方を使う。Ubuntu 24.04 以降は t64 付きの名前)。表に無いものは手で入れてもらう。
LIB_PACKAGES = {"libasound.so.2": ("libasound2t64", "libasound2")}
FONT_PACKAGE = "fonts-ipafont-gothic"
FONTS_CONF = """<?xml version="1.0"?>
<!DOCTYPE fontconfig SYSTEM "fonts.dtd">
<fontconfig>
  <include ignore_missing="yes">/etc/fonts/fonts.conf</include>
  <dir>{fonts}</dir>
  <cachedir>{cachedir}</cachedir>
</fontconfig>
"""
DESKTOP = (1440, 900)
MOBILE = (390, 844)
#: 撮る組み合わせ: テーマ, 画面の大きさ, モバイルとして描くか, ファイル名({} に画面名)。
VIEWS = [
    ("light", DESKTOP, False, "{}-light.png"),
    ("dark", DESKTOP, False, "{}-dark.png"),
    ("light", MOBILE, True, "mobile-{}.png"),
]
#: ページを開いてから、データの描画を待つ秒数。
SETTLE_SECONDS = 1.5
#: デモの全画面のパス。サイドバー(ビュー・大型表示・取り込み・設定)と入力フォームへのリンク。
PAGES_JS = """[...new Set(["/", ...[...document.querySelectorAll(
    ".sidebar-nav a, a[href*='/form/']")].map(a => a.pathname)])]"""


class StepFailed(Exception):
    """ステップの失敗。メッセージは結果の表に出す 1 行の理由。"""


def read_lines(path: str) -> list[str]:
    with open(path, encoding="utf-8") as f:
        return f.read().splitlines()


def cache_dir() -> str:
    home_cache = os.path.join(os.path.expanduser("~"), ".cache")
    base = os.environ.get("XDG_CACHE_HOME") or home_cache
    return os.path.join(base, "monitor-app", "verify")


def cached_chrome(cache: str) -> str | None:
    """キャッシュにある chrome-headless-shell のうち、一番新しい版の実行ファイル。"""
    found = []
    for folder, _, files in os.walk(os.path.join(cache, "chrome-headless-shell")):
        for name in ("chrome-headless-shell", "chrome-headless-shell.exe"):
            if name in files:
                # 版のフォルダは chrome-headless-shell/linux-157.0.8103.38/<OS 名>/ の形
                version = os.path.basename(os.path.dirname(folder))
                numbers = version.rpartition("-")[2].split(".")
                key = [int(n) for n in numbers if n.isdigit()]
                found.append((key, os.path.join(folder, name)))
    return max(found)[1] if found else None


def chrome_env(cache: str) -> dict[str, str]:
    """os.environ に、キャッシュに入れたライブラリ・フォントの場所を足したもの。"""
    env = dict(os.environ)
    lib_root = os.path.join(cache, "libs", "usr", "lib")
    names = os.listdir(lib_root) if os.path.isdir(lib_root) else []
    libs = [os.path.join(lib_root, n) for n in names if "-linux-gnu" in n]
    old = env.get("LD_LIBRARY_PATH")
    if libs:
        env["LD_LIBRARY_PATH"] = os.pathsep.join([*libs, old] if old else libs)
    fonts_conf = os.path.join(cache, "fonts.conf")
    if os.path.isfile(fonts_conf) and "FONTCONFIG_FILE" not in env:
        env["FONTCONFIG_FILE"] = fonts_conf
    return env


def chrome_version(path: str, env: dict[str, str]) -> str:
    try:
        done = subprocess.run(
            [path, "--version"], env=env, capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StepFailed(f"Chrome を起動できません({path}): {exc}") from exc
    if done.returncode != 0:
        reason = (done.stderr.strip().splitlines() or ["(出力なし)"])[0]
        raise StepFailed(
            f"Chrome が起動しません({path}): {reason}。"
            "足りないライブラリは --setup-browser で入ります"
        )
    return done.stdout.strip()


def find_chrome() -> tuple[str, dict[str, str], str]:
    """MONITOR_TEST_CHROME → PATH → キャッシュの順に Chrome を探し、起動できるか確かめる。

    (実行ファイル, Chrome とブラウザ E2E に渡す環境変数, 表に出す「出どころ, 版」)を返す。
    環境変数には MONITOR_TEST_CHROME と MONITOR_TEST_CHROME_ARGS も入れる。
    """
    cache = cache_dir()
    candidates = [(os.environ.get("MONITOR_TEST_CHROME"), "MONITOR_TEST_CHROME")]
    candidates += [(shutil.which(name), "PATH") for name in CHROME_NAMES]
    candidates.append((cached_chrome(cache), "キャッシュ"))
    found = [(path, origin) for path, origin in candidates if path]
    if not found:
        raise StepFailed(
            "Chrome が見つかりません。`uv run python scripts/verify.py --setup-browser`"
            " で入れるか、MONITOR_TEST_CHROME に実行ファイルを指定してください"
            "(Chrome の要らない確認だけなら --quick)"
        )
    path, origin = found[0]
    env = chrome_env(cache)
    version = chrome_version(path, env)
    env["MONITOR_TEST_CHROME"] = path
    # Ubuntu 23.10 以降の AppArmor やコンテナでは、Chrome のサンドボックスが要る名前空間を
    # 作れずに起動しない。開くのは手元のテスト用サーバーだけなので外す
    env.setdefault(
        "MONITOR_TEST_CHROME_ARGS", "--no-sandbox" if sys.platform == "linux" else ""
    )
    return path, env, f"{origin}, {version}"


def apt_extract(packages: tuple[str, ...], dest: str) -> bool:
    """packages のうち最初に取れた .deb を dest に展開する(apt-get download は sudo 不要)。"""
    with tempfile.TemporaryDirectory(prefix="monitor-app-verify-deb-") as tmp:
        for package in packages:
            got = subprocess.run(["apt-get", "download", package], cwd=tmp)
            debs = [name for name in os.listdir(tmp) if name.endswith(".deb")]
            if got.returncode == 0 and debs:
                deb = os.path.join(tmp, debs[0])
                subprocess.run(["dpkg", "-x", deb, dest], check=True)
                return True
    return False


def missing_libs(chrome: str, env: dict[str, str]) -> list[str]:
    out = subprocess.run(
        ["ldd", chrome], env=env, capture_output=True, text=True
    ).stdout
    return [line.split()[0] for line in out.splitlines() if "not found" in line]


def install_libs(cache: str, chrome: str) -> bool:
    missing = missing_libs(chrome, chrome_env(cache))
    if missing:
        print(f"足りないライブラリ: {', '.join(missing)}")
    for lib in missing:
        packages = LIB_PACKAGES.get(lib, ())
        if packages and shutil.which("apt-get"):
            apt_extract(packages, os.path.join(cache, "libs"))
    still = missing_libs(chrome, chrome_env(cache))
    if still:
        print(
            f"自動では入れられないライブラリがあります: {', '.join(still)}。"
            "OS のパッケージマネージャで入れてから、もう一度実行してください",
            file=sys.stderr,
        )
    return not still


def install_font(cache: str) -> None:
    if shutil.which("fc-list"):
        listed = subprocess.run(
            ["fc-list", ":lang=ja", "family"],
            env=chrome_env(cache),
            capture_output=True,
            text=True,
        )
        if listed.stdout.strip():
            return
    print(f"日本語フォントが無いため {FONT_PACKAGE} を入れます")
    fonts = os.path.join(cache, "fonts")
    if not (shutil.which("apt-get") and apt_extract((FONT_PACKAGE,), fonts)):
        print(
            "日本語フォントを入れられませんでした。スクリーンショットの日本語が"
            "□ になります(ブラウザ E2E には影響しません)",
            file=sys.stderr,
        )
        return
    conf = FONTS_CONF.format(
        fonts=os.path.join(fonts, "usr", "share", "fonts"),
        cachedir=os.path.join(cache, "fontcache"),
    )
    with open(os.path.join(cache, "fonts.conf"), "w", encoding="utf-8") as f:
        f.write(conf)


def setup_browser() -> int:
    """chrome-headless-shell と、Linux で足りないライブラリ・日本語フォントをキャッシュに入れる。

    何度実行しても同じ状態に収束する(足りないものだけを入れる)。
    """
    cache = cache_dir()
    print(f"Chrome をキャッシュに入れます: {cache}")
    if not shutil.which("npx"):
        print("npx が見つかりません。Node.js 18 以降を入れてください", file=sys.stderr)
        return 1
    os.makedirs(cache, exist_ok=True)
    install = ["npx", "--yes", PUPPETEER_BROWSERS, "install"]
    install += ["chrome-headless-shell@stable", "--path", cache]
    if subprocess.run(install).returncode != 0:
        print("chrome-headless-shell を入れられませんでした", file=sys.stderr)
        return 1
    chrome = cached_chrome(cache)
    if chrome is None:
        print(f"{cache} に chrome-headless-shell が見つかりません", file=sys.stderr)
        return 1
    if sys.platform == "linux":
        if not install_libs(cache, chrome):
            return 1
        install_font(cache)
    try:
        version = chrome_version(chrome, chrome_env(cache))
        path, _, origin = find_chrome()
    except StepFailed as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"入れた Chrome: {version} ({chrome})")
    print(f"verify が使う Chrome: {path} ({origin})")
    return 0


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def demo_ready(port: int) -> bool:
    """デモの /api/health が 200 を返すか。urllib は http_proxy を見てしまうので直接繋ぐ。"""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
            sock.sendall(b"GET /api/health HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
            return b" 200 " in sock.recv(64)
    except OSError:
        return False


def devtools_address(profile: str) -> tuple[int, str] | None:
    """Chrome が DevToolsActivePort に書いた (ポート, ブラウザの WebSocket のパス)。"""
    try:
        lines = read_lines(os.path.join(profile, "DevToolsActivePort"))
    except OSError:
        return None
    return (int(lines[0]), lines[1]) if len(lines) >= 2 else None


def wait_for(ready, arg, proc: subprocess.Popen, what: str):
    """ready(arg) が真を返すまで最大 60 秒待ち、その値を返す。"""
    deadline = time.monotonic() + 60
    while not (value := ready(arg)):
        if proc.poll() is not None or time.monotonic() > deadline:
            raise StepFailed(f"{what} が起動しません(ログを参照)")
        time.sleep(0.2)
    return value


def stop(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def recv_exact(sock: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ConnectionError("Chrome が DevTools の接続を閉じました")
        data += chunk
    return bytes(data)


def cdp_connect(port: int, path: str) -> socket.socket:
    """Chrome の DevTools に WebSocket(RFC 6455)で繋ぐ。

    このスクリプトは標準ライブラリだけで動かすため、スクリーンショットに要る分だけを
    cdp() で自前で話す。ブラウザ E2E(tests/e2e/test_browser_ui.py の WebSocket)は
    イベントも受け取り、待ち時間を区切るため、別の小さな実装を持つ。
    """
    key = base64.b64encode(os.urandom(16)).decode()
    sock = socket.create_connection(("127.0.0.1", port), timeout=30)
    sock.sendall(
        f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
        "Upgrade: websocket\r\nConnection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n".encode()
    )
    head = b""
    while not head.endswith(b"\r\n\r\n"):
        head += recv_exact(sock, 1)
    if not head.startswith(b"HTTP/1.1 101"):
        sock.close()
        raise ConnectionError(f"DevTools に接続できません: {head.splitlines()[0]!r}")
    return sock


def cdp(sock: socket.socket, method: str, **params) -> dict:
    """DevTools のコマンドを送り、返事の result を返す。

    コマンドは 1 つずつ送って返事を待つので、id はいつも 1 でよい。Chrome はメッセージを
    分割せずに 1 フレームで送るので、続きのフレームは扱わない。
    """
    data = json.dumps({"id": 1, "method": method, "params": params}).encode()
    size = len(data)
    # 送るコマンドは 64KiB より短いので、長さは 7 ビットか 16 ビットに収まる
    if size < 126:
        frame = bytes([0x81, 0x80 | size])
    else:
        frame = bytes([0x81, 0x80 | 126]) + size.to_bytes(2, "big")
    mask = os.urandom(4)
    sock.sendall(frame + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))
    while True:
        head = recv_exact(sock, 2)
        size = head[1] & 0x7F
        if size == 126:
            size = int.from_bytes(recv_exact(sock, 2), "big")
        elif size == 127:
            size = int.from_bytes(recv_exact(sock, 8), "big")
        payload = recv_exact(sock, size)
        if head[0] & 0x0F == 0x8:
            raise ConnectionError("Chrome が DevTools の接続を閉じました")
        reply = json.loads(payload)
        if reply.get("id") == 1:
            if "error" in reply:
                raise RuntimeError(f"{method}: {reply['error']}")
            return reply.get("result", {})


def js(sock: socket.socket, expression: str):
    result = cdp(sock, "Runtime.evaluate", expression=expression, returnByValue=True)
    if "exceptionDetails" in result:
        raise RuntimeError(f"JS の評価に失敗: {result['exceptionDetails']}")
    return result["result"].get("value")


def goto(sock: socket.socket, url: str) -> None:
    navigated = cdp(sock, "Page.navigate", url=url)
    if navigated.get("errorText"):
        raise RuntimeError(f"{url} を開けません: {navigated['errorText']}")
    deadline = time.monotonic() + 15
    while js(sock, "document.readyState") != "complete":
        if time.monotonic() > deadline:
            raise TimeoutError(f"{url} の読み込みが 15 秒で終わりません")
        time.sleep(0.1)


def shoot_pages(sock: socket.socket, base: str, out: str, log) -> tuple[int, list]:
    """デモの全画面を VIEWS の組み合わせで撮る。(枚数, モバイル幅ではみ出す画面)を返す。"""
    goto(sock, base + "/")
    paths = js(sock, PAGES_JS)
    log.write(f"画面: {', '.join(paths)}\n")
    saved, too_wide = 0, []
    for theme, (width, height), mobile, name in VIEWS:
        cdp(
            sock,
            "Emulation.setDeviceMetricsOverride",
            width=width,
            height=height,
            deviceScaleFactor=1,
            mobile=mobile,
        )
        js(sock, f'localStorage.setItem("monitor-theme", "{theme}")')
        for path in paths:
            goto(sock, base + path)
            # キオスクのテーマは localStorage ではなく config で決まるため、属性で揃える
            js(sock, f'document.documentElement.setAttribute("data-theme", "{theme}")')
            time.sleep(SETTLE_SECONDS)
            slug = path.strip("/").replace("/", "_") or "index"
            file = os.path.join(out, name.format(slug))
            png = cdp(sock, "Page.captureScreenshot", format="png")["data"]
            with open(file, "wb") as f:
                f.write(base64.b64decode(png))
            log.write(file + "\n")
            saved += 1
            overflow = "document.documentElement.scrollWidth > window.innerWidth"
            if mobile and js(sock, overflow):
                too_wide.append(path)
    return saved, too_wide


def take_screenshots(log, out: str) -> str:
    """monitor-app demo とヘッドレス Chrome を起動し、デモの全画面を out に撮る。"""
    chrome, env, _ = find_chrome()
    os.makedirs(out, exist_ok=True)
    demo_port = free_port()
    demo_env = {k: v for k, v in os.environ.items() if not k.startswith("MONITOR_")}
    demo = [sys.executable, "-m", "monitor_app", "demo", "--port", str(demo_port)]
    procs = []
    with tempfile.TemporaryDirectory(
        prefix="monitor-app-verify-chrome-", ignore_cleanup_errors=True
    ) as profile:
        browser = [
            chrome,
            "--headless",
            "--remote-debugging-port=0",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            *env["MONITOR_TEST_CHROME_ARGS"].split(),
            "about:blank",
        ]
        try:
            procs.append(
                subprocess.Popen(
                    demo, cwd=REPO, env=demo_env, stdout=log, stderr=subprocess.STDOUT
                )
            )
            wait_for(demo_ready, demo_port, procs[-1], "monitor-app demo")
            procs.append(
                subprocess.Popen(browser, env=env, stdout=log, stderr=subprocess.STDOUT)
            )
            port, path = wait_for(devtools_address, profile, procs[-1], "Chrome")
            with cdp_connect(port, path) as sock:
                tab = cdp(sock, "Target.createTarget", url="about:blank")["targetId"]
            with cdp_connect(port, f"/devtools/page/{tab}") as sock:
                base = f"http://127.0.0.1:{demo_port}"
                saved, too_wide = shoot_pages(sock, base, out, log)
        finally:
            for proc in procs:
                stop(proc)
    if too_wide:
        raise StepFailed(
            f"モバイル幅({MOBILE[0]}px)で横にはみ出す画面: {', '.join(too_wide)}"
            f"(画像は {out})"
        )
    return f"{saved} 枚を {out} に保存"


def run_command(argv: list[str], log, env: dict[str, str] | None = None) -> str:
    """コマンドを REPO で実行して出力をログに書き、最後の行を返す。失敗なら StepFailed。"""
    log.write("$ " + " ".join(argv) + "\n")
    log.flush()
    code = subprocess.run(
        argv, cwd=REPO, env=env, stdout=log, stderr=subprocess.STDOUT
    ).returncode
    lines = [line.strip("= ") for line in read_lines(log.name)]
    lines = [line for line in lines if line]
    last = lines[-1] if lines else ""
    if code != 0:
        raise StepFailed(f"終了コード {code}: {last}")
    return last


def run_browser_tests(log, _shots: str | None) -> str:
    _, env, origin = find_chrome()
    last = run_command(["uv", "run", *PYTEST, "-rs", BROWSER_TESTS], log, env)
    # pytest はテストをスキップしても終了コード 0 を返すため、要約の行で確かめる
    if " passed" not in last or " skipped" in last:
        raise StepFailed(f"走っていないテストがあります(理由はログ): {last}")
    return f"{last} (Chrome: {origin})"


#: ステップの表。cmd を持つステップはそのコマンドを、run を持つステップは
#: run(ログ, --shots の保存先) を実行する。default が False のものは --only か --shots で
#: 選んだときだけ、chrome が True のものは --quick では走らない。
STEPS = [
    {
        "name": "lock",
        "summary": "uv.lock が pyproject.toml と合っているか",
        "cmd": ["uv", "lock", "--check"],
    },
    {
        "name": "format",
        "summary": "black --check",
        "cmd": ["uv", "run", "black", "--check", "."],
    },
    {
        "name": "lint",
        "summary": "ruff check",
        "cmd": ["uv", "run", "ruff", "check", "."],
    },
    {
        "name": "tests",
        "summary": "pytest(全 extras、ブラウザ E2E 以外。CI と同じ)",
        "cmd": ["uv", "run", "--all-extras", *PYTEST, f"--ignore={BROWSER_TESTS}"],
    },
    {
        "name": "browser",
        "summary": f"ブラウザ E2E({BROWSER_TESTS}。CI では走らない)",
        "run": run_browser_tests,
        "chrome": True,
    },
    {
        "name": "shots",
        "summary": "デモ画面のスクリーンショット(--shots DIR で保存先を指定)",
        "run": take_screenshots,
        "chrome": True,
        "default": False,
    },
]


def run_step(step: dict, log_path: str, shots: str | None) -> tuple[str, str]:
    """ステップを 1 つ走らせ、("PASS" か "FAIL", 表に出す 1 行)を返す。"""
    with open(log_path, "w", encoding="utf-8") as log:
        try:
            if "cmd" in step:
                return "PASS", run_command(step["cmd"], log)
            return "PASS", step["run"](log, shots)
        except StepFailed as exc:
            return "FAIL", str(exc)
        except Exception as exc:  # noqa: BLE001 - 失敗として表に残し、残りを続ける
            log.write(f"\n{exc!r}\n")
            return "FAIL", f"{type(exc).__name__}: {exc}"


def run_steps(steps: list[dict], shots: str | None) -> int:
    log_dir = tempfile.mkdtemp(prefix="monitor-app-verify-")
    #: (名前, PASS / FAIL / SKIP, 秒, 表に出す 1 行, ログのパスか None)
    results = []
    interrupted = False
    for index, step in enumerate(steps, 1):
        name = step["name"]
        if interrupted:
            results.append((name, "SKIP", 0.0, "中断したため未実行", None))
            continue
        log_path = os.path.join(log_dir, f"{name}.log")
        print(f"[{index}/{len(steps)}] {name} ...", end=" ", flush=True)
        started = time.monotonic()
        try:
            status, note = run_step(step, log_path, shots)
        except KeyboardInterrupt:
            interrupted = True
            status, note = "FAIL", "中断"
        seconds = time.monotonic() - started
        results.append((name, status, seconds, note, log_path))
        print(f"{status} {seconds:.1f}s")

    width = max(len(name) for name, *_ in results)
    print("\n結果:")
    for name, status, seconds, note, _ in results:
        print(f"  {status}  {name:{width}}  {seconds:6.1f}s  {note}")
    print(f"ログ: {log_dir}")
    for name, status, _, _, log_path in results:
        if status == "FAIL" and log_path and os.path.exists(log_path):
            tail = read_lines(log_path)[-20:]
            if tail:
                print(f"\n--- {name} のログの末尾({log_path}) ---")
                print("\n".join(tail))

    if interrupted:
        return 130
    return 1 if any(status != "PASS" for _, status, *_ in results) else 0


def usage_error(message: str):
    print(f"エラー: {message}(使い方は -h)", file=sys.stderr)
    sys.exit(2)


def print_help() -> None:
    print(__doc__)
    print("ステップ(* は既定で走る):")
    for step in STEPS:
        mark = "*" if step.get("default", True) else " "
        print(f"  {mark} {step['name']:8} {step['summary']}")


def parse_args(argv: list[str]) -> tuple[bool, list[str] | None, str | None, bool]:
    """コマンドラインを (--quick, --only のステップ名, --shots, --setup-browser) にする。"""
    quick = setup = False
    only = shots = None
    rest = list(argv)
    while rest:
        arg = rest.pop(0)
        option, has_value, value = arg.partition("=")
        if option in ("--only", "--shots") and not has_value:
            if not rest:
                usage_error(f"{option} の後に値を指定してください")
            value = rest.pop(0)
        if arg in ("-h", "--help"):
            print_help()
            sys.exit(0)
        elif arg == "--quick":
            quick = True
        elif arg == "--setup-browser":
            setup = True
        elif option == "--only":
            known = [step["name"] for step in STEPS]
            only = [name.strip() for name in value.split(",") if name.strip()]
            if not only or any(name not in known for name in only):
                usage_error(f"ステップは {', '.join(known)} から選んでください")
        elif option == "--shots":
            shots = os.path.abspath(value)
        else:
            usage_error(f"知らないオプションです: {arg}")
    if quick + setup + (only is not None) > 1:
        usage_error("--quick・--only・--setup-browser はどれか 1 つだけ指定できます")
    if only and "shots" in only and shots is None:
        usage_error("shots には --shots DIR で保存先を指定してください")
    return quick, only, shots, setup


def main(argv: list[str]) -> int:
    # 出力をパイプやファイルに流しても、子プロセスの出力と順番が入れ替わらないように
    sys.stdout.reconfigure(line_buffering=True)
    quick, only, shots, setup = parse_args(argv)
    if setup:
        return setup_browser()
    names = only or [
        step["name"]
        for step in STEPS
        if step.get("default", True) and not (quick and step.get("chrome"))
    ]
    if shots:
        names = [*names, "shots"]
    return run_steps([step for step in STEPS if step["name"] in names], shots)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
