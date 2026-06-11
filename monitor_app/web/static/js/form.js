// 作業者入力フォームの送信(フェーズ3・F)。
// フォームの入力値を集めて POST /api/tables/{table} し、成功したらクリアして
// 連続入力できるようにする。
//
// 依存する DOM(form.html):
//   #entry-form[data-table]  … フォーム本体
//   #form-message            … 結果メッセージ

(function () {
  "use strict";

  const form = document.getElementById("entry-form");
  if (!form) return;
  const table = form.dataset.table;
  const messageEl = document.getElementById("form-message");

  function showMessage(text, ok) {
    messageEl.hidden = false;
    messageEl.textContent = text;
    messageEl.className = "form-message " + (ok ? "ok" : "error");
  }

  function collect() {
    const data = {};
    Array.prototype.forEach.call(form.elements, function (el) {
      if (!el.name) return;
      if (el.type === "checkbox") {
        data[el.name] = el.checked;
      } else if (el.value !== "") {
        data[el.name] = el.value;
      }
    });
    return data;
  }

  form.addEventListener("submit", async function (ev) {
    ev.preventDefault();
    const payload = collect();
    try {
      const res = await fetch("/api/tables/" + encodeURIComponent(table), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (res.status === 201) {
        showMessage("登録しました", true);
        form.reset();
        const first = form.querySelector("input, select");
        if (first) first.focus();
      } else {
        const body = await res.json().catch(function () { return {}; });
        const detail = body.detail && body.detail.message ? body.detail.message : "失敗しました";
        showMessage("エラー: " + detail, false);
      }
    } catch (e) {
      showMessage("通信エラーが発生しました", false);
    }
  });
})();
