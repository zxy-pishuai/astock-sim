/* ===== J5 global：从 app.js 1824-2016 行切分（函数体逐字保留，window 桥接全局） ===== */
/* ============ ★ Phase19 全球市场 ============ */
let gmSparkCharts = {};   // sym -> echarts instance
const GM_CARD_CONF = {
  us: { idx: ["usDJI", "usIXIC", "usINX", "usVIX"],
        stocks: ["usNVDA", "usAAPL", "usTSLA", "usMSFT", "usGOOGL",
                 "usAMZN", "usTSM", "usMETA", "usASML"] },
  hk: { idx: ["hkHSI", "hkHSTECH", "hkHSCEI"],
        stocks: ["hk00700", "hk09988", "hk03690"] },
  jp: { idx: ["N225", "KS11"],
        stocks: ["jp7203", "jp6758", "kr000660", "kr005930"] },
  eu: { idx: ["DAX", "FTSE"], stocks: [] },
};
function startGlobalTimer() {
  clearTimers();
  if (!state.gmTimer) state.gmTimer = setInterval(() => {
    if (state.page === "global") refreshGlobal();   // 页隐藏时暂停
  }, 60000);
}
async function refreshGlobal(force) {
  try {
    const d = await fetchJson("/api/global/quotes");
    const s = await fetchJson("/api/global/summary");
    if (!d || d.error) { showGmDown("行情数据暂不可用"); return; }
    let mv = null;
    try { mv = await fetchJson("/api/global/movers"); } catch (e) { mv = null; }
    const q = d.quotes || {};
    renderGmEmotion(s);
    renderGmMovers(mv);
    for (const [m, conf] of Object.entries(GM_CARD_CONF)) renderGmCard(m, q, conf);
    renderGmFx(q);
    $("gmUpdated").textContent = "更新 " + (d.updated || s.updated || "");
  } catch (e) { showGmDown("行情数据暂不可用"); }
}
function showGmDown(msg) {
  // ★2026-09-11：源断开时保留"上次成功更新时间"，避免旧数据被误当实时
  const lastUpd = $("gmUpdated").textContent;
  $("gmUpdated").textContent = msg + (lastUpd && lastUpd.startsWith("更新 ") ? "（保留" + lastUpd.replace("更新 ", "") + "旧数据）" : "");
  ["us", "hk", "jp", "eu", "fx"].forEach(m => {
    const b = $("gmBody_" + m);
    if (b && !b.innerHTML.includes("数据暂不可用") && !b.innerHTML) b.innerHTML = '<div class="placeholder small">数据暂不可用</div>';
  });
}
function renderGmEmotion(s) {
  const up = s.markets_up != null ? s.markets_up : "—";
  const tot = s.total != null ? s.total : "—";
  $("gmUpN").textContent = up;
  $("gmTotN").textContent = tot;
  $("gmEmoStatus").textContent = "全球情绪 " + (up >= tot / 2 ? "偏强" : "偏弱") + "（更新 " + (s.updated || "") + "）";
  // VIX 徽章
  const vix = s.vix != null ? s.vix : null;
  const vixEl = $("gmVix");
  if (vix == null) { vixEl.textContent = "—"; vixEl.className = "vix-badge"; }
  else {
    vixEl.textContent = "VIX " + (typeof vix === "number" ? vix.toFixed(1) : vix);
    vixEl.className = "vix-badge " + (vix < 15 ? "vix-low" : vix <= 25 ? "vix-mid" : "vix-high");
  }
  $("gmStrong").textContent = s.strongest ? `${s.strongest.name} ${s.strongest.pct != null ? (s.strongest.pct > 0 ? "+" : "") + s.strongest.pct.toFixed(1) + "%" : ""}` : "—";
  $("gmWeak").textContent = s.weakest ? `${s.weakest.name} ${s.weakest.pct != null ? s.weakest.pct.toFixed(1) + "%" : ""}` : "—";
  // A股映射提示
  const hints = s.a_share_hints || [];
  const hc = $("gmHints");
  hc.innerHTML = hints.map(h =>
    `<span class="gm-hint ${h.dir}">${h.text}</span>`).join("") ||
    '<span class="tip">当前无 A股映射提示信号</span>';
}
function gmPctHtml(pct, cls) {
  const c = cls || "gm-pct";
  if (pct == null || isNaN(pct)) return `<span class="${c}">—</span>`;
  return `<span class="${c} ${pct > 0 ? "up" : pct < 0 ? "down" : ""}">${(pct > 0 ? "+" : "") + pct.toFixed(2)}%</span>`;
}
function fmtPriceCcy(it) {
  if (it.price == null) return "—";
  const n = Math.abs(it.price) >= 1000
    ? Number(it.price).toLocaleString("zh-CN", { maximumFractionDigits: 2 })
    : Number(it.price).toFixed(2);
  return it.currency ? `${n} ${it.currency}` : n;
}
function renderGmCard(market, q, conf) {
  const badge = $("gmBadge_" + market);
  const body = $("gmBody_" + market);
  const items = conf.idx.map(c => q[c]).filter(Boolean);
  const stocks = conf.stocks.map(c => q[c]).filter(Boolean);
  // 东财被阻断时若 Yahoo 备用源顶上 → 徽章显示 Yahoo备用源
  const useYahoo = items.some(it => it.source === "yahoo");
  const anyT = items.concat(stocks).some(it => it.trading);
  if (!items.length) {
    badge.textContent = "数据暂不可用";
    badge.className = "status-badge down";
    body.innerHTML = '<div class="placeholder small">数据暂不可用（数据源未接通）</div>';
    return;
  }
  badge.textContent = useYahoo ? "Yahoo备用源" : (anyT ? "交易中" : "休市");
  badge.className = "status-badge" + (anyT ? " live" : "") + (useYahoo ? " warn" : "");
  for (const it of items) {
    if (gmSparkCharts[it.code]) { gmSparkCharts[it.code].dispose(); delete gmSparkCharts[it.code]; }
  }
  body.innerHTML = items.map(it => `
    <div class="gm-idx">
      <span class="gm-name">${it.name || it.code}</span>
      <span class="gm-price">${fmtNum(it.price)}</span>
      ${gmPctHtml(it.pct)}
      <span class="gm-spark" id="spark_${it.code}"></span>
    </div>`).join("") || '<div class="placeholder small">数据暂不可用</div>';
  // 个股 chips：名称 + 涨跌幅（红涨绿跌）+ 价格（KRW/JPY 千分位+币种后缀）
  if (stocks.length) {
    body.innerHTML += `<div class="gm-chips">${
      stocks.map(c => `
        <span class="gm-chip">
          <span class="chip-n">${c.name || c.code}</span>
          ${gmPctHtml(c.pct, "chip-p")}
          <span class="chip-x">${fmtPriceCcy(c)}</span>
        </span>`).join("")
    }</div>`;
  }
  // sparkline（异步拉历史）
  items.forEach(it => loadSpark(it.code, "spark_" + it.code));
}
function renderGmMovers(mv) {
  const el = $("gmMovers");
  if (!el) return;
  if (!mv || mv.error || !mv.items || !mv.items.length) {
    el.innerHTML = '<span class="tip">🔥 外盘异动：暂无满足阈值的个股</span>';
    return;
  }
  const tag = mv.lowered
    ? `（不足3只，阈值降至${mv.threshold}%）`
    : `（|涨跌幅|≥${mv.threshold}%）`;
  el.innerHTML = `<span class="gm-movers-t">🔥 外盘异动${tag}</span>` +
    mv.items.map(it => {
      const mkt = ({ us: "美", hk: "港", jp: "日", kr: "韩", eu: "欧" })[it.market] || "";
      return `<span class="gm-mover ${it.pct > 0 ? "up" : "down"}">` +
        `${mkt ? `<i>${mkt}</i>` : ""}<b>${it.name}</b>` +
        `<span>${(it.pct > 0 ? "+" : "") + it.pct.toFixed(2)}%</span></span>`;
    }).join("");
}
function renderGmFx(q) {
  const fxCodes = ["USDCNH", "GC", "CL", "DINIW"];
  const body = $("gmBody_fx");
  const items = fxCodes.map(c => q[c]).filter(Boolean);
  const anyT = items.some(it => it.trading);
  $("gmBadge_fx").textContent = anyT ? "交易中" : "休市";
  $("gmBadge_fx").className = "status-badge" + (anyT ? " live" : "");
  body.innerHTML = items.map(it => `
    <div class="gm-idx">
      <span class="gm-name">${it.name || it.code}</span>
      <span class="gm-price">${fmtNum(it.price)}</span>
      ${gmPctHtml(it.pct)}
    </div>`).join("") || '<div class="placeholder small">数据暂不可用</div>';
}
function fmtNum(v) {
  if (v == null) return "—";
  if (Math.abs(v) >= 1000) return Number(v).toLocaleString("zh-CN", { maximumFractionDigits: 2 });
  return Number(v).toFixed(2);
}
async function loadSpark(sym, elId) {
  const el = $(elId);
  if (!el) return;
  try {
    const d = await fetchJson(`/api/global/history?sym=${sym}&days=60`);
    const rows = (d.rows || []).map(r => r.close).filter(v => v != null);
    if (rows.length < 2) { el.innerHTML = ""; return; }
    if (!echarts) return;
    const inst = echarts.init(el);
    gmSparkCharts[sym] = inst;
    inst.setOption({
      grid: { left: 2, right: 2, top: 2, bottom: 2 },
      xAxis: { type: "category", show: false, data: rows.map((_, i) => i) },
      yAxis: { type: "value", show: false, scale: true },
      series: [{
        type: "line", data: rows, smooth: true, symbol: "none",
        lineStyle: { color: rows[rows.length - 1] >= rows[0] ? "#e6484d" : "#1e8e5a", width: 1.2 },
      }],
      animation: false,
    });
  } catch (e) { el.innerHTML = ""; }
}
async function loadGlobalNews(force) {
  const el = $("gmNewsList");
  if (!el) return;
  // ★ K9：加载中骨架屏；pending 占位由 fetchJson 自动重试
  uiShowSkeleton(el);
  try {
    const d = await fetchJson("/api/news/premarket?scope=global&min_score=0");
    uiClearSkeleton(el);
    const items = (d.items || []).filter(x => x.market !== "其他") || [];
    el.innerHTML = items.slice(0, 8).map(it => `
      <div class="gm-news-item">
        <span class="gm-nt">${it.time || ""}</span>${it.title}
        <span class="gm-mk">#${(it.markets || [it.market]).join(" #")}</span>
      </div>`).join("") || '<div class="placeholder small">暂无全球市场相关新闻</div>';
  } catch (e) {
    uiClearSkeleton(el);
    el.innerHTML = '<div class="placeholder small">新闻数据暂不可用</div>';
    // ★ K9：重试耗尽 → err-banner + 点此重试，不留空白
    uiShowErrBanner(el, () => loadGlobalNews(force));
  }
}
// 全局新闻跟随 refreshGlobal 一并加载（页面激活时）
function refreshGlobalAll(force) {
  refreshGlobal(force);
  if (state.page === "global") loadGlobalNews(force);
}

Object.assign(window, { startGlobalTimer, refreshGlobal, refreshGlobalAll, showGmDown, renderGmEmotion, gmPctHtml, fmtPriceCcy, renderGmCard, renderGmMovers, renderGmFx, fmtNum, loadSpark, loadGlobalNews });
