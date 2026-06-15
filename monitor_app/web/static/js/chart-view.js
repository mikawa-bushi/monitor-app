// ビューのグラフ表示(フェーズ2・D)。Chart.js のラッパ。
// window.MonitorChart.create(canvas, chartConfig, opts) を提供する。
//
// chartConfig(config.py の ChartDef に対応):
//   { type: "line"|"bar", x: string, y: string|string[],
//     x_label: string|null, y_label: string|null,
//     ucl: number|null, lcl: number|null, target: number|null }
//
// opts(省略可):
//   { compact: true } で凡例なし・軸タイトルなし・目盛り上限 x:6/y:5・pointRadius 0。
//   省略時(または compact 未指定)は従来の描画結果を維持する。
//
// 使い方:
//   const chart = MonitorChart.create(canvasEl, chartConfig);         // 通常
//   const chart = MonitorChart.create(canvasEl, chartConfig, {compact: true}); // compact
//   chart.update(payload);   // payload は /api/views/<v> のレスポンス
//
// テーマ追従:
//   グリッド・目盛り色は CSS 変数 --chart-grid / --chart-tick から読む。
//   document の monitor:themechange イベントで色を再適用し chart.update("none")。

(function () {
  "use strict";

  var PALETTE = ["#2563eb", "#16a34a", "#db2777", "#d97706", "#7c3aed"];
  // CSS 変数が取れない場合のフォールバック定数
  var GRID_COLOR_DEFAULT = "rgba(100, 116, 139, 0.18)"; // 目盛り線(薄いスレート)
  var TICK_COLOR_DEFAULT = "#475569"; // 目盛りテキスト

  // CSS 変数から色を読む。空なら定数にフォールバック。
  function cssVar(name, fallback) {
    var val = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return val || fallback;
  }

  function gridColor() {
    return cssVar("--chart-grid", GRID_COLOR_DEFAULT);
  }

  function tickColor() {
    return cssVar("--chart-tick", TICK_COLOR_DEFAULT);
  }

  function asArray(y) {
    return Array.isArray(y) ? y : [y];
  }

  // 横軸ラベルが "YYYY-MM-DD HH:MM:SS" 形式なら "HH:MM" に短縮する。
  // 時系列ビューの軸が日時文字列で潰れて読めない問題への対処。日時でなければ原文のまま。
  function shortenLabel(value) {
    if (typeof value !== "string") return value;
    const m = value.match(/^\d{4}-\d{2}-\d{2}[ T](\d{2}:\d{2})(?::\d{2})?/);
    return m ? m[1] : value;
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

  function create(canvas, cfg, opts) {
    var compact = opts && opts.compact === true;
    var ycols = asArray(cfg.y);
    var chart = null;

    function build(payload) {
      var rows = payload.data || [];
      var labels = rows.map(function (r) { return r[cfg.x]; });
      // compact モードでは pointRadius を 0 に固定
      var ptRadius = compact ? 0 : (cfg.type === "line" ? 3 : 0);

      var datasets = ycols.map(function (col, i) {
        var color = PALETTE[i % PALETTE.length];
        var values = rows.map(function (r) { return r[col]; });
        return {
          label: col,
          data: values,
          borderColor: color,
          backgroundColor: color,
          tension: 0.2,
          // 管理限界を外れた点を赤く強調(単一系列のときのみ)
          pointBackgroundColor: values.map(function (v) {
            var n = parseFloat(v);
            if (cfg.ucl != null && n > cfg.ucl) return "#ef4444";
            if (cfg.lcl != null && n < cfg.lcl) return "#ef4444";
            return color;
          }),
          pointRadius: ptRadius,
        };
      });

      if (cfg.ucl != null) datasets.push(constLine("UCL", cfg.ucl, "#ef4444", labels.length));
      if (cfg.lcl != null) datasets.push(constLine("LCL", cfg.lcl, "#ef4444", labels.length));
      if (cfg.target != null)
        datasets.push(constLine("目標", cfg.target, "#64748b", labels.length));

      return { labels: labels, datasets: datasets };
    }

    // グリッド・目盛り色を現在の CSS 変数から読み直してチャートに適用する。
    function applyThemeColors() {
      if (!chart) return;
      var gc = gridColor();
      var tc = tickColor();
      chart.options.scales.x.grid.color = gc;
      chart.options.scales.x.ticks.color = tc;
      chart.options.scales.x.title.color = tc;
      chart.options.scales.y.grid.color = gc;
      chart.options.scales.y.ticks.color = tc;
      chart.options.scales.y.title.color = tc;
      chart.update("none");
    }

    function update(payload) {
      var data = build(payload);
      var gc = gridColor();
      var tc = tickColor();
      if (!chart) {
        chart = new Chart(canvas.getContext("2d"), {
          type: cfg.type === "bar" ? "bar" : "line",
          data: data,
          options: {
            responsive: true,
            maintainAspectRatio: false,
            animation: false,
            plugins: {
              legend: { display: !compact },
              tooltip: {
                callbacks: {
                  // ツールチップの見出しは元の完全な日時を残す(短縮は軸目盛りのみ)
                  title: function (items) {
                    return items.length ? items[0].label : "";
                  },
                },
              },
            },
            scales: {
              x: {
                title: {
                  display: !compact,
                  text: cfg.x_label || cfg.x,
                  color: tc,
                  font: { weight: "600" },
                },
                grid: { color: gc },
                ticks: {
                  color: tc,
                  autoSkip: true,
                  maxTicksLimit: compact ? 6 : 12,
                  maxRotation: 0,
                  autoSkipPadding: 12,
                  callback: function (value) {
                    return shortenLabel(this.getLabelForValue(value));
                  },
                },
              },
              y: {
                beginAtZero: false,
                title: {
                  display: !compact && cfg.y_label != null,
                  text: cfg.y_label || "",
                  color: tc,
                  font: { weight: "600" },
                },
                grid: { color: gc },
                ticks: {
                  color: tc,
                  maxTicksLimit: compact ? 5 : 8,
                },
              },
            },
          },
        });
        // テーマ変更を listen して色を再適用する
        document.addEventListener("monitor:themechange", applyThemeColors);
      } else {
        chart.data = data;
        chart.update("none");
      }
    }

    return { update: update };
  }

  window.MonitorChart = { create: create };
})();
