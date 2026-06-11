// Andon / 大型表示モード(フェーズ1・B)。
// 複数ビューを一定間隔で自動ローテーションし、各ビューのデータと全体ステータスを表示する。
// アラート発生中はローテーションを止め、該当ビューに固定して強調する。
//
// 依存する DOM(kiosk.html):
//   #kiosk[data-views|data-rotate-seconds|data-refresh-interval]
//   #kiosk-title #kiosk-clock #status-light #alert-banner
//   #thead-row #tbody #kiosk-dots

(function () {
  "use strict";

  const root = document.getElementById("kiosk");
  const views = (root.dataset.views || "").split(",").filter(Boolean);
  const rotateMs = parseInt(root.dataset.rotateSeconds || "15", 10) * 1000;
  const refreshMs = parseInt(root.dataset.refreshInterval || "2000", 10);

  const titleEl = document.getElementById("kiosk-title");
  const clockEl = document.getElementById("kiosk-clock");
  const lightEl = document.getElementById("status-light");
  const theadRow = document.getElementById("thead-row");
  const tbody = document.getElementById("tbody");
  const dotsEl = document.getElementById("kiosk-dots");
  const bannerEl = document.getElementById("alert-banner");

  const alertUi = window.MonitorAlerts.create(bannerEl);

  let current = 0;
  let pinned = false; // アラートで固定中か
  let columns = [];

  // 下部のローテーションドットを構築
  views.forEach(function (_v, i) {
    const dot = document.createElement("span");
    dot.className = "dot";
    dotsEl.appendChild(dot);
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
    return level;
  }

  function renderTable(payload) {
    const cols = payload.columns || [];
    if (cols.join("|") !== columns.join("|")) {
      columns = cols;
      theadRow.replaceChildren();
      cols.forEach(function (col) {
        const th = document.createElement("th");
        th.textContent = col;
        theadRow.appendChild(th);
      });
    }
    const frag = document.createDocumentFragment();
    (payload.data || []).forEach(function (row) {
      const tr = document.createElement("tr");
      cols.forEach(function (col) {
        const td = document.createElement("td");
        const styles = (payload.cell_styles || {})[col];
        const value = row[col];
        td.textContent = value === null || value === undefined ? "" : value;
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

  async function loadCurrent() {
    const name = views[current];
    if (!name) return;
    try {
      const res = await fetch("/api/views/" + encodeURIComponent(name));
      if (!res.ok) throw new Error("HTTP " + res.status);
      const payload = await res.json();
      titleEl.textContent = payload.title || name;
      renderTable(payload);
      const alerts = payload.alerts || [];
      alertUi.update(alerts);
      const level = setLight(alerts);
      // critical のビューがあればそこに固定。なければローテーション再開。
      pinned = level === "critical";
      markDot();
    } catch (e) {
      lightEl.className = "status-light";
    }
  }

  function rotate() {
    if (pinned || views.length <= 1) return;
    current = (current + 1) % views.length;
    loadCurrent();
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
  setInterval(loadCurrent, refreshMs); // 現在ビューのデータ更新
  setInterval(rotate, rotateMs); // ビュー切替
})();
