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

(function () {
  "use strict";

  const root = document.getElementById("app-data");
  const viewName = root.dataset.viewName;
  const interval = parseInt(root.dataset.refreshInterval || "2000", 10);
  const mode = root.dataset.refreshMode || "sse";

  const titleEl = document.getElementById("view-title");
  const theadRow = document.getElementById("thead-row");
  const tbody = document.getElementById("tbody");
  const statusEl = document.getElementById("status");
  const statusText = document.getElementById("status-text");
  const bannerEl = document.getElementById("alert-banner");
  const rowCountEl = document.getElementById("row-count");

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
  let pollTimer = null;
  let source = null;

  function setStatus(state, text) {
    statusEl.className = "status" + (state ? " " + state : "");
    statusText.textContent = text;
  }

  // テーブル描画は MonitorTable(table-render.js)に集約(#20)。
  const EMPTY_TEXT =
    "データがありません — " +
    "ソース同期(monitor-app sync-sources)またはツールの稼働状況を確認してください";

  function render(payload) {
    if (payload.title) titleEl.textContent = payload.title;
    const cols = payload.columns || [];
    if (cols.join("|") !== columns.join("|")) {
      columns = cols;
      MonitorTable.renderHead(theadRow, cols, payload.column_labels);
    }
    const rows = payload.data || [];
    MonitorTable.renderBody(tbody, rows, cols, payload.cell_styles || {}, {
      emptyText: EMPTY_TEXT,
    });
    // #row-count バッジを更新(chart 付きビューの <details> summary に表示)
    if (rowCountEl) rowCountEl.textContent = "全 " + rows.length + " 行";
    alertUi.update(payload.alerts);
    chart.update(payload);
    setStatus("live", "更新中");
  }

  async function fetchOnce() {
    try {
      const res = await fetch("/api/views/" + encodeURIComponent(viewName));
      if (!res.ok) throw new Error("HTTP " + res.status);
      render(await res.json());
    } catch (e) {
      setStatus("error", "取得に失敗しました");
    }
  }

  function startPolling() {
    if (pollTimer) return;
    fetchOnce();
    pollTimer = setInterval(fetchOnce, interval);
  }

  function startSse() {
    source = new EventSource("/api/views/" + encodeURIComponent(viewName) + "/stream");
    source.onmessage = function (ev) {
      render(JSON.parse(ev.data));
    };
    source.onerror = function () {
      // SSE が使えない/切れた場合はポーリングへフォールバック
      source.close();
      source = null;
      startPolling();
    };
  }

  window.addEventListener("beforeunload", function () {
    if (source) source.close();
    if (pollTimer) clearInterval(pollTimer);
  });

  if (mode === "sse" && "EventSource" in window) {
    startSse();
  } else {
    startPolling();
  }
})();
