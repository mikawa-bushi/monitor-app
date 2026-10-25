// ダッシュボードの描画(U03)。
// #dashboard-config の JSON を読み、ミニチャートグリッドと
// ステータスサマリー(#status-bar)を描画する。core.js(MonitorCore)の後に読み込む。
//
// dashboard_json: { columns: int, views: [{name, title, chart}] }
// /api/alerts: { alerts: [{view, column, level, message, count}] }
//
// 取得は MonitorCore.poll(前回の取得が終わるまで次を始めない。失敗時は間隔を伸ばす)。
// 応答が前回と同じなら描き直さない(ステータスバーは「最終更新」の時刻だけ進める)。

(function () {
  "use strict";

  var Core = window.MonitorCore;

  // ---- ステータスバー ------------------------------------------------
  var statusBar = document.getElementById("status-bar");
  var grid = document.getElementById("dashboard-grid");
  var interval = parseInt(
    (statusBar || grid || {}).dataset.refreshInterval || "5000",
    10
  );

  // 最終取得アラートと最終成功時刻を保持(取得失敗時は前回表示を維持しつつ stale 表示)
  var lastAlerts = null;
  var lastOkTime = null;
  var timeEl = null; // 「最終更新」の表示先(変化がないときはここだけ書き換える)
  var shownStale = null; // 描画中のステータスバーが stale 表示か
  var alertTracker = Core.tracker();

  function timeText(stale) {
    // 「最終更新」は最後に成功した時刻を表示する(失敗中に時刻だけ進まないように)。
    var ts = (lastOkTime || new Date()).toLocaleTimeString("ja-JP", {
      hour12: false,
    });
    return (stale ? "⚠ 接続エラー — " : "") + "最終更新 " + ts;
  }

  function renderStatusBar(alerts, stale) {
    if (!statusBar) return;

    var dot = document.createElement("span");
    dot.className = "sb-dot";

    var summary = document.createElement("span");
    summary.className = "sb-summary";

    var right = document.createElement("span");
    right.className = "sb-right";
    right.textContent = timeText(stale);
    timeEl = right;
    shownStale = stale;

    if (stale) {
      dot.classList.add("stale");
      summary.textContent = "データを更新できません";
    } else if (!alerts || alerts.length === 0) {
      dot.classList.add("ok");
      summary.textContent = "すべて正常";
    } else {
      dot.classList.add(Core.worstLevel(alerts));

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
        link.href = Core.url("/table/" + encodeURIComponent(a.view));
        link.textContent = a.message || a.view + " " + a.column;
        link.className = "sb-alert-link";
        summary.appendChild(sep);
        summary.appendChild(link);
      });
    }

    statusBar.replaceChildren(dot, summary, right);
  }

  async function loadAlerts() {
    var body;
    try {
      body = await Core.getJson("/api/alerts", alertTracker);
    } catch (e) {
      // 取得失敗時は前回表示を残しつつ stale を示す(#E9)
      if (shownStale !== true) renderStatusBar(lastAlerts || [], true);
      return false;
    }
    lastOkTime = new Date();
    if (body === Core.UNCHANGED && shownStale === false && timeEl) {
      timeEl.textContent = timeText(false); // 内容は同じ: 時刻だけ進める
      return true;
    }
    if (body !== Core.UNCHANGED) lastAlerts = body.alerts || [];
    try {
      renderStatusBar(lastAlerts || [], false);
    } catch (e) {
      alertTracker.reset(); // 描けなかった応答は、次の取得で描き直す
      return false;
    }
    return true;
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
      link.href = Core.url("/table/" + encodeURIComponent(view.name));
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
    var batchPath = "/api/views/batch?names=" + names.map(encodeURIComponent).join(",");
    var batchTracker = Core.tracker();
    Core.poll(function () {
      return fetchBatch(batchPath, batchTracker, names, charts);
    }, interval);
  }

  async function fetchBatch(batchPath, batchTracker, names, charts) {
    var body;
    try {
      body = await Core.getJson(batchPath, batchTracker);
    } catch (e) {
      if (grid) grid.classList.add("stale"); // 取得失敗を可視化(#E9)
      return false;
    }
    if (body === Core.UNCHANGED) {
      if (grid) grid.classList.remove("stale"); // 復帰したら stale 解除(#E9)
      return true; // 前回と同じ: チャートは描き直さない
    }
    var views = body.views || {};
    try {
      names.forEach(function (name) {
        if (views[name]) charts[name].update(views[name]);
      });
    } catch (e) {
      batchTracker.reset(); // 描けなかった応答は、次の取得で描き直す
      if (grid) grid.classList.add("stale");
      return false;
    }
    if (grid) grid.classList.remove("stale"); // 復帰したら stale 解除(#E9)
    return true;
  }

  // ---- 初期化 ---------------------------------------------------------
  function init() {
    // ステータスバー(初期描画 + 定期更新)
    Core.poll(loadAlerts, interval);

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
