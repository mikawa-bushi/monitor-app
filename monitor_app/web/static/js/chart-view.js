// ビューのグラフ表示(フェーズ2・D)。Chart.js のラッパ。
// window.MonitorChart.create(canvas, chartConfig) を提供する。
//
// chartConfig(config.py の ChartDef に対応):
//   { type: "line"|"bar", x: string, y: string|string[],
//     ucl: number|null, lcl: number|null, target: number|null }
//
// 使い方:
//   const chart = MonitorChart.create(canvasEl, chartConfig);
//   chart.update(payload);   // payload は /api/views/<v> のレスポンス

(function () {
  "use strict";

  const PALETTE = ["#2563eb", "#16a34a", "#db2777", "#d97706", "#7c3aed"];

  function asArray(y) {
    return Array.isArray(y) ? y : [y];
  }

  // 一定値の水平線(UCL/LCL/target)を点線データセットとして作る。
  function constLine(label, value, color, length) {
    return {
      label: label,
      data: new Array(length).fill(value),
      borderColor: color,
      borderDash: [6, 4],
      borderWidth: 1,
      pointRadius: 0,
      fill: false,
      type: "line",
    };
  }

  function create(canvas, cfg) {
    const ycols = asArray(cfg.y);
    let chart = null;

    function build(payload) {
      const rows = payload.data || [];
      const labels = rows.map(function (r) { return r[cfg.x]; });

      const datasets = ycols.map(function (col, i) {
        const color = PALETTE[i % PALETTE.length];
        const values = rows.map(function (r) { return r[col]; });
        return {
          label: col,
          data: values,
          borderColor: color,
          backgroundColor: color,
          tension: 0.2,
          // 管理限界を外れた点を赤く強調(単一系列のときのみ)
          pointBackgroundColor: values.map(function (v) {
            const n = parseFloat(v);
            if (cfg.ucl != null && n > cfg.ucl) return "#ef4444";
            if (cfg.lcl != null && n < cfg.lcl) return "#ef4444";
            return color;
          }),
          pointRadius: cfg.type === "line" ? 3 : 0,
        };
      });

      if (cfg.ucl != null) datasets.push(constLine("UCL", cfg.ucl, "#ef4444", labels.length));
      if (cfg.lcl != null) datasets.push(constLine("LCL", cfg.lcl, "#ef4444", labels.length));
      if (cfg.target != null)
        datasets.push(constLine("目標", cfg.target, "#64748b", labels.length));

      return { labels: labels, datasets: datasets };
    }

    function update(payload) {
      const data = build(payload);
      if (!chart) {
        chart = new Chart(canvas.getContext("2d"), {
          type: cfg.type === "bar" ? "bar" : "line",
          data: data,
          options: {
            responsive: true,
            maintainAspectRatio: false,
            animation: false,
            plugins: { legend: { display: true } },
            scales: { y: { beginAtZero: false } },
          },
        });
      } else {
        chart.data = data;
        chart.update("none");
      }
    }

    return { update: update };
  }

  window.MonitorChart = { create: create };
})();
