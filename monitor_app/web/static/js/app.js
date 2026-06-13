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

  // セルに適用するクラス(config の greater_than / less_than / equal_to ルール)
  function cellClass(value, rules) {
    if (!rules) return "";
    const num = parseFloat(value);
    if (rules.greater_than && num > rules.greater_than.value) return rules.greater_than.class;
    if (rules.less_than && num < rules.less_than.value) return rules.less_than.class;
    if (rules.equal_to && num === rules.equal_to.value) return rules.equal_to.class;
    return "";
  }

  // セルのインラインスタイル(width / font_size / align / bold)
  function applyCellStyle(td, rules) {
    if (!rules) return;
    if (rules.width) td.style.width = rules.width;
    if (rules.font_size) td.style.fontSize = rules.font_size;
    if (rules.align) td.style.textAlign = rules.align;
    if (rules.bold) td.style.fontWeight = "bold";
  }

  function renderHead(cols, labels) {
    labels = labels || {};
    theadRow.replaceChildren();
    cols.forEach(function (col) {
      const th = document.createElement("th");
      // config の labels に表示名(単位込み)があればそれを、なければ列名をそのまま
      th.textContent = labels[col] || col;
      theadRow.appendChild(th);
    });
  }

  // §7: number かつ非整数のみ ja-JP ロケールで整形。整数・文字列・null は不変。
  function formatCell(value) {
    if (typeof value === "number" && !Number.isInteger(value)) {
      return value.toLocaleString("ja-JP", { maximumFractionDigits: 2 });
    }
    return value === null || value === undefined ? "" : String(value);
  }

  function renderBody(rows, cols, cellStyles) {
    const frag = document.createDocumentFragment();
    if (rows.length === 0) {
      const tr = document.createElement("tr");
      const td = document.createElement("td");
      td.className = "empty";
      td.colSpan = cols.length || 1;
      td.textContent =
        "データがありません — " +
        "ソース同期(monitor-app sync-sources)またはツールの稼働状況を確認してください";
      tr.appendChild(td);
      frag.appendChild(tr);
    } else {
      rows.forEach(function (row) {
        const tr = document.createElement("tr");
        cols.forEach(function (col) {
          const td = document.createElement("td");
          const value = row[col];
          td.textContent = formatCell(value);
          const rules = cellStyles[col];
          const cls = cellClass(value, rules);
          if (cls) td.className = cls;
          applyCellStyle(td, rules);
          tr.appendChild(td);
        });
        frag.appendChild(tr);
      });
    }
    tbody.replaceChildren(frag);
    // #row-count バッジを更新(chart 付きビューの <details> summary に表示)
    if (rowCountEl) {
      rowCountEl.textContent = "全 " + rows.length + " 行";
    }
  }

  function render(payload) {
    if (payload.title) titleEl.textContent = payload.title;
    const cols = payload.columns || [];
    if (cols.join("|") !== columns.join("|")) {
      columns = cols;
      renderHead(cols, payload.column_labels);
    }
    renderBody(payload.data || [], cols, payload.cell_styles || {});
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
