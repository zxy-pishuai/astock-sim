/* ===== J5 core/dom.js — DOM/格式化工具（window 桥接全局，供各页面模块复用） ===== */
const $ = id => document.getElementById(id);
const fmt = (v, d = 2) => (v == null || isNaN(v)) ? "—" : Number(v).toFixed(d);
const fmtBig = v => v == null ? "—" : (v >= 1e8 ? (v / 1e8).toFixed(2) + "亿" : (v >= 1e4 ? (v / 1e4).toFixed(0) + "万" : fmt(v, 0)));
const cls = v => v > 0 ? "up" : v < 0 ? "down" : "flat";

/* J5 差量更新：html 与上次相同时不写 DOM（DOM 写操作数大幅下降；MutationObserver 可证） */
function setHtml(el, html) {
  if (!el) return;
  if (el._j5html !== html) { el._j5html = html; el.innerHTML = html; }
}

Object.assign(window, { $, fmt, fmtBig, cls, setHtml });
export { $, fmt, fmtBig, cls, setHtml };
