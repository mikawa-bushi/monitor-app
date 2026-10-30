// CSV のアップロード(管理の機能)。upload.html のフォームを POST /api/upload/{table}
// (multipart/form-data)で送り、取り込みの結果を表示する。
//
// 依存する DOM(upload.html):
//   #upload-form                  … フォーム本体(file・mode・force)
//   #u-table option[data-columns] … 取り込み先のテーブルと列
//   #u-columns                    … 選んだテーブルの列の表示先
//   #u-force-row / #u-force       … 置き換えを断ったときに出す「それでも置き換える」
//   #upload-result                … 結果の表示先
// 送信先の URL は MonitorCore.url で組む(サブパス配信対応。core.js が先に読み込まれる)。

(function () {
  "use strict";

  const form = document.getElementById("upload-form");
  if (!form) return;
  const Core = window.MonitorCore;
  const tableEl = document.getElementById("u-table");
  const columnsEl = document.getElementById("u-columns");
  const fileEl = document.getElementById("u-file");
  const forceRow = document.getElementById("u-force-row");
  const forceEl = document.getElementById("u-force");
  const resultEl = document.getElementById("upload-result");
  const submitBtn = form.querySelector('[type="submit"]');

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function showColumns() {
    const option = tableEl.options[tableEl.selectedIndex];
    columnsEl.textContent = option ? "列: " + option.dataset.columns : "";
  }

  function mode() {
    const checked = form.querySelector('input[name="mode"]:checked');
    return checked ? checked.value : "append";
  }

  function resetForce() {
    forceRow.hidden = true;
    forceEl.checked = false;
  }

  function show(kind, title, lines) {
    resultEl.hidden = false;
    resultEl.className = "upload-result " + kind;
    resultEl.replaceChildren();
    resultEl.appendChild(el("p", "upload-result__title", title));
    if (lines && lines.length) {
      const list = el("ul", "upload-result__list");
      lines.forEach(function (line) {
        list.appendChild(el("li", "", line));
      });
      resultEl.appendChild(list);
    }
  }

  function showLoginNeeded() {
    show("error", "ログインが必要です(管理の機能を使える鍵でログインしてください)");
    const link = el("a", "", "ログインする");
    link.href = Core.url("/login?next=/upload");
    resultEl.appendChild(link);
  }

  function summarize(result) {
    const lines = [];
    if (result.encoding) lines.push("文字コード: " + result.encoding);
    if (result.duplicates) {
      lines.push("主キーが既にある " + result.duplicates + " 行を読み飛ばしました");
    }
    if (result.skipped) lines.push("読めなかった・変換できなかった " + result.skipped + " 行を読み飛ばしました");
    const renamed = Object.keys(result.renamed || {});
    if (renamed.length) {
      lines.push(
        "見出しの表記ゆれを対応付けました: " +
          renamed.map((k) => k + " → " + result.renamed[k]).join("、")
      );
    }
    if ((result.missing || []).length) lines.push("見出しに無い列: " + result.missing.join("、"));
    if ((result.unknown || []).length) {
      lines.push("テーブルに無いため取り込まなかった列: " + result.unknown.join("、"));
    }
    return lines.concat(result.warnings || []);
  }

  tableEl.addEventListener("change", function () {
    showColumns();
    resetForce();
  });
  fileEl.addEventListener("change", resetForce);
  form.querySelectorAll('input[name="mode"]').forEach(function (radio) {
    radio.addEventListener("change", resetForce);
  });
  showColumns();

  form.addEventListener("submit", async function (ev) {
    ev.preventDefault();
    const table = tableEl.value;
    const file = fileEl.files && fileEl.files[0];
    if (!file) {
      show("error", "CSV ファイルを選んでください");
      fileEl.focus();
      return;
    }
    if (
      mode() === "replace" &&
      !window.confirm("テーブル「" + table + "」の行をすべて、CSV の行に置き換えます。よろしいですか?")
    ) {
      return;
    }
    const data = new FormData();
    data.append("file", file);
    data.append("mode", mode());
    data.append("force", forceEl.checked ? "true" : "false");
    submitBtn.disabled = true;
    show("pending", "取り込んでいます…");
    try {
      const res = await fetch(Core.url("/api/upload/" + encodeURIComponent(table)), {
        method: "POST",
        body: data,
      });
      const body = await res.json().catch(function () {
        return {};
      });
      if (res.status === 401) {
        showLoginNeeded();
        return;
      }
      if (!res.ok) {
        show("error", "取り込めませんでした: " + Core.errorMessage(body, res.status));
        return;
      }
      if (body.refused) {
        forceRow.hidden = false;
        show(
          "error",
          "置き換えませんでした(今の行はそのままです)",
          ["CSV の見出しに、テーブルの列が丸ごとありません。"].concat(summarize(body))
        );
        return;
      }
      resetForce();
      const what = body.mode === "replace" ? "置き換えました" : "追記しました";
      show("ok", body.file + " を「" + body.table + "」に " + body.inserted + " 行" + what, summarize(body));
      form.reset();
      tableEl.value = table;
      showColumns();
    } catch (e) {
      show("error", "通信エラーが発生しました");
    } finally {
      submitBtn.disabled = false;
    }
  });
})();
