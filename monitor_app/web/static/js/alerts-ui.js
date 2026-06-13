// アラート表示の共通部品(フェーズ1・A)。table.html と kiosk.html の両方が使う。
// window.MonitorAlerts として公開する。
//
// 使い方:
//   const ui = MonitorAlerts.create(bannerElement);
//   ui.update(payload.alerts);   // payload.alerts は /api/views/<v> が返すアラート配列
//
// アラート 1 件の形: { view, column, level, message, count }

(function () {
  "use strict";

  // ブラウザ通知(許可済みのときだけ)。
  function notify(alerts) {
    if (!("Notification" in window) || Notification.permission !== "granted") return;
    const critical = alerts.filter(function (a) { return a.level === "critical"; });
    const head = critical[0] || alerts[0];
    if (head) new Notification("異常検知", { body: head.message });
  }

  function highestLevel(alerts) {
    if (alerts.some(function (a) { return a.level === "critical"; })) return "critical";
    if (alerts.some(function (a) { return a.level === "warning"; })) return "warning";
    return "info";
  }

  function keyset(alerts) {
    return alerts.map(function (a) { return a.view + ":" + a.column + ":" + a.message; });
  }

  function create(bannerEl) {
    // null = 初回描画前。ページを開いた時点で既に継続中のアラートはバナー表示のみとし、
    // 表示後に「増えた」アラートだけをブラウザ通知の対象にする。
    let prevKeys = null;

    function update(alerts) {
      alerts = alerts || [];
      // バナー描画
      bannerEl.replaceChildren();
      if (alerts.length === 0) {
        bannerEl.hidden = true;
        prevKeys = [];
        return;
      }
      bannerEl.hidden = false;
      bannerEl.className = "alert-banner level-" + highestLevel(alerts);
      alerts.forEach(function (a) {
        const item = document.createElement("div");
        item.className = "alert-item";
        item.textContent = "⚠ " + a.message + "(" + a.count + " 件)";
        bannerEl.appendChild(item);
      });

      // 新規アラートが増えていればブラウザ通知(初回描画は対象外)。
      // 音は鳴らさない(現場の要望によりアラーム音は廃止。表示と通知のみ)。
      const keys = keyset(alerts);
      const isNew = prevKeys !== null &&
        keys.some(function (k) { return prevKeys.indexOf(k) === -1; });
      if (isNew) {
        notify(alerts);
      }
      prevKeys = keys;
    }

    return { update: update };
  }

  // 起動時にブラウザ通知の許可を促す(任意)。
  function requestPermission() {
    if ("Notification" in window && Notification.permission === "default") {
      Notification.requestPermission();
    }
  }

  window.MonitorAlerts = { create: create, requestPermission: requestPermission };
})();
