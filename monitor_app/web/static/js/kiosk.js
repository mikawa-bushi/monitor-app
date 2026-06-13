// Andon / 大型表示モード(フェーズ1・B、v2.3 で拡充)。
// 複数ビューを一定間隔で自動ローテーションし、KPI ストリップ・チャート・
// テーブル・全体ステータスを表示する。critical アラート発生中はローテーションを
// 止めて該当ビューに固定し、画面全体を点滅枠で強調する(アンドン動作)。
//
// 依存する DOM(kiosk.html):
//   #kiosk[data-rotate-seconds|data-refresh-interval]
//   #kiosk-config(type="application/json"、{views: [{name, title, chart|null}]})
//   #kiosk-title #kiosk-clock #status-light #alert-banner
//   #kiosk-progress-fill #kiosk-kpis(任意) #kiosk-charts #kiosk-table-wrap
//   #thead-row #tbody #kiosk-dots

(function () {
  "use strict";

  const root = document.getElementById("kiosk");
  const cfgEl = document.getElementById("kiosk-config");
  const views = cfgEl ? (JSON.parse(cfgEl.textContent).views || []) : [];
  const rotateMs = parseInt(root.dataset.rotateSeconds || "15", 10) * 1000;
  const refreshMs = parseInt(root.dataset.refreshInterval || "2000", 10);

  const titleEl = document.getElementById("kiosk-title");
  const clockEl = document.getElementById("kiosk-clock");
  const lightEl = document.getElementById("status-light");
  const theadRow = document.getElementById("thead-row");
  const tbody = document.getElementById("tbody");
  const dotsEl = document.getElementById("kiosk-dots");
  const bannerEl = document.getElementById("alert-banner");
  const kpisEl = document.getElementById("kiosk-kpis");
  const chartsEl = document.getElementById("kiosk-charts");
  const tableWrapEl = document.getElementById("kiosk-table-wrap");
  const progressEl = document.getElementById("kiosk-progress-fill");

  const alertUi = window.MonitorAlerts.create(bannerEl);

  let current = 0;
  let pinned = false; // critical アラートで固定中か
  let columns = [];
  let rotateTimer = null;

  // --- チャート(ビューごとに canvas を先に作り、表示だけ切り替える) -------
  // chart-view.js は破棄 API を持たないため、canvas の再利用ではなく
  // ビューごとの canvas を作って visibility を切り替える方式にする。
  const charts = views.map(function (v, i) {
    if (!v.chart || !window.MonitorChart) return null;
    const card = document.createElement("div");
    card.className = "kiosk-chart-card";
    card.id = "kiosk-chart-card-" + i;
    const canvas = document.createElement("canvas");
    card.appendChild(canvas);
    chartsEl.appendChild(card);
    return window.MonitorChart.create(canvas, v.chart);
  });

  // --- ローテーションのビュー名ピル(クリックでそのビューへジャンプ) -------
  views.forEach(function (v, i) {
    const pill = document.createElement("button");
    pill.type = "button";
    pill.className = "view-pill";
    pill.textContent = v.title;
    pill.addEventListener("click", function () {
      current = i;
      loadCurrent();
      restartRotation();
    });
    dotsEl.appendChild(pill);
  });
  function markDot() {
    Array.prototype.forEach.call(dotsEl.children, function (d, i) {
      d.classList.toggle("active", i === current);
    });
  }

  function setLight(alerts) {
    let level = "ok";
    if (alerts.some(function (a) { return a.level === "critical"; })) level = "critical";
    else if (alerts.some(function (a) { return a.level === "warning"; })) level = "warning";
    lightEl.className = "status-light " + level;
    // アンドン動作: critical 中は画面全体を点滅枠で強調
    document.body.classList.toggle("alert-critical", level === "critical");
    return level;
  }

  // 非整数の number のみ ja-JP で整形(app.js と同じ規則)
  function formatCell(value) {
    if (typeof value === "number" && !Number.isInteger(value)) {
      return value.toLocaleString("ja-JP", { maximumFractionDigits: 2 });
    }
    return value === null || value === undefined ? "" : String(value);
  }

  function renderTable(payload) {
    const cols = payload.columns || [];
    const labels = payload.column_labels || {};
    if (cols.join("|") !== columns.join("|")) {
      columns = cols;
      theadRow.replaceChildren();
      cols.forEach(function (col) {
        const th = document.createElement("th");
        th.textContent = labels[col] || col;
        theadRow.appendChild(th);
      });
    }
    const frag = document.createDocumentFragment();
    if ((payload.data || []).length === 0) {
      const tr = document.createElement("tr");
      const td = document.createElement("td");
      td.className = "empty";
      td.colSpan = cols.length || 1;
      td.textContent = "データがありません";
      tr.appendChild(td);
      frag.appendChild(tr);
    }
    (payload.data || []).forEach(function (row) {
      const tr = document.createElement("tr");
      cols.forEach(function (col) {
        const td = document.createElement("td");
        const styles = (payload.cell_styles || {})[col];
        const value = row[col];
        td.textContent = formatCell(value);
        if (styles) applyStyle(td, value, styles);
        tr.appendChild(td);
      });
      frag.appendChild(tr);
    });
    tbody.replaceChildren(frag);
  }

  function applyStyle(td, value, rules) {
    const num = parseFloat(value);
    let cls = "";
    if (rules.greater_than && num > rules.greater_than.value) cls = rules.greater_than.class;
    else if (rules.less_than && num < rules.less_than.value) cls = rules.less_than.class;
    else if (rules.equal_to && num === rules.equal_to.value) cls = rules.equal_to.class;
    if (cls) td.className = cls;
    if (rules.align) td.style.textAlign = rules.align;
    if (rules.bold) td.style.fontWeight = "bold";
  }

  // チャートビューかテーブルビューかで表示領域を切り替える
  function showArea(index) {
    const isChart = !!charts[index];
    chartsEl.hidden = !isChart;
    tableWrapEl.hidden = isChart;
    Array.prototype.forEach.call(chartsEl.children, function (card) {
      card.classList.toggle("active", card.id === "kiosk-chart-card-" + index);
    });
  }

  async function loadCurrent() {
    const view = views[current];
    if (!view) return;
    try {
      const res = await fetch("/api/views/" + encodeURIComponent(view.name));
      if (!res.ok) throw new Error("HTTP " + res.status);
      const payload = await res.json();
      titleEl.textContent = payload.title || view.name;
      showArea(current);
      if (charts[current]) {
        charts[current].update(payload);
      } else {
        renderTable(payload);
      }
      const alerts = payload.alerts || [];
      alertUi.update(alerts);
      const level = setLight(alerts);
      // critical のビューがあればそこに固定。なければローテーション再開。
      pinned = level === "critical";
      markDot();
      updateProgress();
    } catch (e) {
      lightEl.className = "status-light";
    }
  }

  // --- KPI ストリップ --------------------------------------------------------
  async function loadKpis() {
    if (!kpisEl) return;
    try {
      const res = await fetch("/api/kpis");
      if (!res.ok) return;
      const kpis = (await res.json()).kpis || [];
      const frag = document.createDocumentFragment();
      kpis.forEach(function (k) {
        // link_view 付きの KPI は関連ビューページへのリンクとして描画する
        const card = document.createElement(k.link_view ? "a" : "div");
        card.className = "kiosk-kpi " + (k.status || "neutral");
        if (k.link_view) {
          card.href = "/table/" + encodeURIComponent(k.link_view);
          card.classList.add("kiosk-kpi-link");
        }
        const title = document.createElement("div");
        title.className = "kiosk-kpi-title";
        title.textContent = k.title;
        const value = document.createElement("div");
        value.className = "kiosk-kpi-value";
        value.textContent = k.display;
        if (k.unit) {
          const unit = document.createElement("span");
          unit.className = "kiosk-kpi-unit";
          unit.textContent = k.unit;
          value.appendChild(unit);
        }
        card.appendChild(title);
        card.appendChild(value);
        frag.appendChild(card);
      });
      kpisEl.replaceChildren(frag);
    } catch (e) {
      /* 取得失敗時は前回表示を維持 */
    }
  }

  // --- ローテーションと進行バー ----------------------------------------------
  function updateProgress() {
    if (!progressEl) return;
    if (pinned || views.length <= 1) {
      // 固定中はバーを止める(critical 中は CSS が赤色にする)
      progressEl.style.transition = "none";
      progressEl.style.width = pinned ? "100%" : "0";
      return;
    }
    progressEl.style.transition = "none";
    progressEl.style.width = "0";
    // 一度レイアウトを確定させてから transition を有効化(リスタートのため)
    void progressEl.offsetWidth;
    progressEl.style.transition = "width " + rotateMs + "ms linear";
    progressEl.style.width = "100%";
  }

  function rotate() {
    if (pinned || views.length <= 1) return;
    current = (current + 1) % views.length;
    loadCurrent();
  }

  function restartRotation() {
    if (rotateTimer) clearInterval(rotateTimer);
    rotateTimer = setInterval(rotate, rotateMs);
    updateProgress();
  }

  function tickClock() {
    const now = new Date();
    clockEl.textContent = now.toLocaleTimeString("ja-JP", { hour12: false });
  }

  if (views.length === 0) {
    titleEl.textContent = "表示するビューがありません";
    return;
  }

  tickClock();
  setInterval(tickClock, 1000);
  loadCurrent();
  loadKpis();
  setInterval(loadCurrent, refreshMs); // 現在ビューのデータ更新
  setInterval(loadKpis, refreshMs); // KPI 更新
  restartRotation(); // ビュー切替
})();
