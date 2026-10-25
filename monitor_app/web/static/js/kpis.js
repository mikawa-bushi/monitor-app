// KPIサマリーカードの描画(フェーズ2・E)。
// /api/kpis を取得して #kpi-grid にカードを並べる。一定間隔で更新する。
//
// KPI 1 件の形: { key, title, value, display, unit, target, status, link_view }
// target がある場合は達成状況を矢印で示す。status(good/bad/neutral)で色分け(CSS 任せ)。
// link_view があるカードは /table/<link_view> へのリンクとして描画する。
// 取得は MonitorCore.poll(重複しない・失敗時は間隔を伸ばす)。応答が前回と同じなら
// 描き直さない。core.js(MonitorCore)の後に読み込む。

(function () {
  "use strict";

  const Core = window.MonitorCore;
  const grid = document.getElementById("kpi-grid");
  if (!grid) return;
  const interval = parseInt(grid.dataset.refreshInterval || "5000", 10);

  function render(kpis) {
    grid.replaceChildren();
    kpis.forEach(function (k) {
      // link_view 付きの KPI は関連ビューへのリンクとして描画する
      const card = document.createElement(k.link_view ? "a" : "div");
      card.className = "kpi-card " + (k.status || "neutral");
      if (k.link_view) {
        card.href = Core.url("/table/" + encodeURIComponent(k.link_view));
        card.classList.add("kpi-link");
        card.title = "関連ビューを開く";
      }

      const title = document.createElement("div");
      title.className = "kpi-title";
      title.textContent = k.title;

      const value = document.createElement("div");
      value.className = "kpi-value";
      value.textContent = k.display;
      if (k.unit) {
        const unit = document.createElement("span");
        unit.className = "kpi-unit";
        unit.textContent = k.unit;
        value.appendChild(unit);
      }

      card.appendChild(title);
      card.appendChild(value);

      if (k.target != null) {
        const targetRow = document.createElement("div");
        targetRow.className = "kpi-target-row";

        const targetLabel = document.createElement("span");
        targetLabel.className = "kpi-title";
        targetLabel.textContent = "目標 " + k.target;
        targetRow.appendChild(targetLabel);

        // 達成状況を矢印などで示す(status は API が good/bad/neutral を返す)
        const arrow = document.createElement("span");
        arrow.className = "kpi-status-arrow";
        if (k.status === "good") {
          arrow.textContent = " ↑";
          arrow.setAttribute("aria-label", "目標達成");
        } else if (k.status === "bad") {
          arrow.textContent = " ↓";
          arrow.setAttribute("aria-label", "目標未達");
        } else {
          arrow.textContent = " →";
          arrow.setAttribute("aria-label", "中立");
        }
        targetRow.appendChild(arrow);

        card.appendChild(targetRow);
      }

      grid.appendChild(card);
    });
  }

  // fetch 失敗を可視化する(#E9)。前回値は残しつつ「古い」ことを示す。
  let lastOk = null;
  const tracker = Core.tracker();

  function markStale() {
    grid.classList.add("stale");
    const when = lastOk
      ? lastOk.toLocaleTimeString("ja-JP", { hour12: false })
      : "未取得";
    grid.title = "接続エラー — 最終更新 " + when;
  }

  async function load() {
    try {
      const body = await Core.getJson("/api/kpis", tracker);
      if (body !== Core.UNCHANGED) render(body.kpis || []);
    } catch (e) {
      // 取得・描画の失敗時は前回表示を残しつつ stale 表示。描けなかった応答も
      // 「変化なし」扱いにせず、次の取得で描き直す
      tracker.reset();
      markStale();
      return false;
    }
    lastOk = new Date();
    grid.classList.remove("stale");
    grid.removeAttribute("title");
    return true;
  }

  Core.poll(load, interval);
})();
