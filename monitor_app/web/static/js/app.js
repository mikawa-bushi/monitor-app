// ビュー表示用のバニラ JavaScript(フレームワーク非依存)。
// API からデータを取得して <table> を描画する。
// SSE(差分配信)を優先し、使えない/切れた場合はポーリングにフォールバックする。
//
// 依存する DOM(table.html が用意する。id を変えるなら両方直すこと):
//   #app-data[data-view-name|data-refresh-interval|data-refresh-mode] … 設定の受け取り口
//   #view-title                         … 見出しの差し替え先
//   #status / #status-text              … 接続状態インジケータ(class を live/error に変える)
//   #thead-row / #tbody                 … テーブルの見出し行 / データ行の挿入先
//   #row-count (任意)                   … データテーブル summary の行数バッジ
// 依存するスクリプト: core.js(MonitorCore)、table-render.js(MonitorTable)、
//   alerts-ui.js(MonitorAlerts)、chart 付きなら chart-view.js(MonitorChart)。
//
// 消費する API ペイロード(GET /api/views/<name> と SSE が返す JSON):
//   { title, columns: string[], column_labels: { <col>: string }, data: object[],
//     alerts: object[] | undefined,
//     cell_styles: { <col>: { greater_than|less_than|equal_to: {value, class},
//                             width, font_size, align, bold } } }
//   cell_styles のルールは config.py の CellStyle に対応する。
//   column_labels は見出しの表示名(単位込み)。無い列は列名をそのまま表示する。
//   data の各行は { <col>: number|string|null } のオブジェクト。
//   number かつ非整数の値は toLocaleString("ja-JP", {maximumFractionDigits: 2}) で整形する。
//
// 描画を減らす工夫(FE-03):
//   - ポーリングで応答が前回と同じ(ETag / 本文)なら DOM に触れない。
//   - chart 付きビューの折りたたみテーブルは、閉じている間は描かず、開いたときに
//     最新のデータで描く。
// 接続数の節約(CRIT-09): タブが非表示のまま一定時間たつと SSE を閉じ、表示に戻ったら
//   開き直す(最初のイベントで最新に戻る)。ブラウザの同一ホスト 6 接続の上限を
//   バックグラウンドのタブが使い切らないようにする。非表示の間もアラートの通知だけは
//   短い取得(/api/alerts)で続ける。

(function () {
  "use strict";

  const Core = window.MonitorCore;
  const root = document.getElementById("app-data");
  const viewName = root.dataset.viewName;
  const interval = parseInt(root.dataset.refreshInterval || "2000", 10);
  const mode = root.dataset.refreshMode || "sse";
  const viewPath = "/api/views/" + encodeURIComponent(viewName);

  // 非表示になってから SSE を閉じるまでの猶予(タブを一瞬切り替えただけでは切らない)
  const HIDDEN_GRACE_MS = 5000;
  // 非表示中にアラートだけを確認する間隔
  const BACKGROUND_ALERT_MS = Math.max(interval, 15000);

  const titleEl = document.getElementById("view-title");
  const theadRow = document.getElementById("thead-row");
  const tbody = document.getElementById("tbody");
  const statusEl = document.getElementById("status");
  const statusText = document.getElementById("status-text");
  const bannerEl = document.getElementById("alert-banner");
  const rowCountEl = document.getElementById("row-count");
  // chart 付きビューではテーブルが <details> の中にある(閉じている間は描かない)
  const collapseEl = tbody ? tbody.closest("details") : null;

  // アラート表示(alerts-ui.js)。要素が無い場合も動くようにフォールバック。
  const alertUi =
    bannerEl && window.MonitorAlerts
      ? window.MonitorAlerts.create(bannerEl)
      : { update: function () {} };
  if (window.MonitorAlerts) window.MonitorAlerts.requestPermission();

  // グラフ表示(chart-view.js)。chart-config がある場合のみ。
  let chart = { update: function () {} };
  const chartCfgEl = document.getElementById("chart-config");
  const canvasEl = document.getElementById("chart");
  if (chartCfgEl && canvasEl && window.MonitorChart) {
    chart = window.MonitorChart.create(canvasEl, JSON.parse(chartCfgEl.textContent));
  }

  let columns = [];
  let pendingTable = null; // 折りたたみ中に届いた最新ペイロード(開いたときに描く)
  let transport = mode === "sse" && "EventSource" in window ? "sse" : "polling";
  let source = null; // EventSource
  let poller = null; // MonitorCore.poll のハンドル(ポーリング時)
  let alertPoller = null; // 非表示中のアラート確認
  let hideTimer = null;
  // SSE の連続再接続失敗カウント。これを超えて初めてポーリングへ降格する(#U2)。
  let sseErrors = 0;
  const MAX_SSE_ERRORS = 6;
  const viewTracker = Core.tracker();
  const alertTracker = Core.tracker();

  function setStatus(state, text) {
    statusEl.className = "status" + (state ? " " + state : "");
    statusText.textContent = text;
  }

  // テーブル描画は MonitorTable(table-render.js)に集約(#20)。
  const EMPTY_TEXT =
    "データがありません — " +
    "ソース同期(monitor-app sync-sources)またはツールの稼働状況を確認してください";

  function renderTable(payload) {
    const cols = payload.columns || [];
    if (cols.join("|") !== columns.join("|")) {
      columns = cols;
      MonitorTable.renderHead(theadRow, cols, payload.column_labels);
    }
    MonitorTable.renderBody(tbody, payload.data || [], cols, payload.cell_styles || {}, {
      emptyText: EMPTY_TEXT,
    });
  }

  function render(payload) {
    if (payload.title) titleEl.textContent = payload.title;
    const rows = payload.data || [];
    // #row-count バッジを更新(chart 付きビューの <details> summary に表示)
    if (rowCountEl) rowCountEl.textContent = "全 " + rows.length + " 行";
    if (collapseEl && !collapseEl.open) {
      pendingTable = payload;
    } else {
      pendingTable = null;
      renderTable(payload);
    }
    alertUi.update(payload.alerts);
    chart.update(payload);
    setStatus("live", "更新中");
  }

  if (collapseEl) {
    collapseEl.addEventListener("toggle", function () {
      if (collapseEl.open && pendingTable) {
        const payload = pendingTable;
        pendingTable = null;
        renderTable(payload);
      }
    });
  }

  // --- ポーリング ------------------------------------------------------------
  async function fetchOnce() {
    try {
      const payload = await Core.getJson(viewPath, viewTracker);
      if (payload === Core.UNCHANGED) {
        setStatus("live", "更新中"); // 変化なし: DOM の再構築は省く
      } else {
        render(payload);
      }
      return true;
    } catch (e) {
      // 描画で失敗した応答も「変化なし」扱いにせず、次の取得で描き直す
      viewTracker.reset();
      setStatus("error", "取得に失敗しました");
      return false;
    }
  }

  function startPolling() {
    if (poller) poller.start();
    else poller = Core.poll(fetchOnce, interval);
  }

  function fallbackToPolling() {
    closeStream();
    clearTimeout(hideTimer);
    hideTimer = null;
    if (alertPoller) alertPoller.stop(); // ポーリングはアラートも含めて取得する
    transport = "polling";
    startPolling();
  }

  // --- SSE --------------------------------------------------------------------
  function closeStream() {
    if (source) {
      source.close();
      source = null;
    }
  }

  function openStream() {
    closeStream();
    sseErrors = 0;
    const es = new EventSource(Core.url(viewPath + "/stream"));
    source = es;
    es.onopen = function () {
      sseErrors = 0;
      setStatus("live", "更新中");
    };
    es.onmessage = function (ev) {
      sseErrors = 0;
      render(JSON.parse(ev.data));
    };
    // 設定ページで保存した(サーバーが配信を終える)。新しい設定でページを読み直す。
    es.addEventListener("reload", function () {
      closeStream();
      window.location.reload();
    });
    es.onerror = function () {
      if (source !== es) return; // 自分で閉じた古い接続
      // EventSource はネットワーク瞬断時、自動で再接続を試みる(readyState=
      // CONNECTING)。一過性の切断でポーリングへ恒久降格しないよう、連続失敗が
      // 閾値を超えたときだけフォールバックする(#U2)。成功時に sseErrors は
      // onopen/onmessage で 0 に戻る。
      if (es.readyState === EventSource.CLOSED) {
        // サーバ/プロキシが event-stream を返さない等の致命的失敗 → 即降格。
        fallbackToPolling();
        return;
      }
      // CONNECTING: ブラウザが自動再接続中。閾値まで待つ。
      sseErrors += 1;
      setStatus("error", "再接続中… (" + sseErrors + "/" + MAX_SSE_ERRORS + ")");
      if (sseErrors >= MAX_SSE_ERRORS) {
        fallbackToPolling();
      }
    };
  }

  // --- タブが非表示の間(SSE のみ)-------------------------------------------
  async function checkAlerts() {
    try {
      const body = await Core.getJson("/api/alerts", alertTracker);
      if (body !== Core.UNCHANGED) alertUi.update(body.alerts || []);
      return true;
    } catch (e) {
      alertTracker.reset();
      return false;
    }
  }

  function enterBackground() {
    hideTimer = null;
    closeStream();
    alertTracker.reset();
    if (alertPoller) alertPoller.start();
    else alertPoller = Core.poll(checkAlerts, BACKGROUND_ALERT_MS, { immediate: false });
  }

  function leaveBackground() {
    if (alertPoller) alertPoller.stop();
    if (!source) openStream(); // 最初のイベントで最新の状態に戻る
  }

  document.addEventListener("visibilitychange", function () {
    if (transport !== "sse") return; // ポーリングは接続を占有しないので対象外
    if (document.hidden) {
      if (!hideTimer && source) hideTimer = setTimeout(enterBackground, HIDDEN_GRACE_MS);
    } else {
      clearTimeout(hideTimer);
      hideTimer = null;
      leaveBackground();
    }
  });

  // --- 開始・終了 ------------------------------------------------------------
  function start() {
    if (transport === "sse") {
      if (document.hidden) enterBackground(); // 裏で開かれたタブは表示されてから接続する
      else openStream();
    } else {
      startPolling();
    }
  }

  function stopAll() {
    clearTimeout(hideTimer);
    hideTimer = null;
    closeStream();
    if (poller) poller.stop();
    if (alertPoller) alertPoller.stop();
  }

  // beforeunload は bfcache(戻る/進むの高速表示)を無効にするブラウザがあるため
  // pagehide で後始末し、bfcache から戻ったら再開する。
  window.addEventListener("pagehide", stopAll);
  window.addEventListener("pageshow", function (ev) {
    if (ev.persisted) start();
  });

  start();
})();
