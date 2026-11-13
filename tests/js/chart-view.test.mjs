import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const here = path.dirname(fileURLToPath(import.meta.url));
const source = fs.readFileSync(
  path.join(here, "..", "..", "monitor_app", "web", "static", "js", "chart-view.js"),
  "utf8"
);

function load() {
  const created = [];
  const document = { documentElement: {}, addEventListener() {} };
  const window = {};
  function Chart(_ctx, config) {
    created.push(config);
    this.data = config.data;
    this.update = () => {};
  }
  Chart.defaults = { font: {} };
  vm.runInNewContext(source, {
    window,
    document,
    getComputedStyle: () => ({ getPropertyValue: () => "" }),
    Chart,
  });
  const canvas = { getContext: () => ({}) };
  return { MonitorChart: window.MonitorChart, canvas, created };
}

const rows = [
  { hour: "08:00", shipped: 120, received: 300 },
  { hour: "09:00", shipped: 180, received: 0 },
];

test("凡例はビューの見出し(column_labels)を使う", () => {
  const { MonitorChart, canvas, created } = load();
  MonitorChart.create(canvas, { type: "line", x: "hour", y: ["shipped", "received"] }).update({
    data: rows,
    column_labels: { shipped: "出荷(個)", received: "入荷(個)" },
  });
  assert.deepEqual(
    Array.from(created[0].data.datasets, (d) => d.label),
    ["出荷(個)", "入荷(個)"]
  );
});

test("見出しの無い列は列名のまま", () => {
  const { MonitorChart, canvas, created } = load();
  MonitorChart.create(canvas, { type: "bar", x: "hour", y: ["shipped", "received"] }).update({
    data: rows,
    column_labels: { shipped: "出荷(個)" },
  });
  assert.deepEqual(
    Array.from(created[0].data.datasets, (d) => d.label),
    ["出荷(個)", "received"]
  );
});

test("column_labels が無いペイロードでも描ける", () => {
  const { MonitorChart, canvas, created } = load();
  MonitorChart.create(canvas, { type: "line", x: "hour", y: "shipped" }).update({ data: rows });
  assert.deepEqual(
    Array.from(created[0].data.datasets, (d) => d.label),
    ["shipped"]
  );
});
