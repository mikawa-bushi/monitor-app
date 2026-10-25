"""同梱 UI のブラウザ E2E(任意): 実サーバ + ヘッドレス Chrome を DevTools プロトコルで操作する。

既定ではスキップする。``MONITOR_TEST_CHROME`` に Chrome / Chromium /
chrome-headless-shell の実行ファイルを指定すると走る。追加の起動引数は
``MONITOR_TEST_CHROME_ARGS``(例: コンテナや Ubuntu 24.04 以降では ``--no-sandbox``)::

    MONITOR_TEST_CHROME=/path/to/chrome uv run python -m pytest tests/e2e/test_browser_ui.py

サーバはサブパス配信(``MONITOR_ROOT_PATH=/monitor``)で起動し、プレフィックスを外さない
プロキシと同じ ``/monitor/...`` でアクセスする。JS の URL 組み立て(MonitorCore.url)も
同時に確かめるため。ルート配信の HTML は tests/test_web_serving.py が確かめている。
"""

from __future__ import annotations

import http.client
import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from contract import helpers

pytestmark = pytest.mark.e2e

CHROME = os.environ.get("MONITOR_TEST_CHROME")
CONTRAST_JS = (Path(__file__).parent / "browser_contrast.js").read_text(
    encoding="utf-8"
)
ROOT = "/monitor"

if not CHROME:
    pytest.skip(
        "MONITOR_TEST_CHROME(Chrome の実行ファイル)が未設定", allow_module_level=True
    )
sync_client = pytest.importorskip("websockets.sync.client")


# ---------------------------------------------------------------------------
# 最小限の DevTools プロトコル クライアント
# ---------------------------------------------------------------------------
class Cdp:
    def __init__(self, ws) -> None:
        self.ws = ws
        self._id = 0
        self.events: list[dict] = []

    def send(self, method: str, params: dict | None = None, session=None) -> dict:
        self._id += 1
        message = {"id": self._id, "method": method, "params": params or {}}
        if session:
            message["sessionId"] = session
        self.ws.send(json.dumps(message))
        deadline = time.monotonic() + 30
        while True:
            data = json.loads(
                self.ws.recv(timeout=max(0.1, deadline - time.monotonic()))
            )
            if data.get("id") == self._id:
                if "error" in data:
                    raise RuntimeError(f"{method}: {data['error']}")
                return data.get("result", {})
            self.events.append(data)

    def pump(self, seconds: float) -> None:
        """イベントを受け取りながら待つ。"""
        deadline = time.monotonic() + seconds
        while (left := deadline - time.monotonic()) > 0:
            try:
                self.events.append(json.loads(self.ws.recv(timeout=left)))
            except TimeoutError:
                break


class Tab:
    def __init__(self, cdp: Cdp, context: str) -> None:
        self.cdp = cdp
        self.target = cdp.send(
            "Target.createTarget", {"url": "about:blank", "browserContextId": context}
        )["targetId"]
        self.session = cdp.send(
            "Target.attachToTarget", {"targetId": self.target, "flatten": True}
        )["sessionId"]
        for domain in ("Page", "Runtime", "Network"):
            self.send(f"{domain}.enable")

    def send(self, method: str, params: dict | None = None) -> dict:
        return self.cdp.send(method, params, self.session)

    def eval(self, expression: str):
        result = self.send(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
        )
        if "exceptionDetails" in result:
            raise AssertionError(f"JS の評価に失敗: {result['exceptionDetails']}")
        return result["result"].get("value")

    def goto(self, url: str) -> None:
        self.send("Page.navigate", {"url": url})
        self.wait_for("document.readyState === 'complete'")

    def wait_for(self, expression: str, timeout: float = 8.0):
        deadline = time.monotonic() + timeout
        while True:
            value = self.eval(expression)
            if value:
                return value
            if time.monotonic() > deadline:
                raise AssertionError(
                    f"{timeout} 秒待っても成り立ちません: {expression}"
                )
            self.cdp.pump(0.1)

    def network(self, method: str) -> list[dict]:
        return [
            e["params"]
            for e in self.cdp.events
            if e.get("sessionId") == self.session and e.get("method") == method
        ]

    def errors(self) -> list[str]:
        return [
            json.dumps(e["params"])[:300]
            for e in self.cdp.events
            if e.get("sessionId") == self.session
            and (
                e.get("method") == "Runtime.exceptionThrown"
                or (
                    e.get("method") == "Runtime.consoleAPICalled"
                    and e["params"]["type"] == "error"
                )
            )
        ]

    def set_hidden(self, hidden: bool) -> None:
        """タブの表示状態の変化を再現する(ヘッドレスでは全タブが visible のため)。"""
        state = "hidden" if hidden else "visible"
        self.eval(f"""(() => {{
              Object.defineProperty(document, "hidden",
                {{ configurable: true, get: () => {str(hidden).lower()} }});
              Object.defineProperty(document, "visibilityState",
                {{ configurable: true, get: () => "{state}" }});
              document.dispatchEvent(new Event("visibilitychange")); return 1; }})()""")

    def close(self) -> None:
        self.cdp.send("Target.closeTarget", {"targetId": self.target})


# ---------------------------------------------------------------------------
# フィクスチャ
# ---------------------------------------------------------------------------
def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _request(port: int, method: str, path: str, body=None) -> tuple[int, bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        payload = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if payload else {}
        conn.request(method, path, body=payload, headers=headers)
        res = conn.getresponse()
        return res.status, res.read()
    finally:
        conn.close()


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("browser-ui")
    env = helpers.child_env()
    env["MONITOR_ROOT_PATH"] = ROOT
    created = helpers.run_cli("startproject", "proj", cwd=tmp, env=env)
    assert created.returncode == 0, created.stderr.decode("utf-8", "replace")
    project = tmp / "proj"
    config = project / "config.py"
    text = config.read_text(encoding="utf-8")
    # critical のアラートが出ているとキオスクはローテーションを止めるため warning にする
    text = text.replace('level="critical",', 'level="warning",')
    text = text.replace("KioskConfig(rotate_seconds=15", "KioskConfig(rotate_seconds=2")
    config.write_text(text, encoding="utf-8")
    port = _free_port()
    log_path = tmp / "server.log"
    with log_path.open("wb") as log:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "monitor_app",
                "runserver",
                "--import-csv",
                "--port",
                str(port),
            ],
            cwd=project,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                try:
                    if _request(port, "GET", ROOT + "/api/health")[0] == 200:
                        break
                except OSError:
                    time.sleep(0.1)
            else:
                pytest.fail(log_path.read_text(encoding="utf-8", errors="replace"))
            yield port
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


@pytest.fixture(scope="module")
def browser(tmp_path_factory):
    profile = tmp_path_factory.mktemp("chrome-profile")
    args = [
        CHROME,
        "--headless",
        "--remote-debugging-port=0",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-gpu",
        "--disable-dev-shm-usage",
        *shlex.split(os.environ.get("MONITOR_TEST_CHROME_ARGS", "")),
        "about:blank",
    ]
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        port_file = profile / "DevToolsActivePort"
        deadline = time.monotonic() + 20
        while not port_file.exists() or len(port_file.read_text().split()) < 2:
            if proc.poll() is not None or time.monotonic() > deadline:
                pytest.fail("Chrome が起動しません(MONITOR_TEST_CHROME_ARGS を確認)")
            time.sleep(0.1)
        port, path = port_file.read_text().split()[:2]
        url = f"ws://127.0.0.1:{port}{path}"
        with sync_client.connect(url, max_size=None, open_timeout=15) as ws:
            yield Cdp(ws)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(profile, ignore_errors=True)


@pytest.fixture
def tab(browser):
    context = browser.send("Target.createBrowserContext")["browserContextId"]
    page = Tab(browser, context)
    yield page
    page.close()
    browser.send("Target.disposeBrowserContext", {"browserContextId": context})


def _url(port: int, path: str) -> str:
    return f"http://127.0.0.1:{port}{ROOT}{path}"


def _rows(tab: Tab) -> int:
    return tab.eval('document.querySelectorAll("#tbody tr").length')


# ---------------------------------------------------------------------------
# テスト
# ---------------------------------------------------------------------------
PAGES = [
    "/",
    "/table/products_view",
    "/table/orders_summary",
    "/form/orders",
    "/kiosk",
    "/login",
]


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("page", PAGES)
def test_text_is_readable_in_both_themes(server, tab, page, theme):
    tab.goto(_url(server, page))
    if page == "/kiosk":  # キオスクのテーマは config で決まる(両方を確かめる)
        tab.eval(f'document.documentElement.setAttribute("data-theme", "{theme}")')
    else:
        tab.eval(f'localStorage.setItem("monitor-theme", "{theme}")')
        tab.goto(_url(server, page))
    tab.cdp.pump(1.5)  # データの描画を待つ
    tab.eval('document.querySelectorAll("details").forEach(d => d.open = true); 1')
    tab.cdp.pump(0.3)
    result = tab.eval(CONTRAST_JS)
    assert result["theme"] == theme
    assert result["checked"] > 0
    assert result["failing"] == [], result["failing"]
    assert tab.errors() == []


def test_assets_and_api_calls_stay_under_the_root_path(server, tab):
    for page in ("/", "/table/products_view", "/kiosk", "/form/orders"):
        tab.goto(_url(server, page))
        tab.cdp.pump(1.0)
    urls = [r["request"]["url"] for r in tab.network("Network.requestWillBeSent")]
    statuses = {
        r["response"]["url"]: r["response"]["status"]
        for r in tab.network("Network.responseReceived")
    }
    assert urls
    outside = [u for u in urls if not u.startswith(_url(server, "/"))]
    assert outside == []
    assert [u for u, s in statuses.items() if s >= 400] == []
    assets = [u for u in urls if "/static/" in u]
    assert assets and all("?v=" in u for u in assets)


def test_table_page_streams_updates_over_sse(server, tab):
    tab.goto(_url(server, "/table/orders_summary"))
    tab.wait_for('document.querySelectorAll("#tbody tr").length > 0')
    before = _rows(tab)
    status, _ = _request(
        server,
        "POST",
        ROOT + "/api/tables/orders",
        {"user_id": 1, "product_id": 2, "amount": 3},
    )
    assert status == 201
    tab.wait_for(f'document.querySelectorAll("#tbody tr").length === {before + 1}')
    streams = [
        r
        for r in tab.network("Network.requestWillBeSent")
        if r.get("type") == "EventSource"
    ]
    assert [r["request"]["url"] for r in streams] == [
        _url(server, "/api/views/orders_summary/stream")
    ]
    banner = tab.eval(
        'document.getElementById("alert-banner").getAttribute("aria-live")'
    )
    assert banner == "polite"  # warning は polite(critical なら role=alert)


def test_hidden_tab_closes_the_stream_and_refreshes_when_visible(server, tab):
    tab.goto(_url(server, "/table/users_view"))
    tab.wait_for('document.querySelectorAll("#tbody tr").length > 0')
    before = _rows(tab)
    tab.set_hidden(True)
    tab.cdp.pump(6.0)  # 猶予 5 秒の後に切断する
    failed = tab.network("Network.loadingFailed")
    assert any(f.get("canceled") for f in failed), failed
    _request(server, "POST", ROOT + "/api/tables/users", {"name": "Hidden"})
    tab.cdp.pump(1.0)
    assert _rows(tab) == before  # 非表示中は接続していない
    tab.set_hidden(False)
    tab.wait_for(f'document.querySelectorAll("#tbody tr").length === {before + 1}')


def test_chart_view_renders_the_collapsed_table_lazily(server, tab):
    tab.goto(_url(server, "/table/products_view"))
    tab.wait_for('/全 \\d+ 行/.test(document.getElementById("row-count").textContent)')
    assert _rows(tab) == 0  # 閉じている間は描かない
    count = int(
        tab.eval('document.getElementById("row-count").textContent.match(/\\d+/)[0]')
    )
    tab.eval('document.querySelector("details").open = true; 1')
    tab.wait_for(f'document.querySelectorAll("#tbody tr").length === {count}')


def test_polling_fallback_skips_unchanged_responses(server, tab):
    # EventSource の無い環境(古いブラウザ・プロキシ)ではポーリングになる
    tab.send(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": "delete window.EventSource;"},
    )
    tab.goto(_url(server, "/table/users_view"))
    tab.wait_for('document.querySelectorAll("#tbody tr").length > 0')
    tab.eval(
        "window.__mutations = 0; new MutationObserver((m) => window.__mutations +="
        ' m.length).observe(document.getElementById("tbody"), { childList: true });'
        " 1"
    )
    tab.cdp.pump(4.5)
    polls = [
        r
        for r in tab.network("Network.requestWillBeSent")
        if r["request"]["url"] == _url(server, "/api/views/users_view")
    ]
    assert len(polls) >= 2
    assert tab.eval("window.__mutations") == 0  # 応答が同じなら描き直さない


def test_dashboard_renders_kpis_status_and_charts(server, tab):
    tab.goto(_url(server, "/"))
    tab.wait_for('document.querySelectorAll("#kpi-grid .kpi-card").length === 2')
    tab.wait_for('document.querySelector("#status-bar .sb-dot") !== null')
    tab.wait_for('!!Chart.getChart(document.querySelector("#dashboard-grid canvas"))')
    href = tab.eval('document.querySelector("#kpi-grid a").getAttribute("href")')
    assert href.startswith(ROOT + "/table/")
    assert tab.errors() == []


def test_kiosk_rotates_through_the_views(server, tab):
    tab.goto(_url(server, "/kiosk"))
    seen = []
    deadline = time.monotonic() + 7
    while time.monotonic() < deadline:
        title = tab.eval('document.getElementById("kiosk-title").textContent')
        if not seen or seen[-1] != title:
            seen.append(title)
        tab.cdp.pump(0.25)
    assert len(set(seen)) >= 3, seen
    assert tab.errors() == []


def test_form_submit_inserts_a_row(server, tab):
    _, body = _request(server, "GET", ROOT + "/api/tables/orders?limit=1000")
    before = len(json.loads(body)["data"])
    tab.goto(_url(server, "/form/orders"))
    tab.eval("""(() => { document.getElementById("f-user_id").value = "2";
           document.getElementById("f-product_id").value = "1";
           document.getElementById("f-amount").value = "9";
           document.querySelector(".btn-submit").click(); return 1; })()""")
    tab.wait_for(
        'document.getElementById("form-message").textContent === "登録しました"'
    )
    _, body = _request(server, "GET", ROOT + "/api/tables/orders?limit=1000")
    assert len(json.loads(body)["data"]) == before + 1


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_cells_with_only_a_background_class_stay_readable(server, tab, theme):
    """config の css_class が bg-* だけ(文字色クラスなし)でも、どちらのテーマでも読める。"""
    tab.goto(_url(server, "/table/users_view"))
    tab.eval(f'localStorage.setItem("monitor-theme", "{theme}")')
    tab.goto(_url(server, "/table/users_view"))
    tab.wait_for('document.querySelectorAll("#tbody tr").length > 0')
    classes = [
        "bg-primary",
        "bg-secondary",
        "bg-success",
        "bg-danger",
        "bg-warning",
        "bg-info",
        "bg-light",
        "bg-dark",
    ]
    tab.eval(f"""(() => {{ const tr = document.createElement("tr");
          {json.dumps(classes)}.forEach((c) => {{ const td = document.createElement("td");
            td.className = c; td.textContent = c; tr.appendChild(td); }});
          document.getElementById("tbody").appendChild(tr); return 1; }})()""")
    result = tab.eval(CONTRAST_JS)
    assert result["theme"] == theme
    assert result["failing"] == [], result["failing"]


# ---------------------------------------------------------------------------
# 取得が返らない・描画に失敗する・データが更新され続けるときの振る舞い
# ---------------------------------------------------------------------------
def _kiosk_state(tab: Tab) -> str:
    return tab.eval(
        'document.getElementById("kiosk-title").textContent + "|"'
        ' + document.getElementById("status-light").className'
    )


def test_kiosk_keeps_rotating_when_a_request_never_returns(server, tab):
    """応答が返らない要求(サーバー機の電源断など)で、取得もローテーションも止まらない。

    poll は前回の取得が終わるまで次を始めないので、getJson が打ち切らないと画面が
    古いまま固まり、stale の表示も出ない。
    """
    tab.goto(_url(server, "/kiosk"))
    tab.wait_for('document.getElementById("kiosk-title").textContent.length > 0')
    # 既定 60 秒を短くして確かめる(ローテーション 2 秒より十分短く)
    tab.eval("MonitorCore.REQUEST_TIMEOUT_MS = 800; 1")
    tab.send(
        "Fetch.enable",
        {"patterns": [{"urlPattern": "*/api/views/*", "requestStage": "Request"}]},
    )
    seen: set[str] = set()
    held: list[str] = []
    passed = 0
    states = [_kiosk_state(tab)]
    deadline = time.monotonic() + 9
    while time.monotonic() < deadline:
        tab.cdp.pump(0.1)
        for event in tab.network("Fetch.requestPaused"):
            request_id = event["requestId"]
            if request_id in seen:
                continue
            seen.add(request_id)
            if not held:
                held.append(request_id)  # 最初の 1 件は返さない
            else:
                passed += 1
                tab.send("Fetch.continueRequest", {"requestId": request_id})
        state = _kiosk_state(tab)
        if state != states[-1]:
            states.append(state)
    tab.send("Fetch.disable")
    titles = {s.split("|")[0] for s in states}
    assert held and passed >= 2, (held, passed)
    assert any("stale" in s for s in states), states  # 打ち切りを stale で示す
    assert len(titles) >= 3, states  # ローテーションも続く


def test_polling_redraws_after_a_failed_render(server, tab):
    """描画に失敗した応答を「変化なし」扱いにせず、次の取得で描き直す。"""
    tab.send(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": """
            delete window.EventSource;
            window.__failures = 1;
            let table;
            Object.defineProperty(window, "MonitorTable", { configurable: true,
              get() { return table; },
              set(value) {
                const renderBody = value.renderBody;
                value.renderBody = function () {
                  if (window.__failures > 0) { window.__failures -= 1; throw new Error("boom"); }
                  return renderBody.apply(this, arguments);
                };
                table = value;
              } });
            """},
    )
    tab.goto(_url(server, "/table/users_view"))
    tab.wait_for("window.__failures === 0")
    tab.wait_for('document.querySelectorAll("#tbody tr").length > 0', timeout=10)
    status = tab.eval('document.getElementById("status").className')
    assert status == "status live"


def test_kiosk_progress_bar_is_not_reset_by_data_updates(server, tab):
    """進行バーはビューの切替で最初から流れる。データの更新(数秒ごと)では戻さない。"""
    tab.send(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": """
            new MutationObserver(function () {
              const el = document.getElementById("kiosk");
              if (el) el.dataset.refreshInterval = "400";  // 更新をローテーションより速く
            }).observe(document, { childList: true, subtree: true });
            """},
    )
    stop = threading.Event()

    def insert_rows():
        n = 0
        while not stop.is_set():
            n += 1
            _request(server, "POST", ROOT + "/api/tables/users", {"name": f"p{n}"})
            stop.wait(0.15)

    worker = threading.Thread(target=insert_rows, daemon=True)
    worker.start()
    try:
        tab.goto(_url(server, "/kiosk"))
        widths: dict[str, float] = {}
        deadline = time.monotonic() + 7
        while time.monotonic() < deadline:
            title, fraction = tab.eval("""(() => {
                const bar = document.getElementById("kiosk-progress-fill");
                return [document.getElementById("kiosk-title").textContent,
                        bar.getBoundingClientRect().width
                          / bar.parentElement.getBoundingClientRect().width]; })()""")
            widths[title] = max(widths.get(title, 0.0), fraction)
            tab.cdp.pump(0.05)
    finally:
        stop.set()
        worker.join(5)
    users = [t for t in widths if "ユーザー" in t]
    assert users, widths
    # データ更新(0.4 秒ごと)のたびに戻ると 2 秒のバーは 2 割程度で止まる
    assert widths[users[0]] > 0.6, widths


def test_empty_alert_banner_stays_in_the_accessibility_tree(server, tab):
    """アラートが無い間もライブリージョンを残す(最初のアラートも polite で読み上げられる)。"""
    tab.goto(_url(server, "/table/users_view"))
    probe = tab.eval("""(() => {
        const real = document.getElementById("alert-banner");
        const el = document.createElement("div");
        el.id = "probe-banner"; el.className = "alert-banner";
        el.setAttribute("aria-live", "polite"); el.hidden = true;
        real.parentNode.insertBefore(el, real);
        const next = el.nextElementSibling, top = next.getBoundingClientRect().top;
        window.__probe = MonitorAlerts.create(el);
        window.__probe.update([]);
        const r = el.getBoundingClientRect();
        return { hidden: el.hidden, live: el.getAttribute("aria-live"),
                 area: r.width * r.height,
                 shift: next.getBoundingClientRect().top - top }; })()""")
    assert (probe["hidden"], probe["live"], probe["shift"]) == (False, "polite", 0)
    assert probe["area"] <= 1, probe  # 見えず、場所も取らない
    tab.send("DOM.enable")
    tab.send("Accessibility.enable")
    root = tab.send("DOM.getDocument", {"depth": 0})["root"]["nodeId"]
    node_id = tab.send(
        "DOM.querySelector", {"nodeId": root, "selector": "#probe-banner"}
    )["nodeId"]
    node = tab.send(
        "Accessibility.getPartialAXTree", {"nodeId": node_id, "fetchRelatives": False}
    )["nodes"][0]
    live = {p["name"]: p["value"]["value"] for p in node.get("properties", [])}
    assert node["ignored"] is False and live.get("live") == "polite", node
    tab.eval(
        'window.__probe.update([{view: "v", column: "c", level: "critical",'
        ' message: "m", count: 1}]); 1'
    )
    banner = tab.eval("""(() => { const el = document.getElementById("probe-banner");
        return [el.getAttribute("role"), el.getAttribute("aria-live"),
                el.getBoundingClientRect().height > 10]; })()""")
    assert banner == ["alert", "assertive", True]
