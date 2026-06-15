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

  // 通知許可はロード時に勝手に要求せず、ユーザー操作(明示ボタン)を起点にする(#U4)。
  // ロード直後の許可ダイアログは唐突で拒否されやすく、拒否されると以後通知不可になる。
  // permission が未決定のときだけ小さな「通知を有効化」ボタンを置き、クリックで要求する。
  function requestPermission() {
    if (!("Notification" in window) || Notification.permission !== "default") return;
    if (document.querySelector(".notify-enable-btn")) return; // 二重設置防止
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "notify-enable-btn";
    btn.textContent = "🔔 通知を有効化";
    btn.addEventListener("click", function () {
      Promise.resolve(Notification.requestPermission()).finally(function () {
        btn.remove();
      });
    });
    document.body.appendChild(btn);
  }

  window.MonitorAlerts = { create: create, requestPermission: requestPermission };
})();
