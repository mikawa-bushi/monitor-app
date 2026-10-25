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
// 依存するスクリプト: core.js(MonitorCore)、alerts-ui.js、table-render.js、
//   chart 付きビューがあれば chart-view.js。
//
// データ取得は MonitorCore.poll で行う(前回の取得が終わるまで次を始めない。失敗時は
// 間隔を伸ばす)。応答が前回と同じなら描き直さない。ローテーションやビュー名の
// クリックで取得中にビューが切り替わった場合、古い取得結果は捨てる(別のビューの
// 領域へ描かない)。

(function () {
  "use strict";

  const Core = window.MonitorCore;
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
  let shownIndex = -1; // 最後に描いたビュー(進行バーを流し直すかの判定に使う)
  let pinned = false; // critical アラートで固定中か
  let columns = [];
  let rotateTimer = null;
  let refresher = null; // 現在ビューの定期取得(MonitorCore.poll)
  const viewTracker = Core.tracker(); // 表示中のビューの応答が変わったか
  const kpiTracker = Core.tracker();

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
      show(i);
      restartRotation();
    });
    dotsEl.appendChild(pill);
  });
  function markDot() {
    Array.prototype.forEach.call(dotsEl.children, function (d, i) {
      const active = i === current;
      d.classList.toggle("active", active);
      if (active) d.setAttribute("aria-current", "true");
      else d.removeAttribute("aria-current");
    });
  }

  function setLight(alerts) {
    const worst = Core.worstLevel(alerts);
    const level = worst === "critical" || worst === "warning" ? worst : "ok";
    lightEl.className = "status-light " + level;
    // アンドン動作: critical 中は画面全体を点滅枠で強調
    document.body.classList.toggle("alert-critical", level === "critical");
    return level;
  }

  // テーブル描画は MonitorTable(table-render.js)に集約(#20)。
  function renderTable(payload) {
    const cols = payload.columns || [];
    if (cols.join("|") !== columns.join("|")) {
      columns = cols;
      MonitorTable.renderHead(theadRow, cols, payload.column_labels || {});
    }
    MonitorTable.renderBody(tbody, payload.data || [], cols, payload.cell_styles || {}, {
      emptyText: "データがありません",
    });
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

  function markStale() {
    // 取得失敗を可視化する(#E9)。大型表示は誰も操作しないため、古い表示の
    // まま気付けないと現場判断を誤る。ライトを stale 状態にして明示する。
    lightEl.className = "status-light stale";
    lightEl.title = "接続エラー — データが更新されていません";
  }

  async function loadCurrent() {
    // 取得の間にローテーション/クリックで current が変わり得るため、対象を先に確定する
    const index = current;
    const view = views[index];
    if (!view) return true;
    let payload;
    try {
      payload = await Core.getJson("/api/views/" + encodeURIComponent(view.name), viewTracker);
    } catch (e) {
      if (index === current) markStale();
      return false;
    }
    if (index !== current) {
      // 取得中に別のビューへ切り替わった: この結果は描かない(新しいビューの取得が続く)
      viewTracker.reset();
      return true;
    }
    if (payload === Core.UNCHANGED) return true; // 表示中の内容と同じ
    try {
      renderView(index, view, payload);
    } catch (e) {
      viewTracker.reset(); // 描けなかった応答は「変化なし」扱いにせず、次の取得で描き直す
      markStale();
      return false;
    }
    return true;
  }

  function renderView(index, view, payload) {
    titleEl.textContent = payload.title || view.name;
    showArea(index);
    if (charts[index]) {
      charts[index].update(payload);
    } else {
      renderTable(payload);
    }
    const alerts = payload.alerts || [];
    alertUi.update(alerts);
    const level = setLight(alerts);
    lightEl.removeAttribute("title");
    // critical のビューがあればそこに固定。なければローテーション再開。
    const wasPinned = pinned;
    pinned = level === "critical";
    markDot();
    // 進行バーは、ビューが替わったときと固定の開始/解除のときだけ最初から流し直す
    // (データが更新されるたびに戻すと、更新間隔がローテーションより短いとき最後まで進まない)
    if (index !== shownIndex || pinned !== wasPinned) updateProgress();
    shownIndex = index;
  }

  // 表示するビューを切り替え、すぐに取得する(実行中の取得があれば終わった直後)
  function show(index) {
    current = index;
    viewTracker.reset(); // 切り替え後の最初の応答は必ず描く
    if (refresher) refresher.now();
  }

  // --- KPI ストリップ --------------------------------------------------------
  async function loadKpis() {
    if (!kpisEl) return true;
    let body;
    try {
      body = await Core.getJson("/api/kpis", kpiTracker);
    } catch (e) {
      return false; /* 取得失敗時は前回表示を維持 */
    }
    if (body === Core.UNCHANGED) return true;
    try {
      renderKpis(body.kpis || []);
    } catch (e) {
      kpiTracker.reset(); // 次の取得で描き直す
      return false;
    }
    return true;
  }

  function renderKpis(kpis) {
    const frag = document.createDocumentFragment();
    kpis.forEach(function (k) {
      // link_view 付きの KPI は関連ビューページへのリンクとして描画する
      const card = document.createElement(k.link_view ? "a" : "div");
      card.className = "kiosk-kpi " + (k.status || "neutral");
      if (k.link_view) {
        card.href = Core.url("/table/" + encodeURIComponent(k.link_view));
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
    show((current + 1) % views.length);
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
  refresher = Core.poll(loadCurrent, refreshMs); // 現在ビューのデータ更新
  if (kpisEl) Core.poll(loadKpis, refreshMs); // KPI 更新
  restartRotation(); // ビュー切替
})();
