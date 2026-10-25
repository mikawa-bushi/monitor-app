// ページ内の文字と背景のコントラスト比を getComputedStyle から測る(test_browser_ui.py が使う)。
// 半透明の背景は祖先の背景に重ねて合成する(背景画像は無視)。WCAG AA: 通常 4.5、大きな文字 3.0。
(() => {
  const parse = (c) => {
    const m = c.match(/rgba?\(([^)]+)\)/);
    if (!m) return [255, 255, 255, 1];
    const p = m[1].split(/[ ,/]+/).filter(Boolean).map(Number);
    return [p[0], p[1], p[2], p.length > 3 ? p[3] : 1];
  };
  const lum = ([r, g, b]) => {
    const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
  };
  const blend = (top, bottom) => {
    const a = top[3];
    return [0, 1, 2].map((i) => top[i] * a + bottom[i] * (1 - a)).concat(1);
  };
  const bgOf = (el) => {
    const layers = [];
    for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
      const c = parse(getComputedStyle(n).backgroundColor);
      if (c[3] > 0) { layers.push(c); if (c[3] >= 1) break; }
    }
    let base = [255, 255, 255, 1];
    if (layers.length && layers[layers.length - 1][3] >= 1) base = layers.pop();
    for (let i = layers.length - 1; i >= 0; i--) base = blend(layers[i], base);
    return base;
  };
  const failing = [];
  let checked = 0;
  document.querySelectorAll("body *").forEach((el) => {
    if (![...el.childNodes].some((n) => n.nodeType === 3 && n.textContent.trim())) return;
    const cs = getComputedStyle(el);
    if (cs.visibility === "hidden" || cs.display === "none" || !el.getClientRects().length) return;
    if (el.closest("[hidden]")) return;
    const bg = bgOf(el);
    let fg = parse(cs.color);
    if (fg[3] < 1) fg = blend(fg, bg);
    const [hi, lo] = [lum(fg), lum(bg)].sort((a, b) => b - a);
    const ratio = (hi + 0.05) / (lo + 0.05);
    const size = parseFloat(cs.fontSize);
    const large = size >= 24 || (size >= 18.66 && (parseInt(cs.fontWeight, 10) || 400) >= 700);
    checked += 1;
    if (ratio < (large ? 3 : 4.5)) {
      failing.push(`${el.tagName.toLowerCase()}.${el.className} "${el.textContent.trim().slice(0, 20)}" ${ratio.toFixed(2)}`);
    }
  });
  return { theme: document.documentElement.getAttribute("data-theme"), checked, failing };
})()
