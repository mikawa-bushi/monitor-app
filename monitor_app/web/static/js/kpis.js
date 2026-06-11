// KPIサマリーカードの描画(フェーズ2・E)。
// /api/kpis を取得して #kpi-grid にカードを並べる。一定間隔で更新する。
//
// KPI 1 件の形: { key, title, value, display, unit, target, status }

(function () {
  "use strict";

  const grid = document.getElementById("kpi-grid");
  if (!grid) return;
  const interval = parseInt(grid.dataset.refreshInterval || "5000", 10);

  function render(kpis) {
    grid.replaceChildren();
    kpis.forEach(function (k) {
      const card = document.createElement("div");
      card.className = "kpi-card " + (k.status || "neutral");

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
        const target = document.createElement("div");
        target.className = "kpi-title";
        target.textContent = "目標 " + k.target;
        card.appendChild(target);
      }
      grid.appendChild(card);
    });
  }

  async function load() {
    try {
      const res = await fetch("/api/kpis");
      if (!res.ok) return;
      render((await res.json()).kpis || []);
    } catch (e) {
      /* 取得失敗時は前回表示を維持 */
    }
  }

  load();
  setInterval(load, interval);
})();
