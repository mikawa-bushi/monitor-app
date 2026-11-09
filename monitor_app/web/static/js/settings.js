// 設定ページ(管理の機能)。/api/settings から使う設定を読み、画面で編集して、点検・保存する。
// ビルド不要の素の JS。DOM は createElement と textContent だけで組む(innerHTML を使わない)。
//
// 依存する DOM(settings.html):
//   #settings-app            … 全体(読み込み中は aria-busy="true")
//   #settings-notice         … 読み込みのエラー・画面で変えた設定があることの知らせ
//   [data-tab] / [data-panel] … タブのボタンと中身(同じ値で対応する)
//   #settings-issues         … 点検の結果
//   #settings-status         … 保存の状態
//   #btn-check / #btn-save / #btn-discard / #btn-reset
//
// 編集中の内容は画面用の形(model)で持ち、送るときに API の形(settings)へ戻す
// (ビュー・KPI は名前の付いた辞書、列の見出し・色分けは列名をキーにした辞書)。
// 保存は画面が読み込んだときの版(revision)を添えて送り、ほかの人が先に保存して
// いれば(409)上書きしない。

(function () {
  "use strict";

  const root = document.getElementById("settings-app");
  if (!root) return;
  const Core = window.MonitorCore;

  const statusEl = document.getElementById("settings-status");
  const issuesEl = document.getElementById("settings-issues");
  const noticeEl = document.getElementById("settings-notice");
  const buttons = {
    check: document.getElementById("btn-check"),
    save: document.getElementById("btn-save"),
    discard: document.getElementById("btn-discard"),
    reset: document.getElementById("btn-reset"),
  };
  const panels = {};
  root.querySelectorAll("[data-panel]").forEach(function (panel) {
    panels[panel.dataset.panel] = panel;
  });
  const tabs = root.querySelectorAll("[data-tab]");

  // セルの色分けの選択肢(app.css の色クラス。config.py の書き方と同じ)
  const STYLE_CLASSES = [
    ["", "(色なし)"],
    ["bg-danger text-white", "赤"],
    ["bg-warning text-dark", "黄"],
    ["bg-success text-white", "緑"],
    ["bg-info text-dark", "水色"],
    ["bg-primary text-white", "青"],
    ["bg-secondary text-white", "灰"],
    ["text-danger", "赤い文字"],
    ["text-success", "緑の文字"],
  ];
  const THRESHOLDS = [
    ["greater_than", "より大きい"],
    ["less_than", "より小さい"],
    ["equal_to", "等しい"],
  ];
  const OPS = [
    [">", "より大きい (>)"],
    [">=", "以上 (>=)"],
    ["<", "より小さい (<)"],
    ["<=", "以下 (<=)"],
    ["==", "等しい (==)"],
    ["!=", "等しくない (!=)"],
  ];
  const LEVELS = [
    ["info", "情報"],
    ["warning", "警告"],
    ["critical", "重大"],
  ];
  const CHANNEL_LABELS = { console: "サーバーのログ", webhook: "Webhook(Slack・Teams)", email: "メール" };
  const KPI_FORMATS = ["{:.0f}", "{:.1f}", "{:.2f}", "{:,.0f}", "{:.1f} %"];
  // ビュー・KPI の名前に使えない文字(URL・SQL・一括取得の区切りを壊す)
  const UNSAFE_NAME = /['"/\\,:#?%]/;

  let loaded = null; // 最後に読み込んだ(保存した)API の応答
  let model = null; // 編集中の内容(画面用の形)
  let viewColumns = {}; // ビュー名 → 結果の列名(点検で分かったもの)
  let dirty = false;
  let busy = false;
  let uid = 0;
  let datalists = []; // [{ el, view: () => ビュー名 }] 点検の後に列の候補を入れ直す

  // ---------------------------------------------------------------------------
  // DOM の部品
  // ---------------------------------------------------------------------------
  function h(tag, props, children) {
    const node = document.createElement(tag);
    Object.keys(props || {}).forEach(function (key) {
      const value = props[key];
      if (value === undefined || value === null || value === false) return;
      if (key === "className") node.className = value;
      else if (key === "text") node.textContent = value;
      else if (key.indexOf("on") === 0) node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value === true ? "" : String(value));
    });
    (children || []).forEach(function (child) {
      if (child === null || child === undefined || child === false) return;
      node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
    });
    return node;
  }

  function nextId(prefix) {
    uid += 1;
    return prefix + "-" + uid;
  }

  function changed() {
    if (!dirty) {
      dirty = true;
      setStatus("未保存の変更があります", "dirty");
    }
    syncButtons();
  }

  function field(label, control, hint) {
    if (!control.id) control.id = nextId("f");
    return h("div", { className: "form-row" }, [
      h("label", { for: control.id, text: label }),
      control,
      hint ? h("p", { className: "form-hint", text: hint }) : null,
    ]);
  }

  function textInput(obj, key, opts) {
    opts = opts || {};
    const input = h("input", {
      type: "text",
      placeholder: opts.placeholder,
      list: opts.list,
      autocomplete: "off",
    });
    input.value = obj[key] === null || obj[key] === undefined ? "" : String(obj[key]);
    input.addEventListener("input", function () {
      obj[key] = opts.nullable && input.value === "" ? null : input.value;
      if (opts.onInput) opts.onInput(input.value);
      changed();
    });
    return input;
  }

  // 空欄は null(未設定)。数値でない入力はそのまま送り、サーバーの点検で知らせる。
  function numberInput(obj, key, opts) {
    opts = opts || {};
    const input = h("input", {
      type: "number",
      step: opts.step || "any",
      min: opts.min,
      max: opts.max,
      inputmode: "decimal",
      placeholder: opts.placeholder,
    });
    input.value = obj[key] === null || obj[key] === undefined ? "" : String(obj[key]);
    input.addEventListener("input", function () {
      const raw = input.value.trim();
      obj[key] = raw === "" ? null : Number(raw);
      changed();
    });
    return input;
  }

  function selectInput(obj, key, options, opts) {
    opts = opts || {};
    const select = h(
      "select",
      {},
      options.map(function (opt) {
        return h("option", { value: opt[0], text: opt[1] });
      })
    );
    const current = obj[key] === null || obj[key] === undefined ? "" : String(obj[key]);
    if (!options.some((opt) => opt[0] === current)) {
      select.appendChild(h("option", { value: current, text: current }));
    }
    select.value = current;
    select.addEventListener("change", function () {
      obj[key] = opts.nullable && select.value === "" ? null : select.value;
      if (opts.onChange) opts.onChange(select.value);
      changed();
    });
    return select;
  }

  function checkInput(obj, key, label, onChange) {
    const input = h("input", { type: "checkbox" });
    input.checked = !!obj[key];
    input.addEventListener("change", function () {
      obj[key] = input.checked;
      if (onChange) onChange(input.checked);
      changed();
    });
    return h("label", { className: "settings-check" }, [input, " " + label]);
  }

  function sqlInput(obj, key) {
    const area = h("textarea", {
      className: "settings-sql",
      rows: 4,
      spellcheck: "false",
      autocapitalize: "off",
    });
    area.value = obj[key] || "";
    area.addEventListener("input", function () {
      obj[key] = area.value;
      changed();
    });
    return area;
  }

  // ビューの結果の列名を候補に出す <datalist>(点検の後に入れ直す)
  function columnList(viewName) {
    const list = h("datalist", { id: nextId("cols") });
    datalists.push({ el: list, view: viewName });
    fillColumns(list, viewName());
    return list;
  }

  function fillColumns(list, viewName) {
    list.replaceChildren();
    (viewColumns[viewName] || []).forEach(function (col) {
      list.appendChild(h("option", { value: col }));
    });
  }

  function refreshColumnLists() {
    datalists.forEach(function (entry) {
      fillColumns(entry.el, entry.view());
    });
  }

  // チェックボックスの組。options は [値, ラベルの中身(文字列・要素の配列)]。チェックを
  // 変えたら、selected(配列)を options の順に選んだ値だけへ書き換える。
  function checklist(selected, options) {
    return h(
      "div",
      { className: "settings-channels" },
      options.map(function (option) {
        const value = option[0];
        const input = h("input", { type: "checkbox", value: value });
        input.checked = selected.indexOf(value) !== -1;
        input.addEventListener("change", function () {
          const keep = options
            .map((o) => o[0])
            .filter((v) => (v === value ? input.checked : selected.indexOf(v) !== -1));
          selected.splice(0, selected.length, ...keep);
          changed();
        });
        return h("label", { className: "settings-check" }, [input, " "].concat(option[1]));
      })
    );
  }

  // list[i] を消して描き直す。ask があれば、その文で確かめてから消す。
  function removeButton(label, list, i, ask) {
    return h("button", {
      type: "button",
      className: "settings-btn settings-btn--quiet settings-btn--danger",
      text: label,
      onclick: function () {
        if (ask && !window.confirm(ask())) return;
        list.splice(i, 1);
        changed();
        render();
      },
    });
  }

  // make() で作った項目を list の最後に足して描き直す。
  function addButton(label, list, make) {
    return h("button", {
      type: "button",
      className: "settings-btn",
      text: "+ " + label,
      onclick: function () {
        list.push(make());
        changed();
        render();
      },
    });
  }

  // ---------------------------------------------------------------------------
  // API の形 ⇔ 画面用の形
  // ---------------------------------------------------------------------------
  function threshold(rule) {
    return rule ? { value: rule.value, cls: rule["class"] || "" } : { value: null, cls: "" };
  }

  function toModel(settings) {
    const views = Object.keys(settings.views || {}).map(function (name) {
      const v = settings.views[name];
      return {
        name: name,
        isNew: false,
        open: false,
        query: v.query,
        title: v.title,
        description: v.description,
        group: v.group,
        max_rows: v.max_rows,
        labels: Object.keys(v.labels || {}).map((col) => ({ column: col, text: v.labels[col] })),
        styles: Object.keys(v.styles || {}).map(function (col) {
          const s = v.styles[col];
          return {
            column: col,
            greater_than: threshold(s.greater_than),
            less_than: threshold(s.less_than),
            equal_to: threshold(s.equal_to),
            width: s.width,
            font_size: s.font_size,
            align: s.align,
            bold: !!s.bold,
          };
        }),
        chart: v.chart
          ? Object.assign({}, v.chart, {
              y: Array.isArray(v.chart.y) ? v.chart.y.join(", ") : v.chart.y,
            })
          : null,
      };
    });
    const kpis = Object.keys(settings.kpis || {}).map(function (name) {
      return Object.assign({ name: name, isNew: false, open: false }, settings.kpis[name]);
    });
    const alerts = (settings.alerts || []).map(function (a) {
      return Object.assign({}, a, { notify: (a.notify || []).slice() });
    });
    const kiosk = settings.kiosk;
    return {
      general: {
        app_title: settings.app_title,
        header_text: settings.header_text,
        footer_text: settings.footer_text,
        refresh_interval_ms: settings.refresh_interval_ms,
        refresh_mode: settings.refresh_mode,
      },
      views: views,
      kpis: kpis,
      alerts: alerts,
      kiosk: {
        enabled: !!kiosk,
        views: kiosk ? kiosk.views.slice() : [],
        rotate_seconds: kiosk ? kiosk.rotate_seconds : 15,
        theme: kiosk ? kiosk.theme : "dark",
      },
      dashboard: {
        views: (settings.dashboard.views || []).slice(),
        columns: settings.dashboard.columns,
      },
    };
  }

  function thresholdOut(rule) {
    return rule.value === null || rule.value === undefined ? null : { value: rule.value, class: rule.cls };
  }

  function blankToNull(value) {
    return value === "" || value === undefined ? null : value;
  }

  function chartOut(chart) {
    if (!chart) return null;
    const ys = String(chart.y || "")
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
    return {
      type: chart.type,
      x: chart.x,
      // 空欄は "" で送り、点検で「結果の列に無い」と知らせる
      y: ys.length === 1 ? ys[0] : ys.length === 0 ? "" : ys,
      x_label: blankToNull(chart.x_label),
      y_label: blankToNull(chart.y_label),
      ucl: chart.ucl,
      lcl: chart.lcl,
      target: chart.target,
    };
  }

  function toSettings(m) {
    const views = {};
    m.views.forEach(function (v) {
      const labels = {};
      v.labels.forEach(function (row) {
        if (row.column.trim()) labels[row.column.trim()] = row.text;
      });
      const styles = {};
      v.styles.forEach(function (s) {
        if (!s.column.trim()) return;
        styles[s.column.trim()] = {
          greater_than: thresholdOut(s.greater_than),
          less_than: thresholdOut(s.less_than),
          equal_to: thresholdOut(s.equal_to),
          width: blankToNull(s.width),
          font_size: blankToNull(s.font_size),
          align: blankToNull(s.align),
          bold: s.bold,
        };
      });
      views[v.name.trim()] = {
        query: v.query,
        title: v.title,
        description: v.description,
        group: v.group,
        max_rows: v.max_rows,
        labels: labels,
        styles: styles,
        chart: chartOut(v.chart),
      };
    });
    const kpis = {};
    m.kpis.forEach(function (k) {
      kpis[k.name.trim()] = {
        title: k.title,
        query: k.query,
        unit: k.unit,
        target: k.target,
        higher_is_better: k.higher_is_better,
        format: k.format,
        link_view: blankToNull(k.link_view),
      };
    });
    return Object.assign({}, m.general, {
      views: views,
      kpis: kpis,
      alerts: m.alerts, // 画面用の形のまま(AlertRule と同じ項目)
      kiosk: m.kiosk.enabled
        ? { views: m.kiosk.views, rotate_seconds: m.kiosk.rotate_seconds, theme: m.kiosk.theme }
        : null,
      dashboard: { views: m.dashboard.views, columns: m.dashboard.columns },
    });
  }

  // 送る前に分かる誤り(名前の空欄・重複)。辞書にすると消えてしまうため、ここで止める。
  function localIssues(m) {
    const issues = [];
    [
      ["ビュー", m.views],
      ["KPI", m.kpis],
    ].forEach(function (pair) {
      const seen = {};
      pair[1].forEach(function (item, i) {
        const name = item.name.trim();
        if (!name) {
          issues.push({ level: "error", message: pair[0] + "の " + (i + 1) + " 番目に名前がありません" });
        } else if (UNSAFE_NAME.test(name)) {
          issues.push({
            level: "error",
            message: pair[0] + " '" + name + "' の名前に使えない文字があります",
            hint: "' \" / \\ , : # ? % は使えません",
          });
        } else if (seen[name]) {
          issues.push({ level: "error", message: pair[0] + " '" + name + "' が 2 つあります(名前は重ならないようにします)" });
        }
        seen[name] = true;
      });
    });
    return issues;
  }

  function viewNames() {
    return model.views.map((v) => v.name.trim()).filter(Boolean);
  }

  function chartViewNames() {
    return model.views.filter((v) => v.chart && v.name.trim()).map((v) => v.name.trim());
  }

  // ---------------------------------------------------------------------------
  // 全般
  // ---------------------------------------------------------------------------
  function renderGeneral(panel) {
    const g = model.general;
    panel.appendChild(
      h("div", { className: "card settings-card" }, [
        field("アプリの名前", textInput(g, "app_title"), "ブラウザのタブに表示します"),
        field("画面上部の見出し", textInput(g, "header_text")),
        field("画面下部の文言", textInput(g, "footer_text")),
        field(
          "更新の間隔(ミリ秒)",
          numberInput(g, "refresh_interval_ms", { step: 100, min: 250 }),
          "画面のデータとアラートを確かめる間隔。2000 で 2 秒ごと"
        ),
        field(
          "更新の方式",
          selectInput(g, "refresh_mode", [
            ["sse", "変わったときに届ける(SSE。おすすめ)"],
            ["polling", "一定の間隔で取りに行く(ポーリング)"],
          ])
        ),
      ])
    );
  }

  // ---------------------------------------------------------------------------
  // ビュー
  // ---------------------------------------------------------------------------
  function itemCard(item, summaryChildren, body) {
    const details = h("details", { className: "card settings-item" }, [
      h("summary", { className: "settings-item__summary" }, summaryChildren),
      h("div", { className: "settings-item__body" }, body),
    ]);
    details.open = !!item.open;
    details.addEventListener("toggle", function () {
      item.open = details.open;
    });
    return details;
  }

  function nameField(item, kind) {
    if (!item.isNew) {
      return h("p", { className: "settings-name" }, ["名前: ", h("code", { text: item.name })]);
    }
    return field(
      "名前(URL に使います。あとから変えられません)",
      textInput(item, "name", { placeholder: kind === "view" ? "例: line_status" : "例: today_count" }),
      "英数字と _ をおすすめします"
    );
  }

  function renderLabels(v, cols) {
    const box = h("div", { className: "settings-sub" }, [h("h4", { text: "列の見出し" })]);
    v.labels.forEach(function (row, i) {
      box.appendChild(
        h("div", { className: "settings-inline" }, [
          h("label", { className: "settings-inline__label" }, [
            "列",
            textInput(row, "column", { list: cols.id, placeholder: "列名" }),
          ]),
          h("label", { className: "settings-inline__label" }, [
            "見出し",
            textInput(row, "text", { placeholder: "例: 温度 (℃)" }),
          ]),
          removeButton("削除", v.labels, i),
        ])
      );
    });
    box.appendChild(
      addButton("見出しを追加", v.labels, function () {
        return { column: "", text: "" };
      })
    );
    return box;
  }

  function colorSelect(rule) {
    const swatch = h("span", { className: "settings-swatch " + rule.cls, text: "123" });
    const select = selectInput(rule, "cls", STYLE_CLASSES, {
      onChange: function (value) {
        swatch.className = "settings-swatch " + value;
      },
    });
    return h("span", { className: "settings-color" }, [select, swatch]);
  }

  function renderStyles(v, cols) {
    const box = h("div", { className: "settings-sub" }, [
      h("h4", { text: "セルの色分け" }),
      h("p", { className: "form-hint", text: "列の値と比べて、当てはまるセルに色を付けます(空欄の条件は使いません)" }),
    ]);
    v.styles.forEach(function (s, i) {
      const rules = THRESHOLDS.map(function (pair) {
        const rule = s[pair[0]];
        return h("div", { className: "settings-inline" }, [
          h("span", { className: "settings-inline__label", text: pair[1] }),
          numberInput(rule, "value", { placeholder: "値" }),
          colorSelect(rule),
        ]);
      });
      box.appendChild(
        h("div", { className: "settings-style" }, [
          h("div", { className: "settings-inline" }, [
            h("label", { className: "settings-inline__label" }, [
              "列",
              textInput(s, "column", { list: cols.id, placeholder: "列名" }),
            ]),
            checkInput(s, "bold", "太字"),
            h("label", { className: "settings-inline__label" }, [
              "配置",
              selectInput(
                s,
                "align",
                [
                  ["", "(既定)"],
                  ["left", "左"],
                  ["center", "中央"],
                  ["right", "右"],
                ],
                { nullable: true }
              ),
            ]),
            removeButton("この列の色分けを削除", v.styles, i),
          ]),
        ].concat(rules))
      );
    });
    box.appendChild(
      addButton("色分けする列を追加", v.styles, function () {
        return {
          column: "",
          greater_than: threshold(null),
          less_than: threshold(null),
          equal_to: threshold(null),
          width: null,
          font_size: null,
          align: null,
          bold: false,
        };
      })
    );
    return box;
  }

  function renderChart(v, cols) {
    const box = h("div", { className: "settings-sub" }, [h("h4", { text: "グラフ" })]);
    const toggle = { on: !!v.chart };
    box.appendChild(
      checkInput(toggle, "on", "グラフで表示する(トップページのダッシュボードにも並びます)", function (on) {
        v.chart = on
          ? { type: "line", x: "", y: "", x_label: null, y_label: null, ucl: null, lcl: null, target: null }
          : null;
        render();
      })
    );
    if (!v.chart) return box;
    const c = v.chart;
    box.appendChild(
      h("div", { className: "settings-grid" }, [
        field(
          "種類",
          selectInput(c, "type", [
            ["line", "折れ線"],
            ["bar", "棒"],
          ])
        ),
        field("横軸の列", textInput(c, "x", { list: cols.id, placeholder: "例: ts" })),
        field("縦軸の列", textInput(c, "y", { list: cols.id, placeholder: "例: temp" }), "複数ならカンマで区切ります"),
        field("横軸の名前", textInput(c, "x_label", { nullable: true })),
        field("縦軸の名前・単位", textInput(c, "y_label", { nullable: true, placeholder: "例: 温度 (℃)" })),
        field("管理上限線(UCL)", numberInput(c, "ucl")),
        field("管理下限線(LCL)", numberInput(c, "lcl")),
        field("目標線", numberInput(c, "target")),
      ])
    );
    return box;
  }

  function renderViews(panel) {
    const groups = Array.from(new Set(model.views.map((v) => v.group).filter(Boolean)));
    const groupList = h(
      "datalist",
      { id: nextId("groups") },
      groups.map((g) => h("option", { value: g }))
    );
    panel.appendChild(groupList);
    panel.appendChild(
      h("p", {
        className: "form-hint",
        text: "ビューは SQL の SELECT 文の結果を表やグラフで表示します。SQL は読み取り専用で実行されます。",
      })
    );
    model.views.forEach(function (v, i) {
      const cols = columnList(function () {
        return v.name.trim();
      });
      const known = viewColumns[v.name.trim()];
      const summary = [
        h("span", { className: "settings-item__title", text: v.title || v.name || "(新しいビュー)" }),
        v.name ? h("code", { text: v.name }) : null,
        v.chart ? h("span", { className: "settings-badge", text: "グラフ" }) : null,
        v.isNew || !(v.name in loaded.config_py.views)
          ? h("span", { className: "settings-badge settings-badge--new", text: "画面で追加" })
          : null,
      ];
      panel.appendChild(
        itemCard(v, summary, [
          cols,
          nameField(v, "view"),
          h("div", { className: "settings-grid" }, [
            field("タイトル", textInput(v, "title")),
            field("グループ(メニューの見出し)", textInput(v, "group", { list: groupList.id })),
            field("説明", textInput(v, "description")),
            field("表示する最大の行数", numberInput(v, "max_rows", { step: 1, min: 1, placeholder: "制限なし" })),
          ]),
          field(
            "SQL",
            sqlInput(v, "query"),
            known ? "結果の列: " + known.join(", ") : "「点検」を押すと、結果の列を確かめられます"
          ),
          renderLabels(v, cols),
          renderStyles(v, cols),
          renderChart(v, cols),
          h("div", { className: "settings-item__footer" }, [
            removeButton("このビューを削除", model.views, i, function () {
              return "ビュー「" + (v.title || v.name) + "」を削除します。よろしいですか?";
            }),
          ]),
        ])
      );
    });
    panel.appendChild(
      addButton("ビューを追加", model.views, function () {
        return {
          name: "",
          isNew: true,
          open: true,
          query: "SELECT * FROM " + (loaded.tables[0] || "テーブル名"),
          title: "",
          description: "",
          group: "",
          max_rows: null,
          labels: [],
          styles: [],
          chart: null,
        };
      })
    );
  }

  // ---------------------------------------------------------------------------
  // KPI
  // ---------------------------------------------------------------------------
  function renderKpis(panel) {
    const formats = h(
      "datalist",
      { id: nextId("formats") },
      KPI_FORMATS.map((f) => h("option", { value: f }))
    );
    panel.appendChild(formats);
    panel.appendChild(
      h("p", {
        className: "form-hint",
        text: "KPI はトップページと大型表示に、1 つの値を大きく表示します。SQL は値を 1 つ返す SELECT 文にします。",
      })
    );
    model.kpis.forEach(function (k, i) {
      const summary = [
        h("span", { className: "settings-item__title", text: k.title || k.name || "(新しい KPI)" }),
        k.name ? h("code", { text: k.name }) : null,
        k.isNew || !(k.name in loaded.config_py.kpis)
          ? h("span", { className: "settings-badge settings-badge--new", text: "画面で追加" })
          : null,
      ];
      panel.appendChild(
        itemCard(k, summary, [
          nameField(k, "kpi"),
          h("div", { className: "settings-grid" }, [
            field("タイトル", textInput(k, "title")),
            field("単位", textInput(k, "unit", { placeholder: "例: 件・%" })),
            field("目標値", numberInput(k, "target", { placeholder: "なし" })),
            field("表示形式", textInput(k, "format", { list: formats.id }), "{:.0f} は整数、{:.1f} は小数 1 桁、{:,.0f} は桁区切り"),
            field(
              "クリックで開くビュー",
              selectInput(k, "link_view", [["", "(なし)"]].concat(viewNames().map((n) => [n, n])), {
                nullable: true,
              })
            ),
          ]),
          checkInput(k, "higher_is_better", "値が大きいほど良い(目標値以上で緑。外すと目標値以下で緑)"),
          field("SQL", sqlInput(k, "query")),
          h("div", { className: "settings-item__footer" }, [
            removeButton("この KPI を削除", model.kpis, i, function () {
              return "KPI「" + (k.title || k.name) + "」を削除します。よろしいですか?";
            }),
          ]),
        ])
      );
    });
    panel.appendChild(
      addButton("KPI を追加", model.kpis, function () {
        return {
          name: "",
          isNew: true,
          open: true,
          title: "",
          query: "SELECT COUNT(*) FROM " + (loaded.tables[0] || "テーブル名"),
          unit: "",
          target: null,
          higher_is_better: true,
          format: "{:.0f}",
          link_view: null,
        };
      })
    );
  }

  // ---------------------------------------------------------------------------
  // アラート
  // ---------------------------------------------------------------------------
  function renderAlerts(panel) {
    panel.appendChild(
      h("p", {
        className: "form-hint",
        text: "ビューの列の値が条件に当てはまる行があると、画面にバナーを出して通知します。状態が変わったときに 1 回だけ通知します。",
      })
    );
    const names = viewNames();
    model.alerts.forEach(function (a, i) {
      const cols = columnList(function () {
        return a.view;
      });
      const channels = checklist(
        a.notify,
        loaded.notify_channels.map(function (ch) {
          return [
            ch.name,
            [
              CHANNEL_LABELS[ch.name] || ch.name,
              ch.ready ? null : h("span", { className: "settings-badge", text: "未設定" }),
            ],
          ];
        })
      );
      panel.appendChild(
        h("div", { className: "card settings-alert" }, [
          cols,
          h("div", { className: "settings-grid" }, [
            field(
              "ビュー",
              selectInput(a, "view", names.map((n) => [n, n]), {
                onChange: function () {
                  refreshColumnLists();
                },
              })
            ),
            field("列", textInput(a, "column", { list: cols.id })),
            field("条件", selectInput(a, "op", OPS)),
            field("値", numberInput(a, "value")),
            field("レベル", selectInput(a, "level", LEVELS)),
            field("メッセージ", textInput(a, "message", { placeholder: "空なら条件から作ります" })),
          ]),
          h("div", { className: "form-row" }, [
            h("span", { className: "settings-label", text: "通知先(何も選ばなければサーバーのログだけ)" }),
            channels,
          ]),
          h("div", { className: "settings-item__footer" }, [
            removeButton("このアラートを削除", model.alerts, i),
          ]),
        ])
      );
    });
    panel.appendChild(
      addButton("アラートを追加", model.alerts, function () {
        return {
          view: names[0] || "",
          column: "",
          op: ">",
          value: null,
          level: "warning",
          message: "",
          notify: [],
        };
      })
    );
  }

  // ---------------------------------------------------------------------------
  // 大型表示・ダッシュボード
  // ---------------------------------------------------------------------------
  function viewChecklist(selected, names, emptyText) {
    if (!names.length) return h("p", { className: "form-hint", text: emptyText });
    return checklist(selected, names.map((n) => [n, [n]]));
  }

  function renderDisplay(panel) {
    const k = model.kiosk;
    const kioskBody = h("div", { className: "settings-sub" });
    function fillKiosk() {
      kioskBody.replaceChildren();
      if (!k.enabled) return;
      kioskBody.appendChild(
        h("div", { className: "settings-grid" }, [
          field("切り替えの間隔(秒)", numberInput(k, "rotate_seconds", { step: 1, min: 1 })),
          field(
            "配色",
            selectInput(k, "theme", [
              ["dark", "暗い(大型モニタ向け)"],
              ["light", "明るい"],
            ])
          ),
        ])
      );
      kioskBody.appendChild(
        h("div", { className: "form-row" }, [
          h("span", { className: "settings-label", text: "表示するビュー(何も選ばなければすべて)" }),
          viewChecklist(k.views, viewNames(), "ビューがありません"),
        ])
      );
    }
    fillKiosk();
    panel.appendChild(
      h("div", { className: "card settings-card" }, [
        h("h3", { text: "大型表示(Andon)" }),
        checkInput(k, "enabled", "大型表示を設定する(トップページに大型表示へのリンクを出します)", fillKiosk),
        kioskBody,
      ])
    );
    const d = model.dashboard;
    panel.appendChild(
      h("div", { className: "card settings-card" }, [
        h("h3", { text: "トップページのグラフ" }),
        field(
          "1 行に並べる数",
          selectInput(
            { value: String(d.columns) },
            "value",
            [
              ["1", "1"],
              ["2", "2"],
              ["3", "3"],
              ["4", "4"],
            ],
            {
              onChange: function (value) {
                d.columns = Number(value);
              },
            }
          )
        ),
        h("div", { className: "form-row" }, [
          h("span", { className: "settings-label", text: "並べるグラフ(何も選ばなければ、グラフのあるビューすべて)" }),
          viewChecklist(d.views, chartViewNames(), "グラフのあるビューがありません(ビューの「グラフ」で設定します)"),
        ]),
      ])
    );
  }

  // ---------------------------------------------------------------------------
  // 描画・状態
  // ---------------------------------------------------------------------------
  function render() {
    datalists = [];
    renderGeneral(clear("general"));
    renderViews(clear("views"));
    renderKpis(clear("kpis"));
    renderAlerts(clear("alerts"));
    renderDisplay(clear("display"));
  }

  function clear(name) {
    panels[name].replaceChildren();
    return panels[name];
  }

  function showTab(name) {
    if (!panels[name]) name = "general";
    tabs.forEach(function (tab) {
      tab.setAttribute("aria-selected", tab.dataset.tab === name ? "true" : "false");
    });
    Object.keys(panels).forEach(function (key) {
      panels[key].hidden = key !== name;
    });
    if (window.history && window.history.replaceState) {
      window.history.replaceState(null, "", "#" + name);
    }
  }

  function setStatus(text, kind) {
    statusEl.textContent = text;
    statusEl.className = "settings-status" + (kind ? " " + kind : "");
  }

  function syncButtons() {
    buttons.check.disabled = busy || !model;
    buttons.save.disabled = busy || !model || !dirty;
    buttons.discard.disabled = busy || !dirty;
    buttons.reset.disabled = busy || !loaded || !loaded.customized;
  }

  // 点検の結果はタブの上に出す。reveal なら(点検・保存のボタンを押したとき)見える位置まで動かす。
  function showIssues(issues, title, reveal) {
    issuesEl.replaceChildren();
    if (!issues || !issues.length) {
      issuesEl.hidden = true;
      return;
    }
    issuesEl.hidden = false;
    issuesEl.appendChild(h("h3", { text: title }));
    const list = h("ul", {});
    issues.forEach(function (issue) {
      const label = issue.level === "error" ? "エラー" : issue.level === "warning" ? "警告" : "情報";
      list.appendChild(
        h("li", { className: "issue issue--" + issue.level }, [
          h("strong", { text: label + ": " }),
          issue.message,
          issue.hint ? h("div", { className: "issue__hint", text: issue.hint }) : null,
        ])
      );
    });
    issuesEl.appendChild(list);
    if (reveal && issuesEl.scrollIntoView) issuesEl.scrollIntoView({ block: "nearest" });
  }

  function counts(issues) {
    const errors = issues.filter((i) => i.level === "error").length;
    const warnings = issues.filter((i) => i.level === "warning").length;
    return { errors: errors, warnings: warnings };
  }

  function renderNotice() {
    noticeEl.replaceChildren();
    if (loaded.load_error) {
      noticeEl.appendChild(
        h("div", { className: "settings-notice settings-notice--error" }, [
          h("p", { text: loaded.load_error }),
          h("p", { text: "保存すると、画面で変えた設定は今の画面の内容に置き換わります。" }),
        ])
      );
    }
    if (loaded.customized) {
      const when = loaded.updated_at ? new Date(loaded.updated_at).toLocaleString() : "";
      noticeEl.appendChild(
        h("div", { className: "settings-notice" }, [
          h("p", {
            text:
              "画面で変えた設定があります(" +
              [when, loaded.updated_by].filter(Boolean).join("・") +
              ")。config.py の同じ項目より優先されます。",
          }),
        ])
      );
    }
  }

  function accept(data) {
    loaded = data;
    model = toModel(data.settings);
    viewColumns = data.view_columns || {};
    dirty = false;
    renderNotice();
    render();
  }

  function needLogin() {
    setStatus("ログインが必要です", "error");
    issuesEl.hidden = false;
    issuesEl.replaceChildren(
      h("p", { text: "管理の機能を使える鍵でログインしてください。" }),
      h("a", { href: Core.url("/login?next=/settings"), text: "ログインする" })
    );
  }

  // 要求を送る。ログインが要る(401)ときと、handled に無い失敗のときは、ここで知らせて
  // null を返す(failText は失敗の知らせの頭に付ける文)。
  async function request(method, path, body, failText, handled) {
    const r = await Core.send(method, path, body);
    if (r.status === 401) {
      needLogin();
      return null;
    }
    if (!r.ok && (handled || []).indexOf(r.status) === -1) {
      setStatus(failText + ": " + Core.errorMessage(r.data, r.status), "error");
      return null;
    }
    return r;
  }

  async function run(label, task) {
    if (busy) return;
    busy = true;
    syncButtons();
    setStatus(label + "…", "pending");
    try {
      await task();
    } catch (e) {
      if (window.console) window.console.error(e);
      setStatus("通信エラーが発生しました", "error");
    } finally {
      busy = false;
      syncButtons();
    }
  }

  function load() {
    return run("読み込み中", async function () {
      const r = await request("GET", "/api/settings", undefined, "読み込めませんでした");
      if (!r) return;
      accept(r.data);
      root.setAttribute("aria-busy", "false");
      const c = counts(r.data.issues || []);
      showIssues(r.data.issues, "今の設定の点検結果");
      setStatus(
        c.errors || c.warnings
          ? "読み込みました(今の設定にエラー " + c.errors + " 件・警告 " + c.warnings + " 件)"
          : "読み込みました",
        c.errors ? "error" : ""
      );
    });
  }

  function stopOnLocalIssues() {
    const issues = localIssues(model);
    if (!issues.length) return false;
    showIssues(issues, "点検の結果", true);
    setStatus("名前を直してください", "error");
    return true;
  }

  function check() {
    if (stopOnLocalIssues()) return;
    return run("点検中", async function () {
      const r = await request("POST", "/api/settings/check", { settings: toSettings(model) }, "点検できませんでした");
      if (!r) return;
      viewColumns = Object.assign({}, viewColumns, r.data.view_columns || {});
      refreshColumnLists();
      const c = counts(r.data.issues);
      showIssues(r.data.issues, "点検の結果", true);
      setStatus(
        c.errors || c.warnings
          ? "点検しました: エラー " + c.errors + " 件・警告 " + c.warnings + " 件"
          : "点検しました: 問題は見つかりませんでした",
        c.errors ? "error" : dirty ? "dirty" : ""
      );
    });
  }

  function save() {
    if (stopOnLocalIssues()) return;
    return run("保存中", async function () {
      const r = await request(
        "PUT",
        "/api/settings",
        { settings: toSettings(model), revision: loaded.revision },
        "保存できませんでした",
        [409, 422]
      );
      if (!r) return;
      if (r.status === 409) return stale(r.data);
      if (r.status === 422) {
        showIssues(r.data.issues, "保存できませんでした", true);
        return setStatus("エラーがあるため保存していません", "error");
      }
      accept(r.data);
      showIssues(r.data.issues, "保存しました(警告があります)", true);
      setStatus("保存しました。開いている画面は自動で読み直します", "ok");
    });
  }

  function stale(data) {
    setStatus(Core.errorMessage(data, 409), "error");
    issuesEl.hidden = false;
    issuesEl.replaceChildren(
      h("p", { text: "今の画面の変更は保存していません。読み込み直すと、ほかの人が保存した設定が表示されます。" }),
      h("button", {
        type: "button",
        className: "settings-btn",
        text: "読み込み直す(今の変更は破棄)",
        onclick: function () {
          dirty = false;
          load();
        },
      })
    );
  }

  function reset() {
    if (!window.confirm("画面で変えた設定をすべて消して、config.py の設定に戻します。よろしいですか?")) return;
    return run("戻しています", async function () {
      const path = "/api/settings?revision=" + encodeURIComponent(loaded.revision);
      const r = await request("DELETE", path, undefined, "戻せませんでした", [409]);
      if (!r) return;
      if (r.status === 409) return stale(r.data);
      accept(r.data);
      showIssues(r.data.issues, "今の設定の点検結果");
      setStatus("config.py の設定に戻しました", "ok");
    });
  }

  function discard() {
    if (!loaded) return;
    accept(loaded);
    showIssues([], "");
    setStatus("変更を取り消しました", "");
    syncButtons();
  }

  tabs.forEach(function (tab) {
    tab.addEventListener("click", function () {
      showTab(tab.dataset.tab);
    });
  });
  buttons.check.addEventListener("click", check);
  buttons.save.addEventListener("click", save);
  buttons.discard.addEventListener("click", discard);
  buttons.reset.addEventListener("click", reset);
  window.addEventListener("beforeunload", function (ev) {
    if (!dirty) return;
    ev.preventDefault();
    ev.returnValue = "";
  });

  showTab((window.location.hash || "").replace("#", ""));
  load();
})();
