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

  // 短いビープ音(Web Audio)。新規アラート発生時に鳴らす。
  function beep() {
    try {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      if (!Ctx) return;
      const ctx = new Ctx();
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.type = "square";
      osc.frequency.value = 880;
      gain.gain.value = 0.1;
      osc.start();
      osc.stop(ctx.currentTime + 0.25);
      osc.onended = function () { ctx.close(); };
    } catch (e) {
      /* 音が出せない環境では黙って無視 */
    }
  }

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
    let prevKeys = [];

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

      // 新規アラートが増えていれば通知音 + ブラウザ通知
      const keys = keyset(alerts);
      const isNew = keys.some(function (k) { return prevKeys.indexOf(k) === -1; });
      if (isNew) {
        beep();
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

  window.MonitorAlerts = { create: create, beep: beep, requestPermission: requestPermission };
})();
