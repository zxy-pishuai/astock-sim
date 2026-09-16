/* ===== J5 core/api.js — fetchJson：30s 超时 + 全局 abort 池（页面隐藏时统一取消） =====
 * ★ K9（2026-09-16）：pending 占位自动重试 + 加载态辅助。
 * 服务端 _cached_api 冷路径返回 {pending:true, retry_after_ms:N}（单飞刷新中）→
 * 本层自动按 N 重试（上限 5 次 / 总预算 30s），期间不覆盖已有数据；
 * 重试耗尽抛 pendingTimeout 错误 → 页面用 uiShowErrBanner 显示 .err-banner + "点此重试"，不留空白页。
 */
window.__aborts = window.__aborts || new Map();

const FETCH_PENDING_MAX = 5;          // pending 重试上限
const FETCH_PENDING_TOTAL_MS = 30000; // 总重试预算
const FETCH_REQUEST_TIMEOUT_MS = 15000; // 单次请求超时（防止一次请求吃掉全部预算）

function _sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

async function fetchJson(url, opts) {
  let tries = 0;
  const started = Date.now();
  for (;;) {
    const budgetLeft = FETCH_PENDING_TOTAL_MS - (Date.now() - started);
    const ac = new AbortController();
    const timer = setTimeout(() => ac.abort(), Math.min(FETCH_REQUEST_TIMEOUT_MS, Math.max(1000, budgetLeft)));
    window.__aborts.set(url + (opts && opts.method || "GET"), ac);
    try {
      const r = await fetch(url, Object.assign({}, opts, { signal: ac.signal }));
      if (!r.ok) throw new Error("HTTP " + r.status);
      const j = await r.json();
      if (j && j.pending === true) {
        tries += 1;
        if (tries >= FETCH_PENDING_MAX || Date.now() - started >= FETCH_PENDING_TOTAL_MS) {
          const e = new Error("PENDING_TIMEOUT " + url);
          e.pendingTimeout = true;
          e.url = url;
          throw e;
        }
        const wait = (j.retry_after_ms && j.retry_after_ms > 0) ? j.retry_after_ms : 3000;
        await _sleep(Math.min(wait, 5000));
        continue; // 重试（期间已有数据不被覆盖——页面渲染逻辑只在拿到真数据后写 DOM）
      }
      return j;
    } finally {
      clearTimeout(timer);
      window.__aborts.delete(url + (opts && opts.method || "GET"));
    }
  }
}

/* ---- K9：加载态辅助（.skeleton / .err-banner 样式由 J5 提供于 app.css） ---- */
function uiShowSkeleton(el) { if (el) el.classList.add("skeleton"); }
function uiClearSkeleton(el) { if (el) el.classList.remove("skeleton"); }

function uiShowErrBanner(anchor, retryFn) {
  if (!anchor) return;
  uiClearSkeleton(anchor);
  const bid = "errBanner-" + (anchor.id || "x");
  const old = document.getElementById(bid);
  if (old) old.remove();
  const b = document.createElement("div");
  b.className = "err-banner";
  b.id = bid;
  b.style.cssText = "display:flex;align-items:center;justify-content:space-between;gap:8px;";
  const span = document.createElement("span");
  span.textContent = "加载失败：服务繁忙或暂不可用（已自动重试）";
  const btn = document.createElement("button");
  btn.className = "btn";
  btn.type = "button";
  btn.textContent = "点此重试";
  btn.addEventListener("click", function () { b.remove(); if (retryFn) retryFn(); });
  b.appendChild(span);
  b.appendChild(btn);
  anchor.parentNode && anchor.parentNode.insertBefore(b, anchor);
}

async function loadWithPlaceholder(el, loader) {
  /* loader: () => Promise<data>；el 为骨架/错误横幅锚点（容器或表体）。
     骨架屏 → 数据；失败 → .err-banner + 点此重试；返回 data 或 null。 */
  uiShowSkeleton(el);
  try {
    const d = await loader();
    uiClearSkeleton(el);
    return d;
  } catch (e) {
    uiClearSkeleton(el);
    uiShowErrBanner(el, function () { loadWithPlaceholder(el, loader); });
    return null;
  }
}

Object.assign(window, { fetchJson, uiShowSkeleton, uiClearSkeleton, uiShowErrBanner, loadWithPlaceholder });
export { fetchJson, uiShowSkeleton, uiClearSkeleton, uiShowErrBanner, loadWithPlaceholder };
