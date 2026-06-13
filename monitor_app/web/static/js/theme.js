/* theme.js — ダークテーマ管理 (IIFE + ES5 風 var JS)
 *
 * 契約(U00 §3):
 *   - localStorage キー: "monitor-theme"  ("dark" | "light")
 *   - data-theme 属性:   <html data-theme="dark"> で切替
 *   - CustomEvent:        "monitor:themechange"
 *   - 公開 API:          window.MonitorTheme.toggle()
 *
 * <head> 内に同期読み込みすることで FOUC を防ぐ。
 */
(function () {
  "use strict";

  var STORAGE_KEY = "monitor-theme";

  function getInitialTheme() {
    var stored = null;
    try {
      stored = localStorage.getItem(STORAGE_KEY);
    } catch (e) {
      // localStorage が使えない環境
    }
    if (stored === "dark" || stored === "light") {
      return stored;
    }
    // prefers-color-scheme をフォールバックにする
    if (
      window.matchMedia &&
      window.matchMedia("(prefers-color-scheme: dark)").matches
    ) {
      return "dark";
    }
    return "light";
  }

  function applyTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
  }

  function saveTheme(theme) {
    try {
      localStorage.setItem(STORAGE_KEY, theme);
    } catch (e) {
      // localStorage が使えない環境
    }
  }

  function currentTheme() {
    return document.documentElement.getAttribute("data-theme") || "light";
  }

  function toggle() {
    var next = currentTheme() === "dark" ? "light" : "dark";
    applyTheme(next);
    saveTheme(next);
    document.dispatchEvent(new CustomEvent("monitor:themechange", { detail: { theme: next } }));
    // テーマトグルボタンのアイコンを更新
    var btn = document.getElementById("theme-toggle");
    if (btn) {
      btn.textContent = next === "dark" ? "☀️" : "🌙";
    }
  }

  // 即時適用(FOUC防止)
  var initial = getInitialTheme();
  applyTheme(initial);

  // 公開 API
  window.MonitorTheme = {
    toggle: toggle,
    current: currentTheme,
  };
})();
