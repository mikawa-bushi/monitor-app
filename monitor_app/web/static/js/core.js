// 画面共通の小さな部品(window.MonitorCore)。他のスクリプトより先に読み込む
// (base.html は <head>、kiosk.html は最初の <script>)。ビルド不要の素の JS。
//
//   MonitorCore.url(path)
//       アプリ内の絶対パス("/api/views/x" など)にサブパスを前置した URL を返す。
//       サブパスは <meta name="monitor-root" content="/monitor"> から読む
//       (MONITOR_ROOT_PATH。ルートで公開しているときは空)。
//   MonitorCore.getJson(path, tracker?, opts?)
//       GET して JSON を返す。HTTP エラー・通信エラーは reject。tracker を渡すと、前回と
//       同じ応答(ETag、無ければ本文)のとき MonitorCore.UNCHANGED を返す(DOM 更新を省ける)。
//       opts.timeout(既定は MonitorCore.REQUEST_TIMEOUT_MS = 60 秒)を過ぎても応答が
//       終わらなければ打ち切って reject する(MonitorCore.TimeoutError)。
//   MonitorCore.tracker()
//       getJson 用の変化検出器。reset() で「次の応答は必ず変化あり」にできる
//       (表示対象を切り替えたとき・取得や描画に失敗したとき)。getJson は応答を受けた時点で
//       記録するので、描画に失敗したら呼び出し側で reset() すること(次回に描き直させる)。
//   MonitorCore.poll(fn, ms, opts?)
//       fn(Promise を返してよい)を ms ごとに実行する。前回の実行が終わるまで次を始めない
//       (要求が積み重ならない)。fn が false を返すか reject すると、間隔を 2 倍ずつ
//       opts.maxDelay(既定: max(ms, 30 秒))まで伸ばし、成功で元に戻す。
//       戻り値 { now(), stop(), start(), isRunning() }。now() は実行中なら直後にもう一度走る。
//       opts.immediate === false なら初回も ms 後(既定はすぐ実行)。
//       fn の Promise が終わらないと次は始まらないため、fn の通信は getJson(打ち切りあり)で行う。
//   MonitorCore.worstLevel(alerts)
//       アラート配列の最も重いレベル("critical" > "warning" > "info")。空なら null。

(function () {
  "use strict";

  var meta = document.querySelector('meta[name="monitor-root"]');
  var ROOT = ((meta && meta.getAttribute("content")) || "").replace(/\/+$/, "");

  function url(path) {
    path = path || "/";
    // スキーム付き・プロトコル相対の URL はアプリ外なのでそのまま返す
    if (/^[a-z][a-z0-9+.-]*:/i.test(path) || path.indexOf("//") === 0) return path;
    if (path.charAt(0) !== "/") path = "/" + path;
    return ROOT + path;
  }

  var UNCHANGED = { unchanged: true };

  function tracker() {
    var last = null;
    return {
      // 応答の識別子(ETag または本文)を記録し、前回と同じなら true
      same: function (key) {
        if (key !== null && key === last) return true;
        last = key;
        return false;
      },
      reset: function () {
        last = null;
      },
    };
  }

  function HttpError(status) {
    this.name = "HttpError";
    this.status = status;
    this.message = "HTTP " + status;
  }
  HttpError.prototype = Object.create(Error.prototype);

  function TimeoutError(ms) {
    this.name = "TimeoutError";
    this.message = "応答がありません(" + ms + " ms)";
  }
  TimeoutError.prototype = Object.create(Error.prototype);

  // 1 回の取得の上限。サーバー機の電源断やネットワークの切断で接続が黙って死ぬと、
  // fetch は TCP の再送が尽きるまで(十数分)終わらない。poll は前回が終わるまで次を
  // 始めないので、打ち切らないと画面が古いまま止まり、stale の表示も出ない。
  // 重いビュー(数十秒かかる集計)を打ち切らないよう、上限は長めにとる。
  // 既定値は MonitorCore.REQUEST_TIMEOUT_MS で変えられる(呼び出しごとなら opts.timeout)。
  var REQUEST_TIMEOUT_MS = 60000;

  function getJson(path, track, opts) {
    var timeout = (opts && opts.timeout) || api.REQUEST_TIMEOUT_MS;
    var controller = typeof AbortController === "function" ? new AbortController() : null;
    var expired = false;
    var timer = null;
    var deadline = new Promise(function (resolve, reject) {
      timer = setTimeout(function () {
        expired = true;
        if (controller) controller.abort(); // 接続ごと打ち切る(本文の読み込み中も)
        reject(new TimeoutError(timeout));
      }, timeout);
    });

    // 打ち切った後に届いた応答で tracker を進めない(描いていない応答を「描画済み」にしない)
    function same(key) {
      return !expired && track.same(key);
    }

    async function request() {
      var init = { headers: { Accept: "application/json" } };
      if (controller) init.signal = controller.signal;
      var res = await fetch(url(path), init);
      if (!res.ok) throw new HttpError(res.status);
      if (!track) return await res.json();
      // ETag(サーバーが付ける版)が前回と同じなら本文を読まずに済ませる
      var etag = res.headers.get("ETag");
      if (etag && same(etag)) {
        if (res.body && res.body.cancel) res.body.cancel().catch(function () {});
        return UNCHANGED;
      }
      var text = await res.text();
      if (!etag && same(text)) return UNCHANGED;
      return JSON.parse(text);
    }

    return Promise.race([request(), deadline]).then(
      function (value) {
        clearTimeout(timer);
        return value;
      },
      function (error) {
        clearTimeout(timer);
        // 失敗後の最初の成功は必ず描画させる(stale 表示を確実に解除するため)
        if (track) track.reset();
        throw error;
      }
    );
  }

  function poll(fn, ms, opts) {
    opts = opts || {};
    // 不正な間隔(0・負・NaN)だと失敗時に待たずに再試行し続けるので、1 秒にする
    if (!(ms > 0)) ms = 1000;
    var maxDelay = opts.maxDelay || Math.max(ms, 30000);
    var timer = null;
    var running = false; // fn の実行中(in-flight)
    var again = false; // 実行中に now() が呼ばれた
    var stopped = false;
    var failures = 0;

    function schedule(delay) {
      clearTimeout(timer);
      timer = stopped ? null : setTimeout(tick, delay);
    }

    function done(ok) {
      running = false;
      failures = ok ? 0 : failures + 1;
      if (stopped) return;
      if (again) {
        again = false;
        schedule(0);
        return;
      }
      schedule(ok ? ms : Math.min(ms * Math.pow(2, failures), maxDelay));
    }

    function tick() {
      timer = null;
      if (stopped) return;
      if (running) {
        again = true;
        return;
      }
      running = true;
      var result;
      try {
        result = Promise.resolve(fn());
      } catch (e) {
        result = Promise.reject(e);
      }
      result.then(
        function (value) {
          done(value !== false);
        },
        function () {
          done(false);
        }
      );
    }

    var handle = {
      now: function () {
        if (stopped) return handle;
        if (running) again = true;
        else schedule(0);
        return handle;
      },
      stop: function () {
        stopped = true;
        again = false;
        clearTimeout(timer);
        timer = null;
        return handle;
      },
      start: function () {
        if (!stopped) return handle;
        stopped = false;
        failures = 0;
        return handle.now();
      },
      isRunning: function () {
        return !stopped;
      },
    };

    if (opts.immediate === false) schedule(ms);
    else tick();
    return handle;
  }

  var LEVEL_WEIGHT = { info: 1, warning: 2, critical: 3 };

  function worstLevel(alerts) {
    if (!alerts || alerts.length === 0) return null;
    var worst = "info";
    alerts.forEach(function (a) {
      var level = a && a.level;
      if ((LEVEL_WEIGHT[level] || 0) > LEVEL_WEIGHT[worst]) worst = level;
    });
    return worst;
  }

  var api = {
    root: ROOT,
    url: url,
    getJson: getJson,
    tracker: tracker,
    UNCHANGED: UNCHANGED,
    HttpError: HttpError,
    TimeoutError: TimeoutError,
    REQUEST_TIMEOUT_MS: REQUEST_TIMEOUT_MS,
    poll: poll,
    worstLevel: worstLevel,
  };
  window.MonitorCore = api;
})();
