// monitor_app/web/static/js/core.js の単体テスト(node --test)。
// ブラウザの代わりに最小限の window/document/fetch/タイマーを用意して読み込む。
// 実行: node --test tests/js/*.test.mjs   (pytest からは tests/test_frontend_js.py が呼ぶ)
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const here = path.dirname(fileURLToPath(import.meta.url));
const source = fs.readFileSync(
  path.join(here, "..", "..", "monitor_app", "web", "static", "js", "core.js"),
  "utf8"
);

// 手動で進める仮想タイマー
function fakeClock() {
  let now = 0;
  let seq = 0;
  const timers = new Map();
  return {
    setTimeout(fn, ms) {
      const id = ++seq;
      timers.set(id, { at: now + (ms || 0), fn });
      return id;
    },
    clearTimeout(id) {
      timers.delete(id);
    },
    pending() {
      return [...timers.values()].map((t) => t.at - now).sort((a, b) => a - b);
    },
    async advance(ms) {
      const end = now + ms;
      for (;;) {
        const due = [...timers.entries()]
          .filter(([, t]) => t.at <= end)
          .sort((a, b) => a[1].at - b[1].at)[0];
        if (!due) break;
        timers.delete(due[0]);
        now = due[1].at;
        due[1].fn();
        await flush();
      }
      now = end;
      await flush();
    },
  };
}

async function flush() {
  for (let i = 0; i < 10; i++) await Promise.resolve();
}

function load({ root = "/monitor", revision = null, fetchImpl, abort = false } = {}) {
  const clock = fakeClock();
  const reloads = [];
  const window = { location: { reload: () => reloads.push(1) } };
  const metas = { "monitor-root": root, "monitor-config": revision };
  const context = {
    window,
    document: {
      querySelector: (sel) => {
        const name = (sel.match(/^meta\[name="(.+)"\]$/) || [])[1];
        return name && metas[name] !== null && metas[name] !== undefined
          ? { getAttribute: () => metas[name] }
          : null;
      },
    },
    fetch: fetchImpl || (() => Promise.reject(new Error("no fetch"))),
    setTimeout: clock.setTimeout,
    clearTimeout: clock.clearTimeout,
    Promise,
    Error,
    Object,
    JSON,
    Math,
  };
  if (abort) context.AbortController = AbortController; // 無い古いブラウザも再現できるように
  vm.runInNewContext(source, context);
  return { core: window.MonitorCore, clock, reloads };
}

// 応答を手動で返す fetch(返すまで保留。signal で中断されたら reject)
function manualFetch() {
  const calls = [];
  const impl = (url, init) =>
    new Promise((resolve, reject) => {
      const call = { url, init, resolve };
      calls.push(call);
      const signal = init && init.signal;
      if (signal) {
        signal.addEventListener("abort", () => {
          call.aborted = true;
          const err = new Error("aborted");
          err.name = "AbortError";
          reject(err);
        });
      }
    });
  return { impl, calls };
}

function response(body, { status = 200, etag = null, config = null } = {}) {
  let read = 0;
  const headers = { etag: etag, "x-monitor-config": config };
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: (name) => headers[name.toLowerCase()] ?? null },
    body: { cancel: () => Promise.resolve() },
    text: async () => {
      read += 1;
      return body;
    },
    json: async () => {
      read += 1;
      return JSON.parse(body);
    },
    get reads() {
      return read;
    },
  };
}

test("url() prefixes the root path from the meta tag", () => {
  const { core } = load({ root: "/monitor/" });
  assert.equal(core.url("/api/views/x"), "/monitor/api/views/x");
  assert.equal(core.url("table/a"), "/monitor/table/a");
  assert.equal(core.url(""), "/monitor/");
  assert.equal(core.url("https://example.com/a"), "https://example.com/a");
  assert.equal(core.url("//cdn.example/a"), "//cdn.example/a");
  const plain = load({ root: "" }).core;
  assert.equal(plain.url("/api/kpis"), "/api/kpis");
  assert.equal(load({ root: null }).core.url("/kiosk"), "/kiosk");
});

test("worstLevel() orders critical > warning > info", () => {
  const { core } = load();
  assert.equal(core.worstLevel([]), null);
  assert.equal(core.worstLevel(null), null);
  assert.equal(core.worstLevel([{ level: "info" }]), "info");
  assert.equal(core.worstLevel([{ level: "info" }, { level: "warning" }]), "warning");
  assert.equal(
    core.worstLevel([{ level: "warning" }, { level: "critical" }, { level: "info" }]),
    "critical"
  );
  assert.equal(core.worstLevel([{ level: "strange" }]), "info");
});

test("getJson() skips unchanged responses by ETag, else by body", async () => {
  const replies = [];
  const { core } = load({ fetchImpl: async (url) => replies.shift()(url) });
  const tracker = core.tracker();
  const seen = [];
  replies.push((url) => (seen.push(url), response('{"a":1}', { etag: '"v1"' })));
  assert.deepEqual(await core.getJson("/api/views/x", tracker), { a: 1 });
  assert.deepEqual(seen, ["/monitor/api/views/x"]);

  const same = response('{"a":1}', { etag: '"v1"' });
  replies.push(() => same);
  assert.equal(await core.getJson("/api/views/x", tracker), core.UNCHANGED);
  assert.equal(same.reads, 0, "unchanged ETag must not read the body");

  replies.push(() => response('{"a":2}', { etag: '"v2"' }));
  assert.deepEqual(await core.getJson("/api/views/x", tracker), { a: 2 });

  const noEtag = core.tracker();
  replies.push(() => response('{"b":1}'));
  replies.push(() => response('{"b":1}'));
  assert.deepEqual(await core.getJson("/x", noEtag), { b: 1 });
  assert.equal(await core.getJson("/x", noEtag), core.UNCHANGED);
});

test("getJson() rejects HTTP errors and forces the next render", async () => {
  const replies = [];
  const { core } = load({ fetchImpl: async () => replies.shift() });
  const tracker = core.tracker();
  replies.push(response('{"a":1}'), response("oops", { status: 503 }), response('{"a":1}'));
  await core.getJson("/x", tracker);
  await assert.rejects(core.getJson("/x", tracker), (e) => e.status === 503);
  // 失敗の後は、内容が同じでも「変化あり」として描画させる(stale 表示の解除)
  assert.deepEqual(await core.getJson("/x", tracker), { a: 1 });
  // tracker なしは常に解析結果を返す
  replies.push(response('{"z":0}'));
  assert.deepEqual(await core.getJson("/x"), { z: 0 });
});

test("poll() never overlaps calls and runs now() right after the current one", async () => {
  const { core, clock } = load();
  let calls = 0;
  let release;
  const fn = () => {
    calls += 1;
    return new Promise((resolve) => (release = resolve));
  };
  const handle = core.poll(fn, 1000);
  assert.equal(calls, 1); // 即時に 1 回
  await clock.advance(5000); // 応答が返らない間は次を始めない
  assert.equal(calls, 1);
  handle.now(); // 実行中: 終わった直後にもう一度
  assert.equal(calls, 1);
  release(true);
  await flush();
  await clock.advance(0);
  assert.equal(calls, 2);
  release(true);
  await flush();
  assert.deepEqual(clock.pending(), [1000]);
  await clock.advance(1000);
  assert.equal(calls, 3);
});

test("poll() backs off on failures and recovers on success", async () => {
  const { core, clock } = load();
  const results = [false, false, false, false, false, true];
  let calls = 0;
  core.poll(() => {
    calls += 1;
    const r = results.shift();
    return r ? Promise.resolve(true) : Promise.reject(new Error("down"));
  }, 2000);
  await flush();
  const delays = [];
  for (let i = 0; i < 6; i++) {
    delays.push(clock.pending()[0]);
    await clock.advance(clock.pending()[0]);
  }
  // 2 倍ずつ伸ばし、上限 30 秒。成功すると元の間隔に戻る
  assert.deepEqual(delays, [4000, 8000, 16000, 30000, 30000, 2000]);
  assert.equal(calls, 7);
});

test("poll() stop() and start()", async () => {
  const { core, clock } = load();
  let calls = 0;
  const handle = core.poll(() => {
    calls += 1;
    return true;
  }, 500, { immediate: false });
  assert.equal(calls, 0);
  await clock.advance(500);
  assert.equal(calls, 1);
  handle.stop();
  assert.equal(handle.isRunning(), false);
  await clock.advance(5000);
  assert.equal(calls, 1);
  handle.now(); // 停止中は何もしない
  await clock.advance(0);
  assert.equal(calls, 1);
  handle.start();
  await clock.advance(0);
  assert.equal(calls, 2);
  assert.equal(handle.isRunning(), true);
  await clock.advance(500);
  assert.equal(calls, 3);
});

test("poll() treats a thrown error like a failure", async () => {
  const { core, clock } = load();
  let calls = 0;
  core.poll(() => {
    calls += 1;
    throw new Error("sync boom");
  }, 1000);
  await flush();
  assert.equal(calls, 1);
  assert.deepEqual(clock.pending(), [2000]);
});

test("getJson() gives up on a stalled request, aborts it and forces the next render", async () => {
  // サーバー機の電源断などで応答が返らない接続。打ち切らないと poll が止まったままになる
  const net = manualFetch();
  const { core, clock } = load({ fetchImpl: net.impl, abort: true });
  const tracker = core.tracker();
  tracker.same('{"a":1}'); // 前回の応答を記録済み
  let error = null;
  core.getJson("/api/views/x", tracker).catch((e) => (error = e));
  await flush();
  assert.equal(net.calls.length, 1);
  assert.ok(net.calls[0].init.signal, "fetch must receive an AbortSignal");
  await clock.advance(core.REQUEST_TIMEOUT_MS - 1);
  assert.equal(error, null);
  await clock.advance(1);
  assert.equal(error && error.name, "TimeoutError");
  assert.equal(net.calls[0].aborted, true);
  assert.equal(tracker.same('{"a":1}'), false, "a failure must force the next render");
  assert.deepEqual(clock.pending(), []);
});

test("getJson() timeout is configurable per call and globally", async () => {
  const net = manualFetch();
  const { core, clock } = load({ fetchImpl: net.impl, abort: true });
  let error = null;
  core.getJson("/x", null, { timeout: 500 }).catch((e) => (error = e));
  await clock.advance(500);
  assert.equal(error && error.name, "TimeoutError");
  core.REQUEST_TIMEOUT_MS = 2000;
  error = null;
  core.getJson("/x").catch((e) => (error = e));
  await clock.advance(1999);
  assert.equal(error, null);
  await clock.advance(1);
  assert.equal(error && error.name, "TimeoutError");
});

test("getJson() clears its timer when the response arrives in time", async () => {
  const net = manualFetch();
  const { core, clock } = load({ fetchImpl: net.impl, abort: true });
  const pending = core.getJson("/x", core.tracker());
  await flush();
  net.calls[0].resolve(response('{"ok":true}'));
  assert.deepEqual(await pending, { ok: true });
  assert.deepEqual(clock.pending(), []);
});

test("a response arriving after the timeout does not mark itself as rendered", async () => {
  // AbortController の無い環境: 打ち切った要求の応答が後から届いても tracker を進めない
  const net = manualFetch();
  const { core, clock } = load({ fetchImpl: net.impl, abort: false });
  const tracker = core.tracker();
  let error = null;
  core.getJson("/x", tracker).catch((e) => (error = e));
  await flush();
  await clock.advance(core.REQUEST_TIMEOUT_MS);
  assert.equal(error && error.name, "TimeoutError");
  net.calls[0].resolve(response('{"late":1}')); // 描かれなかった応答
  await flush();
  const next = core.getJson("/x", tracker);
  await flush();
  net.calls[1].resolve(response('{"late":1}'));
  assert.deepEqual(await next, { late: 1 }, "the same body must still be rendered once");
});

test("poll() falls back to 1 s for an invalid interval instead of spinning", async () => {
  for (const bad of [0, -5, NaN]) {
    const { core, clock } = load();
    let calls = 0;
    core.poll(() => {
      calls += 1;
      return Promise.reject(new Error("down"));
    }, bad);
    await flush();
    assert.equal(calls, 1);
    assert.deepEqual(clock.pending(), [2000], String(bad)); // 1 秒 × 2(失敗の後退)
  }
});

test("getJson() reloads the page once when the settings revision changes", async () => {
  const replies = [
    response('{"a":1}', { config: "rev1" }),
    response('{"a":1}', { config: "rev2" }),
    response('{"a":1}', { config: "rev3", status: 500 }),
  ];
  const { core, reloads } = load({ revision: "rev1", fetchImpl: async () => replies.shift() });
  await core.getJson("/api/alerts");
  assert.equal(reloads.length, 0); // 同じ版なら読み直さない
  await core.getJson("/api/alerts");
  assert.equal(reloads.length, 1); // 設定ページで保存した → 読み直す
  await assert.rejects(core.getJson("/api/alerts"));
  assert.equal(reloads.length, 1); // 読み直しは 1 回だけ(エラーの応答でも)
});

test("checkRevision() does nothing on pages without the revision meta", async () => {
  const { core, reloads } = load({
    revision: null,
    fetchImpl: async () => response("{}", { config: "other" }),
  });
  await core.getJson("/api/views/x");
  assert.equal(core.checkRevision(response("{}", { config: "x" })), false);
  assert.equal(reloads.length, 0);
});

test("send() posts JSON and returns the status and body without rejecting on HTTP errors", async () => {
  const calls = [];
  const { core } = load({
    fetchImpl: async (url, init) => {
      calls.push({ url, init });
      return response('{"detail":{"code":"STALE_SETTINGS","message":"先に保存されました"}}', { status: 409 });
    },
  });
  const r = await core.send("PUT", "/api/settings", { settings: {} });
  assert.equal(calls[0].url, "/monitor/api/settings");
  assert.equal(calls[0].init.method, "PUT");
  assert.equal(calls[0].init.headers["Content-Type"], "application/json");
  assert.equal(calls[0].init.body, '{"settings":{}}');
  assert.deepEqual([r.status, r.ok], [409, false]);
  assert.equal(core.errorMessage(r.data, r.status), "先に保存されました");
  assert.equal(core.errorMessage({}, 502), "HTTP 502");
});

test("send() gives up on a stalled request like getJson()", async () => {
  const net = manualFetch();
  const { core, clock } = load({ fetchImpl: net.impl, abort: true });
  let error = null;
  core.send("GET", "/api/settings").catch((e) => (error = e));
  await flush();
  assert.equal(net.calls[0].init.body, undefined);
  await clock.advance(core.REQUEST_TIMEOUT_MS);
  assert.equal(error && error.name, "TimeoutError");
  assert.equal(net.calls[0].aborted, true);
});
