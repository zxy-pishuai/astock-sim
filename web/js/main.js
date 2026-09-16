/* ===== 天玑量化终端 TianjiQuant — J5 模块化入口（main.js） =====
 * 加载顺序约定：core（挂 window 全局）→ pages（顶层绑定依赖 window.$/fetchJson 等）→ 本文件启动逻辑。
 * 回退：index.html 注释 module 引用、改回 <script src="js/app.legacy.js">。
 */
"use strict";

/* ---------- ★ 4.5 启动动画（自 app.js 逐字保留） ---------- */
(function () {
  const fill = document.getElementById("splashFill");
  const splash = document.getElementById("splash");
  if (!fill || !splash) return;
  let p = 0;
  const iv = setInterval(() => {
    p = Math.min(92, p + 4 + Math.random() * 10);   // 先冲到 92%，等真实数据
    fill.style.width = p + "%";
  }, 120);
  // 页面数据就绪后淡出闪屏（DOMContentLoaded 后 800ms + 数据加载）
  window.addEventListener("load", () => {
    setTimeout(() => {
      clearInterval(iv);
      fill.style.width = "100%";
      setTimeout(() => splash.classList.add("hide"), 350);
      setTimeout(() => splash.remove(), 900);
    }, 500);
  });
  // 兜底：3.5s 内必消失（防网络卡死导致闪屏永驻）
  setTimeout(() => {
    clearInterval(iv);
    fill.style.width = "100%";
    splash.classList.add("hide");
    setTimeout(() => splash.remove(), 500);
  }, 3500);
})();

/* ---------- core（先挂 window 全局） ---------- */
import "./core/dom.js";
import "./core/api.js";
import "./core/state.js";
import "./core/timer.js";

/* ---------- 页面模块（顶层绑定使用 window.$ / window.fetchJson / window.state） ---------- */
import "./pages/market.js";
import "./pages/trading.js";
import "./pages/chart.js";
import "./pages/portfolio.js";
import "./pages/backtest.js";
import "./pages/factor.js";
import "./pages/audit.js";
import "./pages/settings.js";
import "./pages/global.js";
import "./pages/sflow.js";
import "./pages/pnews.js";
import "./pages/tactics.js";
import "./pages/datahealth.js";

/* ============ 顶部：时钟 / 指数 / 市场环境 ============ */
function tickClock() {
  const d = new Date();
  $("clock").textContent = d.toLocaleTimeString("zh-CN", { hour12: false });
}
setInterval(tickClock, 1000); tickClock();

async function refreshOverview() {
  try {
    const o = await fetchJson("/api/overview");
    // ★ D1（2026-09-13）：quality_alert 红条（textContent 防 XSS；无内容隐藏）
    const qn = $("qa-notice");
    if (qn) {
      const bt = (o.quality && o.quality.banner_text) || "";
      if (bt) { qn.style.display = ""; qn.textContent = bt; }
      else { qn.style.display = "none"; }
    }
    const strip = $("indexStrip");
    strip.innerHTML = o.indices.map(i => `
      <div class="idx">
        <span class="i-name">${i.name}</span>
        <span class="i-val ${cls(i.pct_chg)}">${fmt(i.price)}</span>
        <span class="i-pct ${cls(i.pct_chg)}">${i.pct_chg > 0 ? "+" : ""}${fmt(i.pct_chg, 2)}%</span>
      </div>`).join("");
    const badge = $("regimeBadge");
    const map = { "强势": "strong", "中性": "mid", "弱势(抱团)": "weak", "崩溃(防守)": "crash" };
    badge.className = "regime-badge " + (map[o.regime] || "");
    badge.textContent = `${o.regime} · 上涨${(o.breadth * 100).toFixed(0)}% · ${o.max_pos}仓`;
    $("sbLast").textContent = "最后刷新: " + o.time;
    return o;
  } catch (e) { return null; }
}

/* ============ 导航 ============ */
function switchPage(page) {
  state.page = page;
  document.querySelectorAll(".nav-item").forEach(el =>
    el.classList.toggle("active", el.dataset.page === page));
  document.querySelectorAll(".page").forEach(el => el.style.display = "none");
  $("page-" + page).style.display = "";
  clearTimers();
  if (window.stopDepth) stopDepth(); if (window.stopChartDepth) stopChartDepth();   // ★D-S2/D-S4
  if (page === "market") { refreshMarket(); startMarketTimer(); loadWatchStrip(); loadAlerts(); }
  if (page === "trading") { refreshTrading(); startTrTimer(); if (window.startDepthTimer) startDepthTimer(); }
  if (page === "chart") { loadChart(); if (window.startChartDepth) startChartDepth(state.code); }
  if (page === "portfolio") { refreshPortfolio(); startPosTimer(); }
  if (page === "backtest") { initBacktestDates(); }
  if (page === "factor") { loadFactors(); }
  if (page === "sflow") { initSectorFlow(); startSflowTimer(); }
  if (page === "pnews") { initPnews(); startPnewsTimer(); }
  if (page === "global") { refreshGlobalAll(); startGlobalTimer(); }
  if (page === "audit") { refreshAudit(); }
  if (page === "settings") { loadSettings(); }
  if (page === "tactics") { loadTactics(); startTacticsTimer(); }
  if (page === "datahealth") { refreshDataHealth(); startHealthTimer(); }
}
function clearTimers() {
  if (window.stopDepth) stopDepth();   // ★D-S2 十档盘口轮询
  [state.liveTimer, state.marketTimer, state.posTimer, state.idxTimer, state.trTimer, state.sflowTimer, state.pnewsTimer, state.gmTimer, state.tctTimer, state.healthTimer, state.pnBriefTimer].forEach(t => {
    if (t) clearInterval(t);
  });
  state.liveTimer = state.marketTimer = state.posTimer = state.trTimer = state.sflowTimer = state.pnewsTimer = state.gmTimer = state.tctTimer = state.healthTimer = state.pnBriefTimer = null;
}
function startHealthTimer() {
  if (!state.healthTimer) state.healthTimer = setInterval(refreshDataHealth, 60000);
}
/* J5：ES module 顶层函数默认不暴露到全局——pages/*.js 的窗口桥接引用了这两个，必须显式挂载 */
Object.assign(window, { switchPage, clearTimers, startHealthTimer });
document.querySelectorAll(".nav-item").forEach(el =>
  el.addEventListener("click", () => switchPage(el.dataset.page)));

/* ============ 全局刷新按钮 ============ */
$("btnRefresh").addEventListener("click", () => {
  state.klineCache = {};
  refreshOverview();
  if (state.page === "market") refreshMarket();
  if (state.page === "chart") renderChart(true);
  if (state.page === "portfolio") refreshPortfolio();
  $("sbMsg").textContent = "已手动刷新";
  setTimeout(() => { $("sbMsg").textContent = "就绪"; }, 2000);
});

/* ============ 启动 ============ */
(async function boot() {
  document.querySelectorAll(".nav-item").forEach(el =>
    el.addEventListener("click", () => switchPage(el.dataset.page)));
  await refreshOverview();
  state.idxTimer = setInterval(refreshOverview, 8000);
  // ★ 4.6 板块资金流：Tab 切换
  document.querySelectorAll("#sflowTabs .tab").forEach(el =>
    el.addEventListener("click", () => {
      document.querySelectorAll("#sflowTabs .tab").forEach(t => t.classList.remove("active"));
      el.classList.add("active");
      state.sflowType = el.dataset.type;
      initSectorFlow();
    }));
  document.querySelectorAll("#pnScopeTabs .tab").forEach(el =>
    el.addEventListener("click", () => {
      document.querySelectorAll("#pnScopeTabs .tab").forEach(t => t.classList.remove("active"));
      el.classList.add("active");
      state.pnewsScope = el.dataset.scope;
      refreshPnews();
    }));
  document.querySelectorAll("#pnMinTabs .tab").forEach(el =>
    el.addEventListener("click", () => {
      document.querySelectorAll("#pnMinTabs .tab").forEach(t => t.classList.remove("active"));
      el.classList.add("active");
      state.pnewsMin = parseInt(el.dataset.min || "2", 10);
      refreshPnews();
    }));
  const pnBtn = $("pnRefresh");
  if (pnBtn) pnBtn.addEventListener("click", () => refreshPnews(true));
  const gmBtn = $("gmRefresh");
  if (gmBtn) gmBtn.addEventListener("click", () => refreshGlobal(true));
  const gmNw = $("gmNewsRefresh");
  if (gmNw) gmNw.addEventListener("click", () => loadGlobalNews(true));
  switchPage("market");
})();

/* ============ J5：页面不可见 → 暂停全部轮询 + 取消在途请求；恢复可见 → 立即重启该页 ============ */
document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    clearTimers();
    if (window.__aborts) window.__aborts.forEach(ac => ac.abort());
  } else {
    if (!state.idxTimer) state.idxTimer = setInterval(refreshOverview, 8000);
    switchPage(state.page);
  }
});
