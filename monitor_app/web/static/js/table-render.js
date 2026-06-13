// テーブル描画の共通部品(#20)。app.js と kiosk.js が共有する。
// window.MonitorTable として公開する。
//
// 契約(/api/views/<v> のペイロード):
//   columns: string[], column_labels: { <col>: string },
//   data: object[], cell_styles: { <col>: {greater_than|less_than|equal_to:{value,class},
//                                          width, font_size, align, bold} }
//
// セル整形: number かつ非整数のみ ja-JP ロケールで整形。整数・文字列・null は不変。
// セルスタイル: 条件クラス(greater/less/equal の先勝ち)+ width/font_size/align/bold。

(function () {
  "use strict";

  function formatCell(value) {
    if (typeof value === "number" && !Number.isInteger(value)) {
      return value.toLocaleString("ja-JP", { maximumFractionDigits: 2 });
    }
    return value === null || value === undefined ? "" : String(value);
  }

  function cellClass(value, rules) {
    if (!rules) return "";
    var num = parseFloat(value);
    if (rules.greater_than && num > rules.greater_than.value) return rules.greater_than.class;
    if (rules.less_than && num < rules.less_than.value) return rules.less_than.class;
    if (rules.equal_to && num === rules.equal_to.value) return rules.equal_to.class;
    return "";
  }

  function applyCellStyles(td, value, rules) {
    if (!rules) return;
    var cls = cellClass(value, rules);
    if (cls) td.className = cls;
    if (rules.width) td.style.width = rules.width;
    if (rules.font_size) td.style.fontSize = rules.font_size;
    if (rules.align) td.style.textAlign = rules.align;
    if (rules.bold) td.style.fontWeight = "bold";
  }

  function renderHead(theadRow, cols, labels) {
    labels = labels || {};
    theadRow.replaceChildren();
    cols.forEach(function (col) {
      var th = document.createElement("th");
      th.textContent = labels[col] || col; // labels に表示名(単位込み)があれば優先
      theadRow.appendChild(th);
    });
  }

  function renderBody(tbody, rows, cols, cellStyles, opts) {
    opts = opts || {};
    cellStyles = cellStyles || {};
    rows = rows || [];
    var frag = document.createDocumentFragment();
    if (rows.length === 0) {
      var tr = document.createElement("tr");
      var td = document.createElement("td");
      td.className = "empty";
      td.colSpan = cols.length || 1;
      td.textContent = opts.emptyText || "データがありません";
      tr.appendChild(td);
      frag.appendChild(tr);
    } else {
      rows.forEach(function (row) {
        var rowTr = document.createElement("tr");
        cols.forEach(function (col) {
          var cell = document.createElement("td");
          var value = row[col];
          cell.textContent = formatCell(value);
          applyCellStyles(cell, value, cellStyles[col]);
          rowTr.appendChild(cell);
        });
        frag.appendChild(rowTr);
      });
    }
    tbody.replaceChildren(frag);
  }

  window.MonitorTable = {
    formatCell: formatCell,
    cellClass: cellClass,
    applyCellStyles: applyCellStyles,
    renderHead: renderHead,
    renderBody: renderBody,
  };
})();
