// ダッシュボードの描画(U03)。
// #dashboard-config の JSON を読み、ミニチャートグリッドと
// ステータスサマリー(#status-bar)を描画する。
//
// dashboard_json: { columns: int, views: [{name, title, chart}] }
// /api/alerts: { alerts: [{view, column, level, message, count}] }

(function () {
  "use strict";

  // ---- ステータスバー ------------------------------------------------
  var statusBar = document.getElementById("status-bar");
  var grid = document.getElementById("dashboard-grid");
  var interval = parseInt(
    (statusBar || grid || {}).dataset.refreshInterval || "5000",
    10
  );

  // アラートレベルの重み(大きいほど重篤)
  var LEVEL_WEIGHT = { info: 1, warning: 2, critical: 3 };

  // 最終取得アラートを保持(取得失敗時は前回表示を維持)
  var lastAlerts = null;

  function renderStatusBar(alerts) {
    if (!statusBar) return;
    statusBar.innerHTML = "";

    var dot = document.createElement("span");
    dot.className = "sb-dot";

    var summary = document.createElement("span");
    summary.className = "sb-summary";

    var right = document.createElement("span");
    right.className = "sb-right";
    var ts = new Date().toLocaleTimeString("ja-JP", { hour12: false });
    right.textContent = "最終更新 " + ts;

    if (!alerts || alerts.length === 0) {
      dot.classList.add("ok");
      summary.textContent = "すべて正常";
    } else {
      // 最悪レベルを計算
      var worstLevel = "info";
      alerts.forEach(function (a) {
        if ((LEVEL_WEIGHT[a.level] || 0) > (LEVEL_WEIGHT[worstLevel] || 0)) {
          worstLevel = a.level;
        }
      });
      dot.classList.add(worstLevel);

      var count = document.createElement("span");
      count.className = "sb-alert-count";
      count.textContent = "アラート " + alerts.length + "件";
      summary.appendChild(count);

      // 各メッセージをリンクとして列挙
      alerts.forEach(function (a) {
        var sep = document.createElement("span");
        sep.textContent = " / ";
        sep.className = "sb-sep";
        var link = document.createElement("a");
        link.href = "/table/" + encodeURIComponent(a.view);
        link.textContent = a.message || a.view + " " + a.column;
        link.className = "sb-alert-link";
        summary.appendChild(sep);
        summary.appendChild(link);
      });
    }

    statusBar.appendChild(dot);
    statusBar.appendChild(summary);
    statusBar.appendChild(right);
  }

  async function loadAlerts() {
    try {
      var res = await fetch("/api/alerts");
      if (!res.ok) return;
      var data = await res.json();
      lastAlerts = data.alerts || [];
      renderStatusBar(lastAlerts);
    } catch (e) {
      // 取得失敗時は前回表示を維持
      if (lastAlerts !== null) {
        renderStatusBar(lastAlerts);
      }
    }
  }

  // ---- ダッシュボードグリッド ----------------------------------------
  function buildGrid(cfg) {
    if (!grid) return;
    if (!cfg || !cfg.views || cfg.views.length === 0) {
      grid.style.display = "none";
      return;
    }

    // CSS Grid の列数設定
    grid.style.setProperty("--dash-columns", String(cfg.columns || 3));

    var charts = {}; // name -> chart
    var names = [];

    cfg.views.forEach(function (view) {
      var card = document.createElement("div");
      card.className = "dash-card card";

      // タイトル(テーブルページへのリンク)
      var titleEl = document.createElement("div");
      titleEl.className = "dash-card-title";
      var link = document.createElement("a");
      link.href = "/table/" + encodeURIComponent(view.name);
      link.textContent = view.title || view.name;
      titleEl.appendChild(link);

      // canvas
      var canvas = document.createElement("canvas");
      canvas.className = "dash-canvas";

      card.appendChild(titleEl);
      card.appendChild(canvas);
      grid.appendChild(card);

      // MonitorChart が利用可能なら作成
      if (window.MonitorChart) {
        charts[view.name] = MonitorChart.create(canvas, view.chart, { compact: true });
        names.push(view.name);
      }
    });

    if (names.length === 0) return;

    // 全カードを 1 リクエストでまとめて更新(カード毎ポーリングを避ける — #19)
    function refresh() {
      fetchBatch(names, charts);
    }
    refresh();
    setInterval(refresh, interval);
  }

  async function fetchBatch(names, charts) {
    try {
      var qs = names.map(encodeURIComponent).join(",");
      var res = await fetch("/api/views/batch?names=" + qs);
      if (!res.ok) return;
      var views = (await res.json()).views || {};
      names.forEach(function (name) {
        if (views[name]) charts[name].update(views[name]);
      });
    } catch (e) {
      /* 取得失敗時は前回表示を維持 */
    }
  }

  // ---- 初期化 ---------------------------------------------------------
  function init() {
    // ステータスバー初期描画
    loadAlerts();
    setInterval(loadAlerts, interval);

    // ダッシュボードグリッド構築
    var configEl = document.getElementById("dashboard-config");
    if (configEl) {
      try {
        var cfg = JSON.parse(configEl.textContent);
        buildGrid(cfg);
      } catch (e) {
        /* JSON パース失敗: グリッドを出さない */
      }
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
