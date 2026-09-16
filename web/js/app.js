/* ===== 天玑量化终端 TianjiQuant — 应用逻辑 ===== */
"use strict";

/* ---------- ★ 4.5 启动动画 ---------- */
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

/* ---------- 基础 ---------- */
const $ = id => document.getElementById(id);
const fmt = (v, d = 2) => (v == null || isNaN(v)) ? "—" : Number(v).toFixed(d);
const fmtBig = v => v == null ? "—" : (v >= 1e8 ? (v / 1e8).toFixed(2) + "亿" : (v >= 1e4 ? (v / 1e4).toFixed(0) + "万" : fmt(v, 0)));
const cls = v => v > 0 ? "up" : v < 0 ? "down" : "flat";

async function fetchJson(url, opts) {
  const r = await fetch(url, opts);
  if (!r.ok) throw new Error("HTTP " + r.status);
  return r.json();
}

const state = {
  page: "market",
  code: "600519",
  period: "day",
  marketPage: 1,
  marketSize: 50,
  marketSort: "amount",
  marketKw: "",
  watchlist: [],
  liveTimer: null,
  marketTimer: null,
  posTimer: null,
  idxTimer: null,
  trTimer: null,
  healthTimer: null,
  klineCache: {},   // key -> {ts, data}
  kchart: null,
  mchart: null,
  chartMode: "kline",  // kline | minute
};

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
  if (page === "market") { refreshMarket(); startMarketTimer(); loadWatchStrip(); loadAlerts(); }
  if (page === "trading") { refreshTrading(); startTrTimer(); }
  if (page === "chart") { loadChart(); }
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
  [state.liveTimer, state.marketTimer, state.posTimer, state.idxTimer, state.trTimer, state.sflowTimer, state.pnewsTimer, state.gmTimer, state.tctTimer, state.healthTimer].forEach(t => {
    if (t) clearInterval(t);
  });
  state.liveTimer = state.marketTimer = state.posTimer = state.trTimer = state.sflowTimer = state.pnewsTimer = state.gmTimer = state.tctTimer = state.healthTimer = null;
}
function startHealthTimer() {
  if (!state.healthTimer) state.healthTimer = setInterval(refreshDataHealth, 60000);
}
document.querySelectorAll(".nav-item").forEach(el =>
  el.addEventListener("click", () => switchPage(el.dataset.page)));

/* ============ 实盘交易 ============ */
async function refreshTrading() {
  try {
    const s = await fetchJson("/api/trading/status");
    const pill = $("trState");
    if (s.running) {
      pill.className = "state-pill on";
      pill.textContent = s.auto ? "🔁 自动交易运行中" : "🔁 引擎运行中";
    } else {
      pill.className = "state-pill off";
      pill.textContent = "引擎未启动";
    }
    // 市场环境
    const o = await fetchJson("/api/overview");
    const regIcons = { "强势": "🟢", "中性": "🟡", "弱势(抱团)": "🟠", "崩溃(防守)": "🔴" };
    $("trMarket").innerHTML = [
      ["市场环境", regIcons[o.regime] + " " + o.regime],
      ["上涨占比", (o.breadth * 100).toFixed(0) + "%"],
      ["允许仓位", o.max_pos + " 仓"],
      ["买入阈值", o.threshold + " 分"],
      ["持仓", s.positions + " 只"],
      ["现金", "¥" + fmt(s.cash, 0)],
    ].map(x => `<div><span>${x[0]}</span><b>${x[1]}</b></div>`).join("");
    // ★ 4.0 情绪周期仪表盘
    const se = s.sentiment;
    if (se) {
      const phIcons = { "冰点": "🧊", "发酵": "🌱", "高潮": "🔥", "退潮": "📉" };
      const yz = se.yesterday || {};
      const rows = [
        ["情绪相位", (phIcons[se.phase] || "🌡️") + " " + se.phase],
        ["情绪分", se.score + " / 100"],
        ["涨停", se.zt_count + " 家", se.zt_count > 50 ? "up" : ""],
        ["跌停", se.dt_count + " 家", se.dt_count > 0 ? "down" : ""],
        ["空间板", se.max_days + " 板"],
        ["炸板率", (se.zhaban_rate * 100).toFixed(0) + "%", se.zhaban_rate > 0.5 ? "down" : ""],
        ["昨涨停溢价", yz.avg_ret_pct != null ? (yz.avg_ret_pct > 0 ? "+" : "") + yz.avg_ret_pct + "%" : "—", yz.avg_ret > 0 ? "up" : "down"],
        ["赚钱效应", yz.up_ratio != null ? (yz.up_ratio * 100).toFixed(0) + "%" : "—"],
        ["开仓", se.open ? "✅ 允许" : "🚫 禁止", se.open ? "up" : "down"],
        ["建议", se.action || "—"],
      ];
      $("trSentiment").innerHTML = rows.map(x =>
        `<div><span>${x[0]}</span><b class="${x[2] || ""}">${x[1]}</b></div>`).join("");
      // 连板梯队
      const ladder = se.ladder || {};
      const promo = se.promotion || {};
      const ladderHtml = Object.keys(ladder).sort((a, b) => b - a).map(n =>
        `<div class="audit-item">${n}板 ×${ladder[n]}${promo[n + "进" + (parseInt(n) + 1)] != null ? ` · 晋级${(promo[n + "进" + (parseInt(n) + 1)] * 100).toFixed(0)}%` : ""}</div>`).join("");
      $("trLadder").innerHTML = ladderHtml || '<div class="placeholder small">今日无涨停</div>';
      loadSentimentGate();   // Phase16：情绪仓位闸门卡片
      // 4.1 题材爆发度（涨停原因主题词）
      const themes = se.themes || se.sector_burst || {};
      const themeEntries = Object.entries(themes);
      $("trThemes").innerHTML = themeEntries.length
        ? themeEntries.slice(0, 8).map(([t, c]) =>
            `<div class="audit-item">🔥 ${t} <b style="color:var(--gold)">×${c}</b></div>`).join("")
        : '<div class="placeholder small">题材数据加载中…</div>';
      // ★ 4.5 新闻情绪（场外情绪维度，异步加载）
      loadNewsSentiment();
      // ★ 4.5 业绩预增榜（异步加载，不阻塞情绪面板）
      loadEarningsBoard();
    } else {
      $("trSentiment").innerHTML = '<div class="placeholder small">情绪数据加载中…</div>';
      $("trLadder").innerHTML = "";
      $("trThemes").innerHTML = "";
    }
    // 强势板块
    const sec = await fetchJson("/api/sectors");
    $("sectorTip").textContent = sec.map_ready ? "（真实板块映射已加载）" : "（映射加载中…）";
    $("trSectors").innerHTML = sec.top.map(x => `
      <div class="sector-item">
        <span class="s-name">${x.name}</span>
        <span class="s-count">${x.count}只</span>
        <span class="s-pct up">+${x.avg_pct}%</span>
        <span class="s-leader">龙头:${x.leader_name || "—"} ${x.leader_pct ? "(" + (x.leader_pct > 0 ? "+" : "") + x.leader_pct + "%)" : ""}</span>
      </div>`).join("") || '<div class="placeholder small">板块数据加载中…</div>';
    // 竞价公示
    const au = s.auction || {};
    if (au.results && au.results.length) {
      $("trAuction").innerHTML = `<div class="auction-head">${au.time} 共 ${au.results.length} 只候选（大盘竞价高开 ${((au.breadth || 0) * 100).toFixed(0)}%）</div>` +
        au.results.map((r, i) => `
        <div class="auction-item" data-code="${r.code}">
          <span class="a-rank">${i + 1}</span>
          <span class="a-name">${r.name}</span>
          <span class="a-code">${r.code}</span>
          <span class="a-pct up">${r.auction_pct > 0 ? "+" : ""}${r.auction_pct}%</span>
          <span class="a-score">${r.score}分</span>
          <span class="a-sec">${r.sector}</span>
        </div>`).join("");
      $("trAuction").querySelectorAll(".auction-item").forEach(el =>
        el.addEventListener("click", () => { state.code = el.dataset.code; switchPage("chart"); }));
    } else {
      $("trAuction").innerHTML = s.in_auction_time
        ? '<div class="placeholder small">⏳ 竞价窗口扫描中…</div>'
        : '<div class="placeholder small">等待 9:25 竞价窗口（启动引擎后自动扫描）</div>';
    }
    // ★ v3.4 两点半战法候选
    const tt = s.twothirty || {};
    if (tt.results && tt.results.length) {
      $("trTwothirty").innerHTML = `<div class="auction-head">${tt.time} ${tt.msg}</div>` +
        tt.results.map((r, i) => `
        <div class="auction-item" data-code="${r.code}">
          <span class="a-rank">${i + 1}</span>
          <span class="a-name">${r.name}</span>
          <span class="a-code">${r.code}</span>
          <span class="a-pct up">${r.pct > 0 ? "+" : ""}${fmt(r.pct, 1)}%</span>
          <span class="a-score">${r.score}分</span>
          <span class="a-sec" title="${(r.signals || []).join("、")}">${(r.signals || []).slice(0, 2).join("、")}</span>
        </div>`).join("");
      $("trTwothirty").querySelectorAll(".auction-item").forEach(el =>
        el.addEventListener("click", () => { state.code = el.dataset.code; switchPage("chart"); }));
    } else {
      $("trTwothirty").innerHTML = tt.time
        ? '<div class="placeholder small">当日已扫描，暂无候选（涨2~7% + MACD金叉 + 量比>1）</div>'
        : '<div class="placeholder small">14:20-14:35 自动扫描（启动引擎）；也可点下方手动扫描</div>';
    }
    // ★ v3.4 组合风控状态
    const rk = s.risk || {};
    const rkRows = [
      ["单票仓位上限", (rk.single_stock_pct || 0) * 100 + "%"],
      ["最大持仓数", rk.max_positions + " 只"],
      ["今日已买", rk.daily_buy_count + "/" + rk.daily_buy_count_limit + " 次"],
      ["今日买入额", "¥" + fmt(rk.daily_buy_amount || 0, 0) + " / " + fmtBig(rk.daily_buy_amount_limit || 0)],
      ["ST/退市", rk.st_blacklist ? "拦截" : "—"],
      ["冰点禁开仓", rk.market_freeze_regime || "—"],
      ["行业上限", (rk.sector_pct_limit || 0) * 100 + "%"],
      ["相关阈值", "|r|≥" + (rk.corr_alert_threshold || 0.8)],
    ];
    $("trRisk").innerHTML = rkRows.map(x => `<div><span>${x[0]}</span><b>${x[1]}</b></div>`).join("");
    // ★ v3.7 行业敞口
    const exp = rk.sector_exposure || {};
    const expKeys = Object.keys(exp);
    $("trSectorExp").innerHTML = expKeys.length
      ? expKeys.map(sec => {
          const d = exp[sec];
          return `<div class="sector-item ${d.over ? "over" : ""}">
            <span class="s-name">${sec}</span>
            <span class="s-count">${d.codes.length}只</span>
            <span class="s-pct ${d.pct > 0.5 ? "up" : ""}">${(d.pct * 100).toFixed(1)}%${d.over ? " ⚠超限" : ""}</span>
          </div>`;
        }).join("")
      : '<div class="placeholder small">当前空仓或无需监控</div>';
    // ★ v3.7 相关性告警
    const corr = rk.corr_alerts || [];
    $("trCorrAlerts").innerHTML = corr.length
      ? corr.map(a => `<div class="audit-item" style="color:var(--gold)">⚠️ ${a.msg}</div>`).join("")
      : '<div class="placeholder small">持仓间无高风险相关性</div>';
    // 扫描候选
    // ★ 4.6：环境分级提示（崩溃/弱势等禁买模式横幅醒目提示，扫描仍照常出候选）
    const regime = s.last_regime || "";
    const breadthTxt = s.last_breadth ? "市场上涨" + Math.round(s.last_breadth * 100) + "%" : "";
    let regHtml = "";
    if (regime.includes("崩溃")) {
      regHtml = `<span style="color:var(--gold);font-weight:bold"> ⚠️ 环境[${regime}]（${breadthTxt}）：禁自动买入，扫描仍运行，候选供观察</span>`;
    } else if (regime) {
      regHtml = `<span class="tip"> 环境[${regime}]（${breadthTxt}）</span>`;
    }
    $("trScanTime").innerHTML = (s.last_scan_time ? "上次扫描 " + s.last_scan_time : "未扫描") + " | " + (s.last_scan_msg || "") + regHtml;
    const tb = $("trScanTable").querySelector("tbody");
    tb.innerHTML = (s.last_scan || []).map((r, i) => `
      <tr data-code="${r.code}">
        <td>${i + 1}</td><td class="clickable">${r.code}</td><td>${r.name}</td>
        <td class="num">${fmt(r.price)}</td><td class="num ${cls(r.pct_chg)}">${r.pct_chg > 0 ? "+" : ""}${fmt(r.pct_chg, 1)}%</td>
        <td class="num gold">${r.score}分</td>
        <td class="sig-cell" title="${(r.signals || []).join("、")}">${(r.signals || []).slice(0, 3).join("、")}</td>
      </tr>`).join("");
    tb.querySelectorAll("tr").forEach(tr =>
      tr.addEventListener("click", () => { state.code = tr.dataset.code; switchPage("chart"); }));
    // 自选监控
    const wv = $("trWatch");
    if (s.watch_events && s.watch_events.length) {
      const icons = { "拉升": "🔥", "大跌": "🚨", "回落": "⚠️" };
      wv.innerHTML = s.watch_events.slice().reverse().map(e =>
        `<div class="ev ${e.level.toLowerCase()}"><span class="ev-t">${e.t}</span>${icons[e.kind] || ""} ${e.msg}</div>`).join("");
    } else {
      wv.innerHTML = '<div class="placeholder small">启动引擎后，自选股异动（拉升/大跌/冲高回落）实时显示于此</div>';
    }
    // 事件
    const ev = $("trEvents");
    ev.innerHTML = (s.events || []).slice().reverse().map(e =>
      `<div class="ev ${e.level.toLowerCase()}"><span class="ev-t">${e.t}</span>${e.msg}</div>`).join("") || '<div class="placeholder small">暂无事件</div>';
    ev.scrollTop = 0;
  } catch (e) { /* 静默 */ }
}
function startTrTimer() { state.trTimer = setInterval(refreshTrading, 3000); }

/* ★ Phase16 情绪仓位闸门卡片 */
async function loadSentimentGate() {
  try {
    const el = $("trGate");
    const tip = $("trGateTip");
    if (!el) return;
    const d = await fetchJson("/api/sentiment/gate");
    if (d.error) { el.innerHTML = '<div class="placeholder small">闸门数据不可用</div>'; return; }
    tip.textContent = d.enabled ? "已开启（回测结论：不建议开启，默认关）" : "默认关闭（回测结论见报告）";
    const phase = d.phase || "—";
    const mult = d.mult != null ? (d.mult * 100).toFixed(0) + "%" : "—";
    const lc = d.low_confidence ? "（⚠️低置信）" : "";
    // 当前状态行
    const statRows = Object.entries(d.stats || {}).map(([ph, st]) =>
      `<tr>
        <td class="num">${ph}</td>
        <td class="num">${st.samples}</td>
        <td class="num ${st.next5_avg != null && st.next5_avg > 0 ? "up" : "down"}">${st.next5_avg != null ? (st.next5_avg * 100).toFixed(2) + "%" : "—"}</td>
        <td class="num ${st.next1_avg != null && st.next1_avg > 0 ? "up" : "down"}">${st.next1_avg != null ? (st.next1_avg * 100).toFixed(2) + "%" : "—"}</td>
        <td class="num">${st.up1_ratio != null ? (st.up1_ratio * 100).toFixed(1) + "%" : "—"}</td>
        <td class="num">${(d.mult_table && d.mult_table[ph] != null) ? (d.mult_table[ph] * 100).toFixed(0) + "%" : "—"}${st.low_confidence ? " ⚠️" : ""}</td>
      </tr>`).join("");
    el.innerHTML =
      `<div class="kv-grid" style="margin-bottom:6px">
         <div><span>当前阶段</span><b class="${phase === "退潮" || phase === "冰点" ? "down" : "up"}">${phase}${lc}</b></div>
         <div><span>建议仓位乘数</span><b>${mult}</b></div>
         <div><span>开关</span><b>${d.enabled ? "ON" : "OFF"}</b></div>
       </div>
       <div class="table-wrap"><table class="grid" style="min-width:480px">
         <thead><tr><th>阶段</th><th class="num">样本</th><th class="num">5日收益</th><th class="num">次日收益</th><th class="num">次日上涨率</th><th class="num">乘数</th></tr></thead>
         <tbody>${statRows || '<tr><td colspan="6" class="placeholder small">暂无历史统计</td></tr>'}</tbody>
       </table></div>`;
  } catch (e) { /* 静默 */ }
}

/* ★ 4.5 新闻情绪（场外情绪维度）+ 资金聚焦（封板资金） */
async function loadNewsSentiment() {
  try {
    const d = await fetchJson("/api/news");
    const ns = d.sentiment || {};
    const el = $("trThemes");
    if (!el) return;
    const sCls = ns.trend === "偏多" ? "up" : (ns.trend === "偏空" ? "down" : "");
    const newsLine = `<div class="audit-item" style="border-top:1px dashed #1d2434;margin-top:4px;padding-top:4px">
      📰 新闻情绪：<b class="${sCls}">${ns.trend || "中性"}</b>
      <span class="tip">分${ns.score} · 利好${ns.bull_count}/利空${ns.bear_count}</span></div>`;
    const burstLine = (d.burst && Object.keys(d.burst).length)
      ? `<div class="audit-item">📰 新闻热点：${Object.entries(d.burst).slice(0, 4).map(([k, v]) => `${k}×${v}`).join("、")}</div>`
      : "";
    el.innerHTML += newsLine + burstLine;
    // ★ 4.5 资金聚焦（封板资金 = 主力方向）
    try {
      const lu = await fetchJson("/api/limitup");
      const ff = lu.fund_focus || {};
      if (ff.top_seals && ff.top_seals.length) {
        const seals = ff.top_seals.slice(0, 4).map(s =>
          `<span class="audit-item" style="display:inline-block;margin-right:6px">💎 ${s.name} <b style="color:var(--gold)">封${(s.fund / 1e7).toFixed(0)}千万</b>${s.days > 1 ? `(${s.days}板)` : ""}</span>`).join("");
        el.innerHTML += `<div class="audit-item" style="margin-top:2px">💎 封板资金聚焦：${seals}</div>`;
      }
    } catch (e) { /* 静默 */ }
  } catch (e) { /* 静默 */ }
}

/* ★ 4.5 业绩预增榜（PIT按公告日，最强题材催化之一） */
async function loadEarningsBoard() {
  try {
    const d = await fetchJson("/api/earnings?days=14");
    const board = d.board || [];
    const el = $("trEarnings");
    if (!el) return;
    el.innerHTML = board.length
      ? board.slice(0, 8).map(x => `
          <div class="audit-item earn-item" data-code="${x.code}">
            <span class="e-name">${x.name}</span>
            <span class="e-code">${x.code}</span>
            <b style="color:var(--up)">+${x.amp_lower != null ? x.amp_lower : "?"}%</b>
            <span class="e-date">${(x.notice_date || "").slice(5)}</span>
          </div>`).join("")
      : '<div class="placeholder small">近期无业绩预增公告</div>';
    el.querySelectorAll(".earn-item").forEach(elm =>
      elm.addEventListener("click", () => { state.code = elm.dataset.code; switchPage("chart"); }));
  } catch (e) { /* 静默 */ }
}

$("btnTrStart").addEventListener("click", async () => {
  await fetchJson("/api/trading/start", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ auto: $("trAuto").checked }) });
  refreshTrading();
});
$("btnTrStop").addEventListener("click", async () => {
  await fetchJson("/api/trading/stop", { method: "POST" });
  refreshTrading();
});
$("btnTrScan").addEventListener("click", async () => {
  $("trScanTime").textContent = "⏳ 扫描中…（全市场，约10-30秒）";
  await fetchJson("/api/trading/scan", { method: "POST" });
});
$("btnTtScan").addEventListener("click", async () => {
  $("ttTip").textContent = "⏳ 两点半扫描中…";
  await fetchJson("/api/trading/twothirty", { method: "POST" });
  setTimeout(() => { $("ttTip").textContent = "14:20-14:35 扫描：涨幅2~7% + MACD金叉 + 量比>1（次日收盘卖）"; }, 20000);
});

/* ============ ★ 4.5 AI 功能（Mimo V2.5） ============ */
function renderAiReport(el, d) {
  if (!el) return;
  const prov = d.provider === "llm" ? "🤖 Mimo" : (d.provider === "local" ? "📊 本地" : "⚠️");
  const txt = (d.report || "").replace(/\n/g, "<br>");
  el.innerHTML = `<div class="ai-badge">${prov}</div><div class="ai-text">${txt}</div>`;
}
$("btnAiMorning").addEventListener("click", async () => {
  const el = $("trAi");
  el.innerHTML = '<div class="placeholder small">🌅 AI 正在生成盘前晨报…（约10-30秒）</div>';
  try {
    renderAiReport(el, await fetchJson("/api/ai/morning"));
  } catch (e) { el.innerHTML = '<div class="placeholder small">晨报生成失败: ' + e.message + '</div>'; }
});
$("btnAiHealth").addEventListener("click", async () => {
  const el = $("trAi");
  el.innerHTML = '<div class="placeholder small">🩺 AI 正在体检持仓组合…（约10-30秒）</div>';
  try {
    renderAiReport(el, await fetchJson("/api/ai/health"));
  } catch (e) { el.innerHTML = '<div class="placeholder small">体检失败: ' + e.message + '</div>'; }
});
$("btnAiMeeting").addEventListener("click", async () => {
  const el = $("cAi");
  const code = state.code, name = $("cName").textContent || code;
  el.innerHTML = '<div class="placeholder small">🎓 AI 投研会议进行中…（约15-30秒）</div>';
  try {
    renderAiReport(el, await fetchJson(`/api/ai/meeting?code=${code}&name=${encodeURIComponent(name)}`));
  } catch (e) { el.innerHTML = '<div class="placeholder small">会议失败: ' + e.message + '</div>'; }
});
$("btnAiAsk").addEventListener("click", async () => {
  const code = state.code, name = $("cName").textContent || code;
  const q = prompt("🤖 问 AI（关于 " + name + "）：", "这只股票现在能买吗？有什么风险？");
  if (!q) return;
  const el = $("cAi");
  el.innerHTML = '<div class="placeholder small">💬 AI 分析中…（约10-30秒）</div>';
  try {
    renderAiReport(el, await fetchJson(`/api/ai/qa?code=${code}&q=${encodeURIComponent(q)}&name=${encodeURIComponent(name)}`));
  } catch (e) { el.innerHTML = '<div class="placeholder small">问诊失败: ' + e.message + '</div>'; }
});

/* ============ 行情中心 ============ */
async function refreshMarket() {
  const q = new URLSearchParams({
    page: state.marketPage, size: state.marketSize,
    sort: state.marketSort, kw: state.marketKw,
  });
  try {
    const d = await fetchJson("/api/stocklist?" + q);
    const tbody = $("marketTable").querySelector("tbody");
    tbody.innerHTML = d.rows.map(r => `
      <tr data-code="${r.code}" data-name="${r.name}">
        <td class="clickable">${r.code}</td>
        <td class="clickable">${r.name}</td>
        <td class="num">${fmt(r.price)}</td>
        <td class="num ${cls(r.pct_chg)}">${r.pct_chg > 0 ? "+" : ""}${fmt(r.pct_chg, 2)}%</td>
        <td class="num">${fmtBig(r.amount)}</td>
        <td class="num">${fmt(r.turnover, 2)}</td>
        <td class="num">${fmt(r.vol_ratio, 2)}</td>
      </tr>`).join("");
    $("pageInfo").textContent = `${d.page}/${Math.max(1, Math.ceil(d.total / d.size))}`;
    $("marketTotal").textContent = `共 ${d.total} 只`;
    tbody.querySelectorAll("tr").forEach(tr =>
      tr.addEventListener("click", () => {
        state.code = tr.dataset.code;
        switchPage("chart");
      }));
  } catch (e) { /* 静默 */ }
}
function startMarketTimer() {
  state.marketTimer = setInterval(refreshMarket, 30000);
}
$("marketSearch").addEventListener("input", e => {
  state.marketKw = e.target.value.trim(); state.marketPage = 1; refreshMarket();
});
$("marketSort").addEventListener("change", e => { state.marketSort = e.target.value; refreshMarket(); });
$("marketSize").addEventListener("change", e => { state.marketSize = +e.target.value; state.marketPage = 1; refreshMarket(); });
$("prevPage").addEventListener("click", () => { if (state.marketPage > 1) { state.marketPage--; refreshMarket(); } });
$("nextPage").addEventListener("click", () => { state.marketPage++; refreshMarket(); });

/* 自选条 */
async function loadWatchStrip() {
  try {
    const d = await fetchJson("/api/watchlist");
    state.watchlist = d.watchlist || [];
    const codes = state.watchlist.map(w => w.code);
    let quotes = {};
    if (codes.length) quotes = await fetchJson("/api/quotes?codes=" + codes.join(","));
    const strip = $("watchStrip");
    if (!state.watchlist.length) { strip.innerHTML = `<span class="tip">自选股为空 — 在行情表或K线页点「☆ 加自选」</span>`; return; }
    // ★ v3.9：按分组展示（分组标签 + 股票 chip）
    const groups = {};
    state.watchlist.forEach(w => {
      const g = w.group || "默认";
      (groups[g] = groups[g] || []).push(w);
    });
    let html = "";
    Object.keys(groups).forEach(g => {
      html += `<span class="wgroup">${g}</span>`;
      html += groups[g].map(w => {
        const q = quotes[w.code] || {};
        const pct = q.pct_chg || 0;
        return `<span class="wchip" data-code="${w.code}">
          <span class="c-code">${w.code}</span>${w.name || q.name || ""}
          <span class="c-price ${cls(pct)}">${q.price ? fmt(q.price) : "—"} ${pct ? (pct > 0 ? "+" : "") + fmt(pct, 1) + "%" : ""}</span>
          <span class="x" title="移除">✕</span></span>`;
      }).join("");
    });
    strip.innerHTML = html;
    strip.querySelectorAll(".wchip").forEach(chip => {
      chip.addEventListener("click", e => {
        if (e.target.classList.contains("x")) {
          fetchJson("/api/watchlist", { method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ action: "remove", code: chip.dataset.code }) }).then(loadWatchStrip);
          return;
        }
        state.code = chip.dataset.code; switchPage("chart");
      });
    });
  } catch (e) { /* 静默 */ }
}

/* ============ ★ 4.5 价格提醒（条件单） ============ */
async function loadAlerts() {
  try {
    const d = await fetchJson("/api/alerts");
    const alerts = d.alerts || [];
    const el = $("alertStrip");
    if (!alerts.length) {
      el.innerHTML = '<span class="tip">暂无提醒 — 点「+ 新增提醒」设置涨到/跌到条件（触发后微信+页面提醒）</span>';
      return;
    }
    el.innerHTML = alerts.map(a => {
      const dir = a.direction === "up" ? "📈 涨到" : "📉 跌到";
      const st = a.triggered ? "triggered" : (a.enabled ? "active" : "disabled");
      return `<span class="alert-chip ${st}" data-id="${a.id}">
        <span class="a-name">${a.name || a.code}</span>
        <span class="a-dir">${dir}</span>
        <b>${a.price}</b>
        ${a.triggered ? '<span class="a-trig">✅已触发</span>' : ""}
        <span class="x" title="删除">✕</span></span>`;
    }).join("");
    el.querySelectorAll(".alert-chip").forEach(chip => {
      chip.addEventListener("click", e => {
        if (e.target.classList.contains("x")) {
          fetchJson("/api/alerts", { method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ action: "remove", id: chip.dataset.id }) }).then(loadAlerts);
        }
      });
    });
  } catch (e) { /* 静默 */ }
}
$("btnAddAlert").addEventListener("click", async () => {
  const code = prompt("股票代码：", state.code || "600519");
  if (!code) return;
  const direction = prompt("提醒方向：\n1 = 涨到\n2 = 跌到", "1");
  if (!direction) return;
  const price = prompt("触发价格：", "");
  if (!price || isNaN(+price)) { alert("价格无效"); return; }
  // 获取股票名
  let name = "";
  try {
    const q = await fetchJson("/api/quotes?codes=" + code);
    name = (q[code] || {}).name || code;
  } catch (e) { name = code; }
  const dir = direction === "2" ? "down" : "up";
  await fetchJson("/api/alerts", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action: "add", code, name, direction: dir, price: +price }) });
  loadAlerts();
});
$("btnAlertRefresh").addEventListener("click", loadAlerts);
// 行情页刷新时同步加载提醒
const _origRefreshMarket = refreshMarket;
refreshMarket = function () { _origRefreshMarket(); loadAlerts(); };

/* ============ K线分析 ============ */
// ★ 4.5 修复：tab 命名 min5=分时(曲线), min5k=5分K线(蜡烛)，min15/30/60/day=蜡烛
const periodMap = { min5k: "min5", min15: "min15", min30: "min30", min60: "min60", day: "day" };

async function loadChart() {
  $("cCode").textContent = state.code;
  $("cName").textContent = "加载中…";
  // 周期 tab 高亮
  document.querySelectorAll("#periodTabs span").forEach(s =>
    s.classList.toggle("active", s.dataset.p === state.period));
  // ★ 4.5 修复：min5=分时曲线；其余(min5k/min15/30/60/day)=K线蜡烛
  state.chartMode = state.period === "min5" ? "minute" : "kline";
  $("optLive").checked = state.period !== "day";
  await renderChart();
  startLiveTimer();
}

let _chartSeq = 0;   // ★ 4.5 渲染版本号：防止异步 fetch 竞态（切tab/切股后旧响应覆盖新图）

async function renderChart(force) {
  const code = state.code;
  const mode = state.chartMode;
  const mySeq = ++_chartSeq;   // 本次渲染的令牌
  try {
    if (mode === "minute") {
      const d = await fetchJson("/api/minute?code=" + code);
      if (mySeq !== _chartSeq || state.chartMode !== "minute" || state.code !== code) return;  // 已被更新的渲染取代
      renderMinute(d);
      renderInfo(d.quote || {});
      if (d.quote && d.quote.name) $("cName").textContent = d.quote.name;
      return;
    }
    const period = periodMap[state.period] || "day";
    const key = code + ":" + period;
    const cached = state.klineCache[key];
    let data;
    if (cached && !force && Date.now() - cached.ts < 80000) {
      data = cached.data;
    } else {
      data = await fetchJson(`/api/kline?code=${code}&period=${period}&days=300`);
      state.klineCache[key] = { ts: Date.now(), data };
    }
    if (mySeq !== _chartSeq || state.chartMode !== "kline" || state.code !== code) return;  // 已被取代
    renderKline(data);
    const q = await fetchJson("/api/quotes?codes=" + code);
    if (mySeq !== _chartSeq || state.code !== code) return;
    const quote = q[code] || {};
    renderInfo(quote);
    if (quote.name) $("cName").textContent = quote.name;
    renderScore(code, quote);
    loadMoneyflow(code);   // ★ v3.8 资金面面板
  } catch (e) {
    if (mySeq === _chartSeq) $("cName").textContent = "数据加载失败: " + e.message;
  }
}

/* ★ v3.8 + 4.0 资金面面板（龙虎榜/两融/北向/主力资金流） */
async function loadMoneyflow(code) {
  try {
    const d = await fetchJson("/api/moneyflow?code=" + code);
    if (d.error) return;
    const lines = [];
    // 4.0 主力资金流（置顶）
    if (d.fflow) {
      const f = d.fflow;
      const amt = f.main_latest != null ? `${f.main_latest >= 0 ? "+" : ""}${fmtBig(Math.abs(f.main_latest))}` : "—";
      lines.push(`主力资金：${amt}（${f.desc || ""}）`);
    }
    const lhb = d.dragon_tiger || [];
    if (lhb.length) {
      const l = lhb[0];
      lines.push(`${l.date} 龙虎榜：净${l.net_amt >= 0 ? "买" : "卖"}${fmtBig(Math.abs(l.net_amt))}（${(l.reason || "").slice(0, 18)}）`);
    }
    // ★ 4.5 龙虎榜席位（机构/游资合力，比净买额更准）
    if (d.seat) {
      const s = d.seat;
      const parts = [];
      if (s.inst_net) parts.push(`机构净${s.inst_net > 0 ? "买" : "卖"}${fmtBig(Math.abs(s.inst_net))}`);
      if (s.hot_net) parts.push(`游资净${s.hot_net > 0 ? "买" : "卖"}${fmtBig(Math.abs(s.hot_net))}`);
      lines.push(`席位(${s.date})：${parts.join(" ") || "无机构/游资"} — ${s.verdict}`);
    }
    const m = d.margin || [];
    if (m.length) {
      lines.push(`${m[0].date} 融资余额 ${fmtBig(m[0].rzye)}${d.margin_change != null ? `（${d.margin_change > 0 ? "+" : ""}${(d.margin_change * 100).toFixed(1)}%）` : ""}`);
    }
    const n = d.northbound || [];
    if (n.length) {
      lines.push(`北向持股变动 ${d.northbound_change != null ? (d.northbound_change > 0 ? "+" : "") + (d.northbound_change * 100).toFixed(1) + "%" : "（季度数据）"}（${n[0].date}）`);
    }
    if (lines.length) {
      $("mfBox").style.display = "";
      $("mfSignals").innerHTML = lines.map(x => `<li>${x}</li>`).join("");
    } else {
      $("mfBox").style.display = "none";
    }
  } catch (e) { $("mfBox").style.display = "none"; }
}

function renderKline(d) {
  const k = d.klines || [];
  if (!k.length) return;
  const dates = k.map(x => x.date);
  const closes = k.map(x => x.close);
  const kdata = k.map(x => [x.open, x.close, x.low, x.high]);
  const ma = [];
  if ($("optMA").checked) {
    [5, 10, 20, 60].forEach(p => {
      if (k.length < p) return;
      ma.push({ name: "MA" + p, data: calcSMA(closes, p) });
    });
  }
  const boll = $("optBOLL").checked ? calcBOLL(closes, 20) : null;
  const vol = k.map(x => ({ v: x.volume, up: x.close >= x.open }));
  const sub = $("optSub").value;
  const subData = sub === "macd" ? calcMACD(closes) : { rsi: calcRSI(closes, 14) };
  const d2 = { dates, kdata, ma, boll, vol, sub, subData };
  // ★ 4.5 修复：K线与分时共用同一 DOM，切换时必须销毁对方实例，
  //   否则旧 echarts 实例挂在已被 innerHTML 清空的 DOM 上 → 崩/白屏
  if (state.mchart) { state.mchart.dispose(); state.mchart = null; }
  if (!state.kchart) {
    const dom = $("klineChart");
    dom.innerHTML = "";
    state.kchart = new KlineChart(dom);
  }
  state.kchart.render(d2);
}

// ★ 分时全时槽模板（240槽）：开盘初分时线从左侧延伸，不再占满全图
function minuteAxisTemplate(bars) {
  const has = new Set(bars.map(b => (b.date || "").slice(11, 16)));
  const startMin = has.has("09:30") ? 30 : 31;
  const times = [];
  const pushRange = (h0, m0, count) => {
    let h = h0, m = m0;
    for (let i = 0; i < count; i++) {
      times.push(String(h).padStart(2, "0") + ":" + String(m).padStart(2, "0"));
      m++; if (m === 60) { m = 0; h++; }
    }
  };
  pushRange(9, startMin, 120);
  pushRange(13, startMin - 30, 120);
  return times;
}

function renderMinute(d) {
  const bars = (d.bars || []).filter(b => b.close > 0);
  if (!bars.length) {
    // ★ 4.5 修复：无分时数据时清空图表并销毁实例，避免残留旧图
    if (state.kchart) { state.kchart.dispose(); state.kchart = null; }
    if (state.mchart) { state.mchart.dispose(); state.mchart = null; }
    const dom = $("klineChart");
    if (dom) dom.innerHTML = '<div class="placeholder small" style="padding:40px;text-align:center">分时数据加载中…（交易时段实时刷新）</div>';
    return;
  }
  // ★ 数据按时间落位到固定时槽，未来时槽留空
  const byTime = {};
  let cumAmt = 0, cumVol = 0;
  bars.forEach(b => {
    const t = (b.date || "").slice(11, 16);
    cumAmt += (b.amount || 0) || (b.close * b.volume);
    cumVol += b.volume;
    byTime[t] = { price: b.close, vol: b.volume, avg: cumVol > 0 ? +(cumAmt / cumVol).toFixed(2) : b.close };
  });
  const times = minuteAxisTemplate(bars);
  const prices = times.map(t => byTime[t] ? byTime[t].price : null);
  const vols = times.map(t => byTime[t] ? byTime[t].vol : null);
  const avgs = times.map(t => byTime[t] ? byTime[t].avg : null);
  const yest = (d.quote && d.quote.yest_close) || prices.find(p => p != null) || 0;
  // ★ 数据非今日（开盘前/节假日）时标注上一交易日
  const _now = new Date();
  const _todayStr = _now.getFullYear() + "-" + String(_now.getMonth() + 1).padStart(2, "0") + "-" + String(_now.getDate()).padStart(2, "0");
  const refLabel = (d.date && d.date !== _todayStr) ? "上一交易日分时 (" + d.date.slice(5) + ")" : "";
  // ★ 4.5 修复：切换分时前销毁 K线实例（共用 DOM，互斥）
  if (state.kchart) { state.kchart.dispose(); state.kchart = null; }
  if (!state.mchart) {
    const dom = $("klineChart");
    dom.innerHTML = "";
    state.mchart = new MinuteChart(dom);
  }
  state.mchart.render({ times, prices, avgs, vols, yest, refLabel });
  // ★ 4.4 分时量能标签（时段量比 / 冲高回落·追涨预警）
  const mvEl = $("cMinuteVol");
  if (mvEl) {
    const mv = d.minute_vol || {};
    if (mv.ratio) {
      let cls2 = "up";
      let tag = "📊 分时量";
      if (mv.ratio < 0.7) { cls2 = "down"; tag = "⚠ 量能不足"; }
      else if (mv.ratio >= 3.0) { cls2 = "warn"; tag = "⚠ 爆量分歧"; }
      else if (mv.ratio >= 1.5) { tag = "📊 放量确认"; }
      mvEl.style.display = "block";
      mvEl.innerHTML = `${tag}：时段量比 <b class="${cls2}">${mv.ratio}</b>（${mv.verdict || ""}）`;
    } else {
      mvEl.style.display = "none";
    }
  }
}

function renderInfo(q) {
  const pct = q.pct_chg || 0;
  $("cPrice").textContent = fmt(q.price);
  $("cPrice").className = "big-price " + cls(pct);
  $("cPct").textContent = (pct > 0 ? "+" : "") + fmt(pct, 2) + "%";
  $("cPct").className = "pct " + cls(pct);
  const rows = [
    ["今开", fmt(q.open)], ["昨收", fmt(q.yest_close)],
    ["最高", fmt(q.high)], ["最低", fmt(q.low)],
    ["成交额", fmtBig(q.amount)], ["换手", q.turnover ? fmt(q.turnover, 2) + "%" : "—"],
    ["量比", fmt(q.vol_ratio, 2)], ["振幅", (q.high && q.low && q.yest_close) ? fmt((q.high - q.low) / q.yest_close * 100, 2) + "%" : "—"],
  ];
  $("cStats").innerHTML = rows.map(r => `<div><span>${r[0]}</span><b>${r[1]}</b></div>`).join("");
}

async function renderScore(code, quote) {
  try {
    const s = await fetchJson("/api/score?code=" + code);
    $("cScore").textContent = s.score + "分";
    $("cSignals").innerHTML = (s.signals || []).map(x => `<li>${x}</li>`).join("") || "<li>无信号</li>";
    // ★ 4.4 量价综合诊断（量价是决定股价的唯二因素）
    const vpEl = $("cVp");
    if (s.vp) {
      vpEl.style.display = "block";
      vpEl.innerHTML = `📊 量价：<b>${s.vp}</b>`;
      vpEl.className = "vp-line" + ((s.vp_tags || []).some(t => t.indexOf("⚠") >= 0) ? " warn" : "");
    } else {
      vpEl.style.display = "none";
    }
    // ★ 4.5 多源信号投票（6 分析师多空博弈）
    const conEl = $("cConsensus");
    const c = s.consensus;
    if (c && c.votes && c.votes.length) {
      const icons = { "看多": "🟢", "看空": "🔴", "中性": "⚪" };
      const vCls = c.vote === "看多" ? "up" : (c.vote === "看空" ? "down" : "gold");
      conEl.style.display = "block";
      conEl.innerHTML = `
        <div class="con-head">🧠 多源投票（v4.5）：<b class="${vCls}">${icons[c.vote] || ""} ${c.vote}</b>
          <span class="con-meta">${c.longs}多 ${c.shorts}空 · 分歧度${c.divergence}</span></div>
        <div class="con-votes">
          ${c.votes.map(v => `<span class="con-v ${v.stance === "看多" ? "up" : (v.stance === "看空" ? "down" : "")}">
            ${icons[v.stance] || ""} ${v.name}</span>`).join("")}
        </div>
        <div class="con-verdict">${c.verdict}</div>`;
    } else {
      conEl.style.display = "none";
    }
  } catch (e) { /* 静默 */ }
}

/* 实时增量刷新 */
function startLiveTimer() {
  if (state.liveTimer) clearInterval(state.liveTimer);
  if (state.page !== "chart") return;
  state.liveTimer = setInterval(async () => {
    if (!document.hidden && $("optLive").checked) await renderChart();
  }, state.chartMode === "minute" ? 5000 : 30000);
}

/* 周期/选项事件 */
document.querySelectorAll("#periodTabs span").forEach(s =>
  s.addEventListener("click", () => {
    state.period = s.dataset.p;
    // ★ 4.5 修复：min5=分时曲线；其余=K线蜡烛
    state.chartMode = state.period === "min5" ? "minute" : "kline";
    $("optLive").checked = state.period !== "day";
    loadChart();
  }));
["optMA", "optBOLL", "optSub"].forEach(id =>
  $(id).addEventListener("change", () => { if (state.chartMode === "kline") renderChart(true); }));
$("optLive").addEventListener("change", () => {
  if ($("optLive").checked) startLiveTimer();
  else if (state.liveTimer) { clearInterval(state.liveTimer); state.liveTimer = null; }
});

/* 信息面板操作 */
$("btnWatch").addEventListener("click", async () => {
  const name = $("cName").textContent || "";
  await fetchJson("/api/watchlist", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action: "add", code: state.code, name }) });
  $("btnWatch").textContent = "✓ 已加自选";
  setTimeout(() => { $("btnWatch").textContent = "☆ 加自选"; }, 1500);
});
$("btnBuy").addEventListener("click", () => openTrade("buy"));
$("btnSell").addEventListener("click", () => openTrade("sell"));

/* ============ 交易弹层 ============ */
function openTrade(side) {
  $("tmTitle").textContent = side === "buy" ? "买入" : "卖出";
  $("tmCode").value = state.code;
  $("tmName").textContent = $("cName").textContent || "";
  $("tmQty").value = side === "sell" ? "" : "100";
  $("tmPrice").value = "";
  $("tmMsg").textContent = side === "sell" ? "提示：T+1 规则，当日买入不可卖出" : "";
  $("tradeModal").style.display = "flex";
  $("tmOk").onclick = async () => {
    const body = { side, code: $("tmCode").value.trim(), qty: +$("tmQty").value || 100 };
    const p = $("tmPrice").value.trim();
    if (p) body.price = p;
    const r = await fetchJson("/api/trade", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    $("tmMsg").textContent = r.msg;
    if (r.ok) { setTimeout(() => { $("tradeModal").style.display = "none"; refreshPortfolio(); }, 900); }
  };
}
$("tmClose").addEventListener("click", () => $("tradeModal").style.display = "none");
$("btnManualBuy").addEventListener("click", () => {
  state.code = ""; $("tmTitle").textContent = "买入"; $("tmCode").value = ""; $("tmName").textContent = "";
  $("tmQty").value = "100"; $("tmPrice").value = ""; $("tmMsg").textContent = "";
  $("tradeModal").style.display = "flex";
  $("tmOk").onclick = async () => {
    const body = { side: "buy", code: $("tmCode").value.trim(), qty: +$("tmQty").value || 100 };
    const p = $("tmPrice").value.trim();
    if (p) body.price = p;
    const r = await fetchJson("/api/trade", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    $("tmMsg").textContent = r.msg;
    if (r.ok) setTimeout(() => { $("tradeModal").style.display = "none"; refreshPortfolio(); }, 900);
  };
});

/* ============ 持仓页 ============ */
async function refreshPortfolio() {
  try {
    const d = await fetchJson("/api/state");
    const cards = [
      ["总资产", d.total, d.pnl_pct, true],
      ["现金", d.cash, null],
      ["持仓市值", d.mv, null],
      ["累计盈亏", d.pnl, d.pnl_pct, true],
    ];
    $("assetCards").innerHTML = cards.map(c => `
      <div class="asset-card">
        <div class="a-label">${c[0]}</div>
        <div class="a-val ${c[3] ? cls(c[1]) : ""}">¥${fmt(c[1], 0)}</div>
        ${c[2] != null ? `<div class="a-label ${cls(c[2])}">${(c[2] * 100).toFixed(2)}%</div>` : ""}
      </div>`).join("");
    const ptb = $("posTable").querySelector("tbody");
    ptb.innerHTML = d.positions.map(p => `
      <tr data-code="${p.code}">
        <td class="clickable">${p.code}</td><td>${p.name}</td>
        <td class="num">${p.qty}</td><td class="num">${fmt(p.entry_price)}</td>
        <td class="num">${fmt(p.price)}</td><td class="num">${fmt(p.price * p.qty, 0)}</td>
        <td class="num ${cls(p.pnl)}">${p.pnl > 0 ? "+" : ""}${fmt(p.pnl, 0)}</td>
        <td class="num ${cls(p.pnl_pct)}">${(p.pnl_pct * 100).toFixed(2)}%</td>
        <td class="num">${p.days || 0}天</td>
        <td>
          <button class="btn small buy" data-act="add" data-code="${p.code}" data-name="${p.name}">加仓</button>
          <button class="btn small sell" data-act="sell" data-code="${p.code}" data-name="${p.name}">卖出</button>
        </td>
      </tr>`).join("");
    ptb.querySelectorAll("button").forEach(b =>
      b.addEventListener("click", e => {
        e.stopPropagation();
        state.code = b.dataset.code; $("cName").textContent = b.dataset.name;
        if (b.dataset.act === "add") openTrade("buy");   // v3.9: 分批加仓（摊薄成本）
        else openTrade("sell");
      }));
    ptb.querySelectorAll("tr").forEach(tr =>
      tr.addEventListener("click", () => {
        if (tr.dataset.code) { state.code = tr.dataset.code; switchPage("chart"); }
      }));
    // 交易流水
    const ttb = $("tradeTable").querySelector("tbody");
    ttb.innerHTML = d.trades.slice().reverse().map(t => `
      <tr>
        <td>${(t.time || "").slice(5, 16)}</td>
        <td class="${t.side === "buy" ? "up" : "down"}">${t.side === "buy" ? "买入" : "卖出"}</td>
        <td>${t.code}</td><td>${t.name}</td>
        <td class="num">${fmt(t.price)}</td><td class="num">${t.qty}</td>
        <td class="num">${fmt(t.fee)}</td>
        <td class="num ${t.pnl != null ? cls(t.pnl) : ""}">${t.pnl != null ? (t.pnl > 0 ? "+" : "") + fmt(t.pnl, 0) : "—"}</td>
        <td title="${t.reason || ""}">${(t.reason || "").slice(0, 24)}</td>
      </tr>`).join("");
    // ★ 4.5 持仓归因（选股/择时/规模拆解）
    const attr = d.attribution;
    const acEl = $("attrCard");
    if (attr && attr.trade_count > 0) {
      acEl.style.display = "block";
      $("attrSummary").innerHTML = `
        <div class="attr-bars">
          ${[["选股", attr.selection], ["择时", attr.timing], ["规模", attr.scale]]
            .map(([k, v]) => `<div class="attr-item ${v >= 0 ? "up" : "down"}">
              <span>${k}</span><b>${v >= 0 ? "+" : ""}${fmt(v, 0)}</b></div>`).join("")}
        </div>
        <div class="attr-verdict">${attr.verdict}（已实现 ${attr.trade_count} 笔 · 基准收益 ${(attr.bench_return * 100).toFixed(2)}%）</div>`;
      const atb = $("attrTable").querySelector("tbody");
      atb.innerHTML = attr.stocks.map(s => `
        <tr>
          <td class="clickable">${s.code}</td><td>${s.name}</td>
          <td class="num ${cls(s.pnl)}">${s.pnl > 0 ? "+" : ""}${fmt(s.pnl, 0)}</td>
          <td class="num">${fmt(s.cost, 0)}</td>
          <td class="num ${cls(s.ret)}">${(s.ret * 100).toFixed(2)}%</td>
          <td class="num ${cls(s.selection)}">${s.selection > 0 ? "+" : ""}${fmt(s.selection, 0)}</td>
          <td class="num ${cls(s.timing)}">${s.timing > 0 ? "+" : ""}${fmt(s.timing, 0)}</td>
        </tr>`).join("");
    } else {
      acEl.style.display = "none";
    }
    renderPortfolioEquity(d.trades);
  } catch (e) { /* 静默 */ }
}
function startPosTimer() { state.posTimer = setInterval(refreshPortfolio, 8000); }

function renderPortfolioEquity(trades) {
  const dom = $("equityChart");
  const sells = trades.filter(t => t.side === "sell" && t.pnl != null);
  if (!sells.length) { dom.innerHTML = `<div class="placeholder">暂无已实现交易 — 卖出成交后显示资金曲线</div>`; return; }
  const init = 100000;
  const pts = [];
  let cum = init;
  sells.forEach(t => {
    cum += t.pnl;
    pts.push([t.time, +cum.toFixed(2)]);
  });
  const chart = echarts.getInstanceByDom(dom) || echarts.init(dom);
  chart.setOption({
    backgroundColor: "transparent", animation: false,
    tooltip: { trigger: "axis", valueFormatter: v => "¥" + fmt(v, 0), backgroundColor: "#1a2030", borderColor: "#2c3650", textStyle: { color: "#d6dde8" } },
    grid: { left: 70, right: 20, top: 20, bottom: 30 },
    xAxis: { type: "time", axisLabel: { color: "#8b96a8", fontSize: 10 } },
    yAxis: { type: "value", scale: true, axisLabel: { formatter: v => "¥" + (v / 10000).toFixed(1) + "万", color: "#8b96a8", fontSize: 10 },
      splitLine: { lineStyle: { color: "#1d2434" } } },
    series: [{ type: "line", data: pts, symbol: "none", lineStyle: { width: 1.6, color: "#4c9aff" }, areaStyle: { color: "#4c9aff", opacity: .1 } }],
  }, true);
  chart.resize();
}

/* ============ 回测页 ============ */
function initBacktestDates() {
  if ($("btStart").value) return;
  const end = new Date(), start = new Date();
  end.setDate(end.getDate() - 1);
  start.setFullYear(start.getFullYear() - 1);
  $("btStart").value = start.toISOString().slice(0, 10);
  $("btEnd").value = end.toISOString().slice(0, 10);
}
$("btMode").addEventListener("change", () => {
  const isPF = $("btMode").value === "portfolio";
  const isOpt = $("btMode").value === "optimize";
  const isMW = $("btMode").value === "multi_window";
  const isRig = $("btMode").value === "rigorous";
  $("btFoldsRow").style.display = (isPF || isMW || isRig) ? "none" : "";
  $("btStrategiesRow").style.display = isPF ? "" : "none";
  $("btOptRow").style.display = isOpt ? "" : "none";
  if (isRig) {
    // 严谨回测：提示将强制使用 top500 池
    const hint = $("btPoolHint");
    if (hint) hint.textContent = "🔬 严谨回测自动使用成交额 Top500 池，预计 30~60 分钟";
  }
});
$("btPool").addEventListener("change", async () => {
  $("btCustom").style.display = $("btPool").value === "custom" ? "" : "none";
  // ★ Phase10：大池预估耗时提示（82只×1年≈70s，与股票数近似成正比）
  const hint = $("btPoolHint");
  if (!hint) return;
  const v = $("btPool").value;
  if (v === "top500") { hint.textContent = "⏱ 约 500 只，满一年窗口约 6~7 分钟"; }
  else if (v === "local_full") {
    // 实时查池数量以给出准确预估
    try {
      const d = await fetchJson("/api/backtest/pool?kind=local_full");
      const n = d.count || 0;
      hint.textContent = `⏱ 本地全量 ${n} 只，满一年窗口约 ${Math.max(1, Math.round(n / 82 * 70 / 60))}~${Math.round(n / 82 * 70 / 60) + 1} 分钟`;
    } catch (e) { hint.textContent = "⏱ 大池回测较慢，建议用 3~6 个月窗口"; }
  } else { hint.textContent = ""; }
});

$("btnBacktest").addEventListener("click", async () => {
  const strategy = $("btStrategy").value;
  const isRig = $("btMode").value === "rigorous";
  // ★ Phase14：严谨回测强制 top500 池
  const poolType = isRig ? "top500" : $("btPool").value;
  let codes = [], names = {};
  try {
    if (poolType === "watchlist") {
      const w = await fetchJson("/api/watchlist");
      codes = (w.watchlist || []).map(x => x.code);
      (w.watchlist || []).forEach(x => { names[x.code] = x.name; });
    } else if (poolType === "custom") {
      codes = $("btCustom").value.split(/[,，\s]+/).map(s => s.trim()).filter(Boolean);
    } else if (poolType === "top500" || poolType === "local_full") {
      // ★ Phase10：大池走专用接口
      const d = await fetchJson("/api/backtest/pool?kind=" + poolType);
      if (d.error) { alert("获取股票池失败: " + d.error); return; }
      codes = d.codes || [];
      names = d.names || {};
    } else {
      const size = poolType === "top100" ? 100 : 300;
      const d = await fetchJson(`/api/stocklist?size=${size}&sort=amount`);
      codes = d.rows.map(r => r.code);
      d.rows.forEach(r => { names[r.code] = r.name; });
    }
  } catch (e) { alert("获取股票池失败: " + e.message); return; }
  if (!codes.length) { alert("股票池为空"); return; }
  const payload = {
    codes, names, strategy,
    start: $("btStart").value, end: $("btEnd").value,
    capital: +$("btCapital").value || 100000,
    mode: $("btMode").value,           // v3.5: normal | walk_forward | v3.7: portfolio | v3.9: optimize | Phase12: multi_window
    wf_folds: +$("btFolds").value || 3, // v3.5: 折叠数
    strategies: $("btMode").value === "portfolio"
      ? Array.from($("btStrategies").selectedOptions).map(o => o.value) : undefined,
    allocation: $("btAlloc").value,
    save_report: $("btSaveReport").checked,   // v3.9: 保存回测报告
    optimize_grid: $("btMode").value === "optimize" ? parseOptGrid($("btOptGrid").value) : undefined,
    optimize_metric: $("btOptMetric").value,
    params: {
      buy_threshold: +$("btThreshold").value || 25,
      max_positions: +$("btMaxPos").value || 3,
      position_pct: +$("btPosPct").value || 0.3,
      slippage: +$("btSlip").value || 0.001,
      portfolio_method: $("btPortMethod").value || "risk_parity",
      min5_days: +$("btMin5").value || 0,
    },
    sensitivity: $("btMode").value === "rigorous" ? {
      param: "buy_threshold", base: +$("btThreshold").value || 25, steps: [0.8, 1.0, 1.2],
      param2: "position_pct", steps2: [0.2, 0.3, 0.4],
    } : ($("btSens").checked && strategy !== "board_intraday" && strategy !== "auction_intraday" ? {
      param: "buy_threshold", base: +$("btThreshold").value || 25, steps: [0.8, 1.0, 1.2],
    } : null),
  };
  $("btPlaceholder").style.display = "none";
  $("btResult").style.display = "none";
  $("btPremium").style.display = "none";
  $("btProgress").textContent = `⏳ 任务启动（${codes.length} 只，并行下载K线中…）`;
  let jid;
  try {
    const r = await fetchJson("/api/backtest", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    jid = r.job_id;
  } catch (e) { $("btProgress").textContent = "启动失败: " + e.message; return; }
  const iv = setInterval(async () => {
    try {
      const s = await fetchJson("/api/backtest/status?id=" + jid);
      const pg = s.progress || {};
      if (s.status === "running") {
        // ★ Phase14：rigorous 用后端 phase 文案；其余沿用 下载/回放
        const phase = pg.phase
          ? (pg.phase.includes("严谨") ? pg.phase : (pg.phase === "回放" ? "回放交易中" : pg.phase))
          : "下载K线";
        $("btProgress").textContent = `⏳ ${phase} ${pg.cur || 0}/${pg.total || codes.length}`;
      } else if (s.status === "done") {
        clearInterval(iv);
        $("btProgress").textContent = "✅ 完成";
        renderBacktestResult(s.result, strategy);
        loadReports();   // v3.9: 刷新历史报告列表
      } else if (s.status === "error") {
        clearInterval(iv);
        $("btProgress").textContent = "❌ " + (s.error || "回测失败");
      }
    } catch (e) { /* 静默 */ }
  }, 1500);
});

/* 解析寻优参数网格：buy_threshold:[20,25,30];max_positions:[2,3] */
function parseOptGrid(s) {
  const grid = {};
  (s || "").split(";").forEach(part => {
    const m = part.match(/([\w_]+):\[([^\]]+)\]/);
    if (m) grid[m[1]] = m[2].split(",").map(x => +x.trim()).filter(v => !isNaN(v));
  });
  return grid;
}

/* v3.9：加载历史回测报告列表 */
async function loadReports() {
  try {
    const d = await fetchJson("/api/reports");
    const tb = $("btReportsTable").querySelector("tbody");
    tb.innerHTML = (d.reports || []).map(r => `
      <tr>
        <td>${(r.saved_at || "").slice(5, 16)}</td>
        <td>${r.strategy}</td>
        <td>${r.mode}</td>
        <td>${(r.start || "").slice(5)}~${(r.end || "").slice(5)}</td>
        <td class="num ${cls(r.total_return)}">${r.total_return != null ? (r.total_return * 100).toFixed(2) + "%" : "—"}</td>
        <td class="num">${r.sharpe != null ? fmt(r.sharpe, 2) : "—"}</td>
        <td class="num down">${r.max_drawdown != null ? (r.max_drawdown * 100).toFixed(2) + "%" : "—"}</td>
        <td class="num">${r.trade_count != null ? r.trade_count : "—"}</td>
        <td><a class="btn small" href="/api/report?id=${r.id}" target="_blank" style="text-decoration:none">查看</a></td>
      </tr>`).join("") || '<tr><td colspan="9" class="placeholder small">暂无历史报告（回测时勾选"保存回测报告"）</td></tr>';
  } catch (e) { /* 静默 */ }
}

function renderBacktestResult(r, strategy) {
  if (r.error) { $("btProgress").textContent = "❌ " + r.error; return; }
  // ★ v3.9：参数寻优结果分支
  if (r.best && r.results) {
    $("btResult").style.display = "";
    $("btPortfolio").style.display = "none";
    $("btWF").style.display = "none";
    $("btNormal").style.display = "";
    $("btPremium").style.display = "none";
    const b = r.best;
    const bParams = Object.entries(b.params || {}).map(([k, v]) => k + "=" + v).join(", ");
    $("btProgress").textContent = `✅ 寻优完成：搜索 ${r.searched} 组，最优 [${bParams}] 指标 ${r.metric}=${b.metric}`;
    // 用最优参数渲染常规回测展示（复用 btMetrics 区域需要数据，简化：直接展示指标卡）
    const m = [
      ["最优" + r.metric, b.metric, b.metric > 0 ? "good" : "", fmt(b.metric, 3)],
      ["总收益", b.total_return, b.total_return >= 0 ? "good" : "bad", (b.total_return * 100).toFixed(2) + "%"],
      ["夏普", b.sharpe, b.sharpe >= 1 ? "good" : "", fmt(b.sharpe, 2)],
      ["最大回撤", b.max_drawdown, "bad", (b.max_drawdown * 100).toFixed(2) + "%"],
      ["胜率", b.win_rate, b.win_rate >= 0.5 ? "good" : "", (b.win_rate * 100).toFixed(1) + "%"],
      ["交易笔数", null, "", b.trade_count + " 笔"],
      ["搜索组数", null, "", r.searched + " 组"],
      ["参数", null, "", bParams],
    ];
    $("btMetrics").innerHTML = m.map(x =>
      `<div class="metric ${x[2]}"><div class="m-label">${x[0]}</div><div class="m-val">${x[3]}</div></div>`).join("");
    // 寻优结果表（全部组合）
    const tb = $("btTrades").querySelector("tbody");
    tb.innerHTML = (r.results || []).map((x, i) => `
      <tr>
        <td>#${i + 1}</td>
        <td colspan="2">${Object.entries(x.params || {}).map(([k, v]) => k + "=" + v).join(" ")}</td>
        <td>${r.metric}</td>
        <td class="num">${x.metric != null ? fmt(x.metric, 3) : "—"}</td>
        <td class="num ${cls(x.total_return)}">${x.total_return != null ? (x.total_return * 100).toFixed(2) + "%" : "—"}</td>
        <td class="num">${x.sharpe != null ? fmt(x.sharpe, 2) : "—"}</td>
        <td class="num down">${x.max_drawdown != null ? (x.max_drawdown * 100).toFixed(2) + "%" : "—"}</td>
        <td>${x.trade_count} 笔</td>
      </tr>`).join("") || '<tr><td colspan="9" class="placeholder small">无有效组合</td></tr>';
    loadReports();
    return;
  }
  // ★ v3.7：多策略组合结果分支
  if (r.portfolio && r.strategies) {
    $("btResult").style.display = "";
    $("btPortfolio").style.display = "";
    $("btWF").style.display = "none";
    $("btNormal").style.display = "none";
    $("btPremium").style.display = "none";
    const pf = r.portfolio || {};
    $("btPFVerdict").textContent = `组合收益 ${(pf.total_return * 100).toFixed(2)}% · 期末资产 ¥${fmt(pf.final_equity, 0)} · ${pf.strategies} 个策略 · 分配方式：${pf.allocation === "equal" ? "等权" : "按绩效评分"}`;
    $("btPFVerdict").className = "wf-verdict " + (pf.total_return >= 0 ? "good" : "bad");
    const tb = $("btPFTable").querySelector("tbody");
    tb.innerHTML = (r.strategies || []).map(s => `
      <tr>
        <td>${s.strategy}</td>
        <td class="num">${(s.weight * 100).toFixed(1)}%</td>
        <td class="num">¥${fmt(r.capital_alloc[s.strategy] || 0, 0)}</td>
        <td class="num ${s.return != null ? cls(s.return) : ""}">${s.return != null ? (s.return * 100).toFixed(2) + "%" : "—"}</td>
        <td class="num">${s.sharpe != null ? fmt(s.sharpe, 2) : "—"}</td>
        <td class="num down">${s.max_drawdown != null ? (s.max_drawdown * 100).toFixed(2) + "%" : "—"}</td>
        <td class="num">${s.trade_count != null ? s.trade_count : "—"}</td>
        <td class="num ${s.contribution != null ? cls(s.contribution) : ""}">${s.contribution != null ? (s.contribution * 100).toFixed(2) + "%" : "—"}</td>
      </tr>`).join("");
    $("btPFMetrics").innerHTML = [
      ["组合总收益", pf.total_return, pf.total_return >= 0 ? "good" : "bad", (pf.total_return * 100).toFixed(2) + "%"],
      ["期末资产", null, "", "¥" + fmt(pf.final_equity, 0)],
      ["策略数", null, "", pf.strategies + " 个"],
      ["分配方式", null, "", pf.allocation === "equal" ? "等权" : "绩效评分"],
    ].map(x => `<div class="metric ${x[2]}"><div class="m-label">${x[0]}</div><div class="m-val">${x[3]}</div></div>`).join("");
    return;
  }
  // ★ Phase14：一键严谨回测结果分支
  if (r.mode === "rigorous") {
    $("btResult").style.display = "";
    $("btRigorous").style.display = "";
    $("btWF").style.display = "none";
    $("btNormal").style.display = "none";
    $("btPremium").style.display = "none";
    $("btMW").style.display = "none";
    if (r.error) { $("btProgress").textContent = "❌ " + r.error; return; }
    const v = r.verdict || {};
    $("btRgVerdict").textContent = (v.text || "无判定");
    $("btRgVerdict").className = "wf-verdict " + (v.level === "ok" ? "good" : v.level === "warn" ? "warn" : "bad");
    // ① 各窗口表
    const mw = r.multi_window || {}; const mww = mw.windows || [];
    $("btRgMW").querySelector("tbody").innerHTML = mww.map(w =>
      w.skipped ? `<tr><td colspan="6" class="placeholder small">${w.start}~${w.end}：无数据</td></tr>`
      : `<tr><td>${w.start}~${w.end}</td>
          <td class="num ${w.annual_return != null ? cls(w.annual_return) : ""}">${w.annual_return != null ? (w.annual_return * 100).toFixed(2) + "%" : "—"}</td>
          <td class="num">${w.sharpe != null ? fmt(w.sharpe, 2) : "—"}</td>
          <td class="num down">${w.max_drawdown != null ? (w.max_drawdown * 100).toFixed(2) + "%" : "—"}</td>
          <td class="num">${w.win_rate != null ? (w.win_rate * 100).toFixed(1) + "%" : "—"}</td>
          <td class="num">${w.trade_count != null ? w.trade_count : "—"}</td></tr>`).join("")
      || '<tr><td colspan="6" class="placeholder small">无窗口数据</td></tr>';
    // ② IS/OOS 表
    const wf = r.walk_forward || {}; const wff = wf.folds || [];
    $("btRgWF").querySelector("tbody").innerHTML = wff.map(f => `
      <tr><td>${f.fold}</td>
        <td class="num ${f.is_return != null ? cls(f.is_return) : ""}">${f.is_return != null ? (f.is_return * 100).toFixed(2) + "%" : "—"}</td>
        <td class="num ${f.oos_return != null ? cls(f.oos_return) : ""}">${f.oos_return != null ? (f.oos_return * 100).toFixed(2) + "%" : "—"}</td>
        <td class="num">${f.oos_sharpe != null ? fmt(f.oos_sharpe, 2) : "—"}</td>
        <td class="num down">${f.oos_max_drawdown != null ? (f.oos_max_drawdown * 100).toFixed(2) + "%" : "—"}</td>
        <td class="num">${f.degradation != null ? (f.degradation * 100).toFixed(1) + "%" : "—"}</td></tr>`).join("")
      || '<tr><td colspan="6" class="placeholder small">无 WF 数据</td></tr>';
    // ③ 敏感性热力
    const s = r.sensitivity || {}; const sm = s.matrix || [];
    let maxV = -1e18, minV = 1e18;
    sm.forEach(row => row.forEach(v => { if (v != null) { maxV = Math.max(maxV, v); minV = Math.min(minV, v); } }));
    const range = (maxV - minV) || 1e-9;
    const stb = $("btRgSens").querySelector("tbody");
    stb.innerHTML = sm.map((row, ri) => `<tr><td class="num">${(s.values2 || [0.2, 0.3, 0.4])[ri]}</td>` +
      row.map(v => {
        if (v == null) return `<td class="num">—</td>`;
        const heat = (v - minV) / range;
        const bg = heat > 0.5 ? `rgba(230,72,77,${0.25 + (heat - 0.5) * 0.6})`
          : `rgba(120,180,140,${0.15 + (1 - heat) * 0.35})`;
        return `<td class="num" style="background:${bg}">${(v * 100).toFixed(1)}%</td>`;
      }).join("") + "</tr>").join("")
      || '<tr><td colspan="4" class="placeholder small">无敏感性数据</td></tr>';
    return;
  }
  // ★ Phase12：跨年代多窗口验证结果分支
  if (r.windows && r.summary) {
    $("btResult").style.display = "";
    $("btMW").style.display = "";
    $("btWF").style.display = "none";
    $("btNormal").style.display = "none";
    $("btPremium").style.display = "none";
    const s = r.summary || {};
    const se = s.avg_annual != null ? ["平均年化", s.avg_annual, s.avg_annual > 0 ? "good" : "bad", (s.avg_annual * 100).toFixed(2) + "%"] : null;
    const ss = s.std_annual != null ? ["年化标准差", s.std_annual, "", (s.std_annual * 100).toFixed(2) + "%"] : null;
    const sp = s.positive_ratio != null ? ["正收益窗口占比", s.positive_ratio, s.positive_ratio >= 0.5 ? "good" : "bad", (s.positive_ratio * 100).toFixed(0) + "%"] : null;
    const sc = ["有效窗口数", null, "", (s.count || 0) + " 个"];
    $("btMWVerdict").textContent = s.verdict || "";
    $("btMWVerdict").className = "wf-verdict " + (String(s.verdict || "").startsWith("✅") ? "good" : "bad");
    $("btMWMetrics").innerHTML = [se, ss, sp, sc].filter(Boolean).map(x =>
      `<div class="metric ${x[2]}"><div class="m-label">${x[0]}</div><div class="m-val">${x[3]}</div></div>`).join("");
    const tb = $("btMWTable").querySelector("tbody");
    tb.innerHTML = (r.windows || []).map(w => w.skipped ? `
      <tr><td colspan="9" class="placeholder small">${w.start} ~ ${w.end}：无数据（${w.error || "本地库历史不足"}）</td></tr>`
      : `
      <tr>
        <td>${(w.window && w.window[0]) || w.start}</td>
        <td>${w.start}</td><td>${w.end}</td>
        <td class="num ${w.annual_return != null ? cls(w.annual_return) : ""}">${w.annual_return != null ? (w.annual_return * 100).toFixed(2) + "%" : "—"}</td>
        <td class="num">${w.sharpe != null ? fmt(w.sharpe, 2) : "—"}</td>
        <td class="num down">${w.max_drawdown != null ? (w.max_drawdown * 100).toFixed(2) + "%" : "—"}</td>
        <td class="num">${w.win_rate != null ? (w.win_rate * 100).toFixed(1) + "%" : "—"}</td>
        <td class="num">${w.trade_count != null ? w.trade_count : "—"}</td>
        <td class="num ${w.total_return != null ? cls(w.total_return) : ""}">${w.total_return != null ? (w.total_return * 100).toFixed(2) + "%" : "—"}</td>
      </tr>`).join("") || '<tr><td colspan="9" class="placeholder small">无窗口结果</td></tr>';
    return;
  }
  // ★ v3.5：walk-forward 结果分支
  if (r.folds) {
    $("btResult").style.display = "";
    $("btWF").style.display = "";
    $("btNormal").style.display = "none";
    $("btPremium").style.display = "none";
    const s = r.summary || {};
    $("btWFVerdict").textContent = (s.verdict || "") + `（平均样本外收益 ${s.avg_oos_return != null ? (s.avg_oos_return * 100).toFixed(2) + "%" : "—"}，正收益折叠 ${s.positive_folds}/${s.folds}）`;
    $("btWFVerdict").className = "wf-verdict " + (s.stable ? "good" : "bad");
    const tb = $("btWFTables").querySelector("tbody");
    tb.innerHTML = (r.folds || []).map(f => `
      <tr>
        <td>${f.fold}</td>
        <td>${f.train[0]} ~ ${f.train[1]}</td>
        <td>${f.test[0]} ~ ${f.test[1]}</td>
        <td class="num">${f.best_threshold != null ? f.best_threshold : "—"}</td>
        <td class="num ${f.is_return != null ? cls(f.is_return) : ""}">${f.is_return != null ? (f.is_return * 100).toFixed(2) + "%" : "—"}</td>
        <td class="num ${f.oos_return != null ? cls(f.oos_return) : ""}">${f.oos_return != null ? (f.oos_return * 100).toFixed(2) + "%" : "—"}</td>
        <td class="num">${f.oos_sharpe != null ? fmt(f.oos_sharpe, 2) : "—"}</td>
        <td class="num down">${f.oos_max_drawdown != null ? (f.oos_max_drawdown * 100).toFixed(2) + "%" : "—"}</td>
        <td class="num">${f.oos_win_rate != null ? (f.oos_win_rate * 100).toFixed(1) + "%" : "—"}</td>
        <td class="num">${f.oos_trades != null ? f.oos_trades : "—"}</td>
      </tr>`).join("");
    return;
  }
  $("btResult").style.display = "";
  $("btWF").style.display = "none";
  $("btNormal").style.display = "";
  $("btResult").style.display = "";
  const m = [
    ["总收益", r.total_return, r.total_return >= 0 ? "good" : "bad", (r.total_return * 100).toFixed(2) + "%"],
    ["年化收益", r.annual_return, r.annual_return >= 0 ? "good" : "bad", (r.annual_return * 100).toFixed(2) + "%"],
    ["最大回撤", r.max_drawdown, "bad", (r.max_drawdown * 100).toFixed(2) + "%"],
    ["夏普比率", r.sharpe, r.sharpe >= 1 ? "good" : "", fmt(r.sharpe, 2)],
    ["胜率", r.win_rate, r.win_rate >= 0.5 ? "good" : "", (r.win_rate * 100).toFixed(1) + "%"],
    ["盈亏比", r.profit_factor, r.profit_factor >= 1.5 ? "good" : "", fmt(r.profit_factor, 2)],
    ["交易笔数", null, "", r.trade_count + "笔"],
    ["期末资产", null, "", "¥" + fmt(r.final_equity, 0)],
  ];
  $("btMetrics").innerHTML = m.map(x =>
    `<div class="metric ${x[2]}"><div class="m-label">${x[0]}</div><div class="m-val">${x[3]}</div></div>`).join("");
  // ★ v3.4 绩效分析（Sortino/Calmar/回撤区间/水下时间）
  const pa = r.performance;
  if (pa) {
    const pm = [
      ["波动率(年化)", pa.volatility != null, (pa.volatility * 100).toFixed(2) + "%"],
      ["Sortino", pa.sortino != null, pa.sortino >= 99 ? "∞" : fmt(pa.sortino, 2)],
      ["Calmar", pa.calmar != null, fmt(pa.calmar, 2)],
      ["回撤区间", pa.dd_peak_date && pa.dd_trough_date, (pa.dd_peak_date || "—") + " → " + (pa.dd_trough_date || "—") + (pa.dd_recover_date ? " 恢复" : " 未恢复")],
      ["回撤持续", pa.max_dd_days != null, pa.max_dd_days + " 交易日"],
      ["水下时间", pa.underwater_days != null, pa.underwater_days + " 日 / 最长" + (pa.max_underwater_days || 0) + "日"],
      ["最大连续亏损", pa.max_consecutive_losses != null, pa.max_consecutive_losses + " 次"],
      ["平均持仓", pa.avg_holding_days != null ? pa.avg_holding_days + " 天" : "—"],
    ];
    $("btPerfMetrics").innerHTML = pm.map(x =>
      `<div class="metric ${x[1] ? "" : "dim"}"><div class="m-label">${x[0]}</div><div class="m-val">${x[2]}</div></div>`).join("");
    renderMonthlyHeat(pa.monthly_returns || {});
  } else {
    $("btPerfMetrics").innerHTML = '<div class="placeholder small">绩效分析数据不足</div>';
  }
  // ★ v3.4 / Phase13 参数敏感度（单参数表格 / 双参数热力表）
  const sens = r.sensitivity;
  if (sens && sens.dual && sens.matrix) {
    // ---- 双参数热力表 ----
    const stb = $("btSensTable").querySelector("tbody");
    const v2 = sens.values2 || [];
    const m = sens.matrix || [];
    // 收集行/列最大值用于配色（相对强度）
    let maxV = -1e18, minV = 1e18;
    m.forEach(row => row.forEach(v => { if (v != null) { maxV = Math.max(maxV, v); minV = Math.min(minV, v); } }));
    const range = (maxV - minV) || 1e-9;
    // 第一行：列头（param 的步长值）
    let html = `<tr><th>${sens.param2} \\ ${sens.param}</th>` +
      (sens.stepsLabels || [0.8, 1.0, 1.2]).map(s => `<th class="num">${s}</th>`).join("") + "</tr>";
    v2.forEach((vv, i) => {
      const row = m[i] || [];
      html += `<tr><td class="num">${vv}</td>` + row.map(v => {
        if (v == null) return `<td class="num">—</td>`;
        const heat = (v - minV) / range;
        // 颜色：收益高→亮红，低→深色（与红涨绿跌主题一致）
        const bg = heat > 0.5
          ? `rgba(230,72,77,${0.25 + (heat - 0.5) * 0.6})`
          : `rgba(120,180,140,${0.15 + (1 - heat) * 0.35})`;
        return `<td class="num" style="background:${bg}">${(v * 100).toFixed(1)}%</td>`;
      }).join("") + "</tr>";
    });
    stb.innerHTML = html;
    // ---- 稳定性判断：相邻档收益变化 ----
    $("btSensNote") && ($("btSensNote").style.display = "");
    const note = $("btSensNote");
    if (note) {
      // 对每行（param2 固定）看 param 列方向相邻变化；再对所有相邻对取最大 |Δ|
      const deltas = [];
      m.forEach(row => {
        for (let j = 1; j < row.length; j++) {
          if (row[j] != null && row[j - 1] != null) deltas.push(Math.abs(row[j] - row[j - 1]));
        }
      });
      v2.forEach((_, i) => {
        for (let j = 1; j < m.length; j++) {
          if (m[j] && m[j - 1] && m[j][0] != null && m[j - 1][0] != null)
            deltas.push(Math.abs(m[j][0] - m[j - 1][0]));
        }
      });
      const maxD = deltas.length ? Math.max.apply(null, deltas) : 0;
      const avgD = deltas.length ? deltas.reduce((a, b) => a + b, 0) / deltas.length : 0;
      const stable = avgD < 0.05 && maxD < 0.12;   // 相邻平均变化<5pp 且最大<12pp → 平台区
      note.innerHTML = (stable
        ? `<span class="up">✅ 参数平台区：相邻档收益变化平缓（平均 ${(avgD * 100).toFixed(1)}pp、最大 ${(maxD * 100).toFixed(1)}pp），稳健</span>`
        : `<span class="down">⚠️ 参数敏感：相邻档收益出现悬崖（最大变化 ${(maxD * 100).toFixed(1)}pp），存在过拟合风险</span>`)
        + `<span class="tip">　提示：网格横轴=${sens.param}，纵轴=${sens.param2}</span>`;
    }
  } else if (sens && sens.length) {
    const stb = $("btSensTable").querySelector("tbody");
    stb.innerHTML = sens.map(x => `
      <tr>
        <td class="num">${x.value}</td>
        <td class="num ${cls(x.total_return)}">${(x.total_return * 100).toFixed(2)}%</td>
        <td class="num">${fmt(x.sharpe, 2)}</td>
        <td class="num down">${(x.max_drawdown * 100).toFixed(2)}%</td>
        <td class="num">${(x.win_rate * 100).toFixed(1)}%</td>
        <td class="num">${x.trade_count}</td>
      </tr>`).join("");
    $("btSensNote") && ($("btSensNote").style.display = "none");
  } else if (r.sensitivity === null || r.sensitivity === undefined) {
    const stb = $("btSensTable").querySelector("tbody");
    stb.innerHTML = '<tr><td colspan="6" class="placeholder small">未开启敏感度扫描（高级参数中勾选）</td></tr>';
  }
  // ★ v3.4 回测真实性审计提示
  const notes = r.audit_notes || [];
  const suspNote = r.suspension_skips ? `停牌跳过交易 ${r.suspension_skips} 次` : "";
  $("btAudit").innerHTML = (suspNote ? `<div class="audit-item">· ${suspNote}</div>` : "") +
    notes.map(n => `<div class="audit-item">· ${n}</div>`).join("") ||
    '<div class="placeholder small">无审计提示</div>';
  const eq = r.equity_curve || [];
  if (eq.length) {
    const dates = eq.map(e => e.date);
    const norm = eq.map(e => +(e.equity / eq[0].equity).toFixed(4));
    const bench = r.benchmark_curve || null;
    renderEquityChart($("btEquityChart"), dates, norm, bench, "基准(等权)");
  }
  const ttb = $("btTrades").querySelector("tbody");
  ttb.innerHTML = (r.trades || []).map(t => `
    <tr>
      <td>${t.date}</td><td class="${t.side === "buy" ? "up" : "down"}">${t.side === "buy" ? "买" : "卖"}</td>
      <td>${t.code}</td><td>${t.name}</td>
      <td class="num">${fmt(t.price)}</td><td class="num">${t.qty}</td><td class="num">${fmt(t.fee)}</td>
      <td class="num ${t.pnl != null ? cls(t.pnl) : ""}">${t.pnl != null ? (t.pnl > 0 ? "+" : "") + fmt(t.pnl, 0) : "—"}</td>
      <td title="${t.reason || ""}">${(t.reason || "").slice(0, 30)}</td>
    </tr>`).join("");
  // 次日溢价统计（日线premium策略顶层 / 分钟策略 premium 字段）
  const prem = r.premium || ((strategy === "premium" && r.rows) ? r : null);
  if (prem && prem.rows) {
    $("btPremium").style.display = "";
    $("btPMetrics").innerHTML = [
      ["样本数", null, "", prem.total + " 次"],
      ["胜率(次日收涨)", prem.win_rate, prem.win_rate >= 0.5 ? "good" : "", (prem.win_rate * 100).toFixed(1) + "%"],
      ["平均高开", prem.avg_open, prem.avg_open > 0 ? "good" : "bad", "+" + prem.avg_open.toFixed(2) + "%"],
      ["平均收盘溢价", prem.avg_close, prem.avg_close > 0 ? "good" : "bad", (prem.avg_close > 0 ? "+" : "") + prem.avg_close.toFixed(2) + "%"],
    ].map(x => `<div class="metric ${x[2]}"><div class="m-label">${x[0]}</div><div class="m-val">${x[3]}</div></div>`).join("");
    const tb = $("btPRows").querySelector("tbody");
    tb.innerHTML = prem.rows.map(x => {
      const trig = x.time ? `${x.time} 触发` : (x.pct != null ? `${x.pct > 0 ? "+" : ""}${fmt(x.pct, 1)}%` : "—");
      return `<tr><td>${x.date}</td><td>${x.next_date}</td><td>${x.code}</td><td>${x.name}</td>
      <td class="num">${trig}</td>
      <td class="num ${cls(x.open_prem)}">${x.open_prem > 0 ? "+" : ""}${fmt(x.open_prem, 1)}%</td>
      <td class="num ${cls(x.close_prem)}">${x.close_prem > 0 ? "+" : ""}${fmt(x.close_prem, 1)}%</td>
      <td class="num ${cls(x.high_prem)}">${x.high_prem > 0 ? "+" : ""}${fmt(x.high_prem, 1)}%</td>
    </tr>`;
    }).join("");
  }
}

/* 月度收益热力图（v3.4） */
function renderMonthlyHeat(monthly) {
  const dom = $("btMonthly");
  const keys = Object.keys(monthly);
  if (!keys.length) { dom.innerHTML = '<div class="placeholder small">无月度数据</div>'; return; }
  dom.innerHTML = keys.map(k => {
    const v = monthly[k];
    const color = v >= 0
      ? `rgba(220,60,60,${Math.min(0.9, 0.15 + Math.abs(v) * 6)})`
      : `rgba(30,160,80,${Math.min(0.9, 0.15 + Math.abs(v) * 6)})`;
    return `<div class="mcell" style="background:${color}" title="${k}: ${(v * 100).toFixed(2)}%">
      <span class="mm">${k}</span><span class="mv">${(v * 100).toFixed(1)}%</span></div>`;
  }).join("");
}

/* ============ 因子研究页 (v3.6) ============ */
let fcData = null;  // 当前 IC 扫描结果

async function loadFactors() {
  try {
    const d = await fetchJson("/api/factors");
    $("fcTip").textContent = `因子库 ${d.factors.length} 个 · 输入股票池后扫描`;
  } catch (e) { /* 静默 */ }
}

async function factorScan() {
  const codes = $("fcCodes").value.trim().replace(/[，\s]+/g, ",");
  const horizon = $("fcHorizon").value;
  if (!codes) { alert("请输入股票池代码"); return; }
  $("fcTip").textContent = "⏳ 扫描中…（逐因子计算截面 IC）";
  try {
    const d = await fetchJson(`/api/factor/scan?codes=${encodeURIComponent(codes)}&horizon=${horizon}`);
    fcData = d.results || [];
    $("fcTip").textContent = `${d.stocks} 只股票 · ${fcData.length} 个因子有效`;
    const tb = $("fcTable").querySelector("tbody");
    tb.innerHTML = fcData.map((r, i) => `
      <tr data-factor="${r.name}">
        <td>${i + 1}</td>
        <td class="clickable">${r.name}</td>
        <td title="${r.desc || ""}">${r.desc || ""}</td>
        <td class="num ${r.ic_mean > 0 ? "up" : "down"}">${r.ic_mean.toFixed(4)}</td>
        <td class="num">${fmt(r.icir, 3)}</td>
        <td class="num">${(r.positive_ratio * 100).toFixed(0)}%</td>
        <td class="num">${r.samples}</td>
        <td><button class="btn small" data-layer="${r.name}">分层回测</button></td>
      </tr>`).join("");
    tb.querySelectorAll("[data-layer]").forEach(b =>
      b.addEventListener("click", () => factorLayers(b.dataset.layer)));
    tb.querySelectorAll("[data-factor]").forEach(tr =>
      tr.addEventListener("click", () => factorLayers(tr.dataset.factor)));
  } catch (e) {
    $("fcTip").textContent = "❌ " + e.message;
  }
}

async function factorLayers(name) {
  const codes = $("fcCodes").value.trim().replace(/[，\s]+/g, ",");
  const horizon = $("fcHorizon").value;
  $("fcLayerTip").textContent = `分层回测：${name}（第1层=因子值最小，第5层=最大）`;
  try {
    const d = await fetchJson(`/api/factor/ic?factor=${encodeURIComponent(name)}&codes=${encodeURIComponent(codes)}&horizon=${horizon}`);
    const layers = d.layers || [];
    $("fcLayers").innerHTML = layers.map(l => {
      const v = l.avg_ret;
      const color = v >= 0
        ? `rgba(220,60,60,${Math.min(0.9, 0.2 + Math.abs(v) * 30)})`
        : `rgba(30,160,80,${Math.min(0.9, 0.2 + Math.abs(v) * 30)})`;
      return `<div class="mcell" style="width:110px;background:${color}">
        <span class="mm">第${l.layer}层</span>
        <span class="mv">${(v * 100).toFixed(2)}%</span>
        <span class="mm" style="font-size:9px">胜率${(l.up_ratio * 100).toFixed(0)}%</span>
      </div>`;
    }).join("") + `<div class="audit-item" style="width:100%;margin-top:6px">
      单调性评分：<b>${d.monotonicity != null ? d.monotonicity : "—"}</b>（1.0=完全单调，收益随因子值递增/递减）· IC=${d.ic_mean != null ? d.ic_mean : "—"}</div>`;
  } catch (e) {
    $("fcLayerTip").textContent = "❌ " + e.message;
  }
}

async function calcWeights() {
  const codes = $("fcCodes").value.trim().replace(/[，\s]+/g, ",");
  const method = $("fcMethod").value;
  if (!codes) { alert("请输入股票池代码"); return; }
  try {
    const d = await fetchJson(`/api/portfolio/weights?codes=${encodeURIComponent(codes)}&method=${method}`);
    if (d.error) { $("fcWeights").innerHTML = '<div class="audit-item">' + d.error + "</div>"; return; }
    $("fcWeights").innerHTML = (d.weights || []).map(w => `
      <div class="audit-item">${w.name}(${w.code}) <b style="color:${w.weight > 0.25 ? "var(--gold)" : ""}">${(w.weight * 100).toFixed(1)}%</b></div>
    `).join("") + `<div class="audit-item">方法：${d.method} · 权重合计 ${(d.sum * 100).toFixed(0)}% · ${d.stocks} 只</div>`;
  } catch (e) {
    $("fcWeights").innerHTML = '<div class="audit-item">❌ ' + e.message + "</div>";
  }
}
$("btnFactorScan").addEventListener("click", factorScan);
$("btnFcWeights").addEventListener("click", calcWeights);

/* ★ 4.3 LLM 因子挖掘 */
async function mineFactors() {
  const codes = $("fcCodes").value.trim().replace(/[，\s]+/g, ",");
  const n = +$("fcMineN").value || 5;
  const provider = $("fcMineProvider").value;
  if (!codes) { alert("请输入股票池代码"); return; }
  $("fcMineResult").innerHTML = '<div class="audit-item">⏳ 挖掘中（LLM提因子→IC检验）…</div>';
  try {
    const d = await fetchJson(`/api/factor/mine?codes=${encodeURIComponent(codes)}&n=${n}&horizon=5&provider=${provider}`);
    if (d.error) { $("fcMineResult").innerHTML = '<div class="audit-item">❌ ' + d.error + "</div>"; return; }
    const cands = d.candidates || [];
    $("fcMineResult").innerHTML = (cands.length ? cands.map((c, i) => `
      <div class="audit-item">${i + 1}. <b>${c.name}</b> IC=${c.ic_mean} ICIR=${c.icir} 正占比=${(c.positive_ratio * 100).toFixed(0)}%</div>`
    ).join("") : '<div class="audit-item">本轮无有效候选（IC 样本不足）</div>') +
      `<div class="audit-item" style="margin-top:4px">${d.stocks} 只股票 · 建议将高 |IC| 因子加入因子库</div>`;
  } catch (e) {
    $("fcMineResult").innerHTML = '<div class="audit-item">❌ ' + e.message + "</div>";
  }
}
async function loadFactorPool() {
  try {
    const d = await fetchJson("/api/factor/pool");
    const fs = d.factors || [];
    $("fcMineResult").innerHTML = (fs.length ? fs.map((f, i) => `
      <div class="audit-item">${i + 1}. <b>${f.expr}</b> IC=${f.ic} ICIR=${f.icir} 样本=${f.samples}（${f.created}）</div>`
    ).join("") : '<div class="audit-item">因子池为空 — 先运行挖掘</div>');
  } catch (e) {
    $("fcMineResult").innerHTML = '<div class="audit-item">❌ ' + e.message + "</div>";
  }
}
$("btnFcMine").addEventListener("click", mineFactors);
$("btnFcPool").addEventListener("click", loadFactorPool);

/* ============ 审计复盘页 (v3.5) ============ */
async function refreshAudit() {
  try {
    const day = $("auditDay").value;
    const q = day ? "?day=" + day : "";
    const d = await fetchJson("/api/audit" + q);
    const summ = d.summary || { total: 0 };
    const cards = [
      ["当日事件", summ.total + " 条", null, false],
      ["买入", summ.buys + " 笔", summ.buys > 0, false],
      ["卖出", summ.sells + " 笔", summ.sells > 0, false],
      ["风控拦截", summ.risk_blocks + " 次", summ.risk_blocks > 0, false],
      ["告警", summ.alerts + " 条", summ.alerts > 0, false],
    ];
    $("auditSummary").innerHTML = cards.map(c => `
      <div class="asset-card">
        <div class="a-label">${c[0]}</div>
        <div class="a-val ${c[2] ? "up" : ""}">${c[1]}</div>
      </div>`).join("");
    const ok = d.chain_ok;
    const chainTxt = ok
      ? "完整 ✓（" + d.chain_checked + " 条记录，防篡改）"
      : "异常 ✗（" + (d.chain_broken_at || "数据损坏") + "）";
    $("auditChain").innerHTML = "🛡️ 哈希链校验：<b class=\"" + (ok ? "up" : "down") + "\">" + chainTxt + "</b>";
    $("auditTip").textContent = day ? `按日回放 ${day} · 共 ${d.total} 条` : "最近事件（默认当日）";
    const list = $("auditList");
    list.innerHTML = (d.rows || []).slice().reverse().map(r => {
      const clsMap = { BUY: "buy", SELL: "sell", WARN: "warn", ERROR: "error", OK: "ok" };
      const k = clsMap[r.level] || "";
      return `<div class="ev ${k}"><span class="ev-t">${(r.t || "").slice(11, 19)}</span>[${r.kind}] ${r.event} ${r.msg || ""} ${r.code ? "(" + r.code + ")" : ""}</div>`;
    }).join("") || '<div class="placeholder small">当日无审计事件</div>';
  } catch (e) { /* 静默 */ }
}
$("btnAuditRefresh").addEventListener("click", refreshAudit);
$("auditDay").addEventListener("change", refreshAudit);
$("btnDailyPush").addEventListener("click", async () => {
  $("auditChain").textContent = "⏳ 日结推送中…";
  const r = await fetchJson("/api/audit/daily", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
  $("auditChain").textContent = r.ok ? "✅ 日结已推送（需在设置页配置并启用通知）" : "❌ " + (r.error || "推送失败");
});
$("btnReviewNow").addEventListener("click", async () => {
  $("auditReview").innerHTML = '<div class="placeholder small">⏳ 生成复盘中（LLM 或本地结构化）…</div>';
  const q = $("auditDay").value ? "?day=" + $("auditDay").value : "";
  try {
    const r = await fetchJson("/api/review" + q, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    if (r.ok) {
      const prov = { ollama: "本地 Ollama", api: "LLM API", local: "本地结构化" }[r.provider] || r.provider;
      $("auditReview").innerHTML = `<div class="review-head">${r.day} 复盘 · ${prov}</div><pre class="review-pre">${r.report}</pre>`;
    } else {
      $("auditReview").innerHTML = '<div class="placeholder small">❌ ' + (r.error || "生成失败") + "</div>";
    }
  } catch (e) {
    $("auditReview").innerHTML = '<div class="placeholder small">❌ ' + e.message + "</div>";
  }
});

/* ============ 设置页 ============ */
async function loadSettings() {
  try {
    const n = await fetchJson("/api/notify");
    $("setSendkey").value = n.sendkey || "";
    $("setNotify").checked = !!n.enabled;
    $("setWecom").value = n.wecom_url || "";
    $("setDingtalk").value = n.dingtalk_url || "";
    $("setDaily").checked = n.daily_report !== false;
  } catch (e) { /* 静默 */ }
  // v3.5：LLM 配置
  try {
    const r = await fetchJson("/api/llm/config");
    $("setOllamaUrl").value = r.ollama_url || "http://127.0.0.1:11434";
    $("setOllamaModel").value = r.ollama_model || "";
    $("setApiKey").value = r.api_key || "";
    $("setApiBase").value = r.api_base || "https://api.deepseek.com/v1";
    $("setApiModel").value = r.model || "";
  } catch (e) { /* 静默 */ }
  // ★ 4.3：佣金档位
  try {
    const r = await fetchJson("/api/commission");
    const sel = $("setCommTier");
    sel.innerHTML = (r.tiers || []).map(t =>
      `<option value="${t.name}" ${t.name === r.current ? "selected" : ""}>${t.name}（${(t.rate * 10000).toFixed(2)}）</option>`).join("");
  } catch (e) { /* 静默 */ }
  // ★ 4.5：数据更新状态
  loadUpdateStatus();
}
async function loadUpdateStatus() {
  try {
    const r = await fetchJson("/api/update");
    $("upMin5").textContent = r.data && r.data.min5_latest ? r.data.min5_latest : "—";
    $("upDay").textContent = r.data && r.data.day_latest ? r.data.day_latest : "—";
    $("upState").textContent = r.running ? "⏳ 更新中…" : (r.needs ? "⚠️ 有更新待执行" : "✅ 已是最新");
    $("upMsg").textContent = r.running ? "后台正在增量更新数据（min5 + 日K）…" : "";
  } catch (e) { /* 静默 */ }
}
$("btnDataUpdate").addEventListener("click", async () => {
  $("upMsg").textContent = "⏳ 已触发后台更新，稍后刷新查看结果…";
  await fetchJson("/api/update?run=1");
  setTimeout(loadUpdateStatus, 8000);
});
$("btnSaveComm").addEventListener("click", async () => {
  const r = await fetchJson("/api/commission", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ tier: $("setCommTier").value }) });
  alert(r.ok ? "佣金档位已保存（万" + (r.rate * 10000).toFixed(2) + "）" : "保存失败");
});
$("btnSaveNotify").addEventListener("click", async () => {
  await fetchJson("/api/notify", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ sendkey: $("setSendkey").value.trim(), enabled: $("setNotify").checked,
      wecom_url: $("setWecom").value.trim(), dingtalk_url: $("setDingtalk").value.trim(),
      daily_report: $("setDaily").checked }) });
  alert("已保存");
});
$("btnSaveLlm").addEventListener("click", async () => {
  await fetchJson("/api/llm", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ollama_url: $("setOllamaUrl").value.trim(), ollama_model: $("setOllamaModel").value.trim(),
      api_key: $("setApiKey").value.trim(), api_base: $("setApiBase").value.trim(),
      model: $("setApiModel").value.trim() }) });
  alert("LLM 配置已保存");
});
$("btnGenReview").addEventListener("click", async () => {
  const box = $("reviewResult");
  box.style.display = "";
  box.innerHTML = '<div class="placeholder small">⏳ 生成复盘中…</div>';
  try {
    const r = await fetchJson("/api/review", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    if (r.ok) {
      const prov = { ollama: "本地 Ollama", api: "LLM API", local: "本地结构化" }[r.provider] || r.provider;
      box.innerHTML = `<div class="review-head">${r.day} 复盘 · ${prov}</div><pre class="review-pre">${r.report}</pre>`;
    } else box.innerHTML = '<div class="placeholder small">❌ ' + (r.error || "生成失败") + "</div>";
  } catch (e) { box.innerHTML = '<div class="placeholder small">❌ ' + e.message + "</div>"; }
});
$("btnReset").addEventListener("click", async () => {
  if (!confirm("确认重置模拟盘？将清空所有持仓与交易流水。")) return;
  await fetchJson("/api/reset", { method: "POST" });
  alert("已重置");
  refreshPortfolio();
});

/* ============ 日志 ============ */
$("btnLog").addEventListener("click", async () => {
  $("logModal").style.display = "flex";
  try {
    const d = await fetchJson("/api/log");
    $("logBox").textContent = d.lines.join("\n");
  } catch (e) { $("logBox").textContent = "日志不可用"; }
});
$("btnLogClose").addEventListener("click", () => $("logModal").style.display = "none");

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
  try {
    const d = await fetchJson("/api/news/premarket?scope=global&min_score=0");
    const items = (d.items || []).filter(x => x.market !== "其他") || [];
    const el = $("gmNewsList");
    el.innerHTML = items.slice(0, 8).map(it => `
      <div class="gm-news-item">
        <span class="gm-nt">${it.time || ""}</span>${it.title}
        <span class="gm-mk">#${(it.markets || [it.market]).join(" #")}</span>
      </div>`).join("") || '<div class="placeholder small">暂无全球市场相关新闻</div>';
  } catch (e) { $("gmNewsList").innerHTML = '<div class="placeholder small">新闻数据暂不可用</div>'; }
}
// 全局新闻跟随 refreshGlobal 一并加载（页面激活时）
function refreshGlobalAll(force) {
  refreshGlobal(force);
  if (state.page === "global") loadGlobalNews(force);
}

/* ============ ★ 4.6 板块资金流向（双侧连线图） ============ */
let sflowChart = null;
let sflowInit = false;
function startSflowTimer() {
  clearTimers();
  if (!state.sflowTimer) state.sflowTimer = setInterval(refreshSectorFlow, 5000);
}
function initSectorFlow() {
  if (!sflowInit) {
    sflowChart = echarts.init($("sflowChart"));
    sflowInit = true;
    window.addEventListener("resize", () => { if (sflowChart) sflowChart.resize(); });
  }
  refreshSectorFlow();
}
async function refreshSectorFlow() {
  try {
    const type = state.sflowType || "industry";
    const r = await fetchJson("/api/sector/flow?type=" + type + "&top=6");
    if (r.error) { $("sflowNote").textContent = "\u6570\u636e\u83b7\u53d6\u5931\u8d25: " + r.error; return; }
    if (r.disabled) {
      $("sflowNote").textContent = "\u677f\u5757\u8d44\u91d1\u6d41\u672a\u542f\u7528\uff08config.SECTOR_FLOW_ENABLED=False\uff09";
      $("sflowTime").textContent = "";
      $("sflowStats").innerHTML = "";
      $("sflowInList").innerHTML = "";
      $("sflowOutList").innerHTML = "";
      if (sflowChart) sflowChart.clear();
      return;
    }
    const srcTxt = r.source === "sina" ? "\u65b0\u6d6a\u00b7\u5b9e\u65f6" : ((r.source || "").indexOf("eastmoney") === 0 ? "\u4e1c\u8d22\u00b7\u5ef6\u65f6" : (r.source || ""));
    $("sflowTime").textContent = (r.fetched_at ? "\u66f4\u65b0 " + r.fetched_at : "") + (srcTxt ? " \u00b7 " + srcTxt : "") + (r.total ? " \u00b7 \u5171" + r.total + "\u4e2a\u677f\u5757" : "");
    $("sflowNote").textContent = r.degraded ? "\u26a0\ufe0f " + r.degraded : "";
    renderSectorFlow(r);
  } catch (e) {
    $("sflowNote").textContent = "\u52a0\u8f7d\u5931\u8d25: " + e.message;
  }
}
/* \u2605 4.7 \u91d1\u989d\u683c\u5f0f\u5316\uff08\u5e26\u7b26\u53f7\uff0cfmtBig \u4e0d\u5904\u7406\u8d1f\u6570\uff09 */
const _sfAmt = v => v == null ? "\u2014" : (v >= 0 ? "+" : "-") + fmtBig(Math.abs(v));
function renderSflowStats(left, right) {
  const sumIn = right.reduce((a, s) => a + (s.net > 0 ? s.net : 0), 0);
  const sumOut = left.reduce((a, s) => a + (s.net < 0 ? s.net : 0), 0);
  const topIn = right[0], topOut = left[0];
  const chips = [
    ["\u51c0\u6d41\u5165 TOP", topIn ? topIn.name + " " + _sfAmt(topIn.net) : "\u2014", "in"],
    ["\u51c0\u6d41\u51fa TOP", topOut ? topOut.name + " " + _sfAmt(topOut.net) : "\u2014", "out"],
    ["TOP6 \u5408\u8ba1\u6d41\u5165", sumIn ? _sfAmt(sumIn) : "\u2014", "in"],
    ["TOP6 \u5408\u8ba1\u6d41\u51fa", sumOut ? _sfAmt(sumOut) : "\u2014", "out"],
  ];
  $("sflowStats").innerHTML = chips.map(c =>
    '<div class="sf-chip"><div class="sf-chip-t">' + c[0] + '</div><div class="sf-chip-v ' + c[2] + '">' + c[1] + "</div></div>").join("");
}
function renderSflowLists(left, right, newsSec) {
  newsSec = newsSec || {};
  const row = (s, i) =>
    '<div class="sf-row"><span class="sf-rank">' + (i + 1) + '</span><span class="sf-name">' + s.name + (newsSec[s.name] ? ' <span class="sf-news" title="盘前新闻关联板块">📰</span>' : "") +
    '</span><span class="sf-pct ' + ((s.pct || 0) >= 0 ? "in" : "out") + '">' + ((s.pct || 0) >= 0 ? "+" : "") + (s.pct || 0).toFixed(2) +
    '%</span><span class="sf-net ' + ((s.net || 0) >= 0 ? "in" : "out") + '">' + _sfAmt(s.net) + "</span></div>";
  $("sflowInList").innerHTML = right.map(row).join("") || '<div class="tip" style="padding:8px">\u6682\u65e0\u6570\u636e</div>';
  $("sflowOutList").innerHTML = left.map(row).join("") || '<div class="tip" style="padding:8px">\u6682\u65e0\u6570\u636e</div>';
}
function renderSectorFlow(r) {
  const left = r.left || [], right = r.right || [], links = r.links || [];
  const newsSec = r.news_sectors || {};
  renderSflowStats(left, right);
  renderSflowLists(left, right, newsSec);
  renderSflowNewsHint(newsSec);
  const IN = "#e74c5a", OUT = "#2ecc71";
  const nodes = [];
  left.forEach(s => nodes.push({
    name: s.name, value: Math.abs(s.net) || 1,
    itemStyle: newsSec[s.name] ? { color: OUT, borderColor: "#f5c542", borderWidth: 2 } : { color: OUT },
    label: { position: "left", formatter: (newsSec[s.name] ? "📰 " : "") + s.name + "  " + _sfAmt(s.net), color: OUT },
  }));
  right.forEach(s => nodes.push({
    name: s.name, value: Math.abs(s.net) || 1,
    itemStyle: newsSec[s.name] ? { color: IN, borderColor: "#f5c542", borderWidth: 2 } : { color: IN },
    label: { position: "right", formatter: (newsSec[s.name] ? "📰 " : "") + s.name + "  " + _sfAmt(s.net), color: IN },
  }));
  const slinks = links
    .map(lk => {
      const L = left[lk.source], R = right[lk.target];
      if (!L || !R) return null;
      const v = Math.min(Math.abs(L.net), Math.abs(R.net)) || Math.abs(R.net) || 1;
      return { source: L.name, target: R.name, value: Math.max(v, 1) };
    })
    .filter(Boolean);
  sflowChart.setOption({
    backgroundColor: "transparent",
    tooltip: {
      trigger: "item", backgroundColor: "#1a2030", borderColor: "#2c3650",
      textStyle: { color: "#d6dde8" },
      formatter: p => {
        if (p.dataType === "edge") return p.data.source + " \u2192 " + p.data.target + "<br/>\u8d44\u91d1\u6d41\u8f6c " + fmtBig(p.data.value);
        const s = left.find(x => x.name === p.name) || right.find(x => x.name === p.name) || {};
        return "<b>" + p.name + "</b><br/>\u4e3b\u529b\u51c0\u6d41\u5165\uff1a" + _sfAmt(s.net) +
          "<br/>\u6da8\u8dcc\u5e45\uff1a" + (s.pct != null ? ((s.pct >= 0 ? "+" : "") + s.pct.toFixed(2) + "%") : "-") +
          "<br/>\u4e3b\u529b\u51c0\u5360\u6bd4\uff1a" + (s.net_pct != null ? s.net_pct.toFixed(2) + "%" : "-") +
          (newsSec[p.name] ? "<br/>📰 盘前新闻关联板块（" + newsSec[p.name].count + " 条新闻）" : "");
      },
    },
    series: [{
      type: "sankey", orient: "horizontal", nodeAlign: "justify",
      left: 150, right: 150, top: 12, bottom: 12,
      nodeWidth: 12, nodeGap: 14, layoutIterations: 0,
      emphasis: { focus: "adjacency" },
      data: nodes, links: slinks,
      itemStyle: { borderWidth: 0 },
      lineStyle: { color: "gradient", curveness: 0.5, opacity: 0.35 },
      label: { fontSize: 12 },
    }],
  }, true);
}

/* ============ ★ 4.7 盘前新闻 ============ */
let pnReady = false;
function _pnEsc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;" }[c]));
}
function startPnewsTimer() {
  clearTimers();
  if (!state.pnewsTimer) state.pnewsTimer = setInterval(() => refreshPnews(false), 60000);
}
function initPnews() {
  if (!pnReady) { pnReady = true; }
  refreshPnews(false);
}
async function refreshPnews(force) {
  const view = state.pnewsView || "list";
  try {
    $("pnStatus").textContent = "加载中…";
    if (view === "sector") {
      const rs = await fetchJson("/api/news/sectors");
      if (!rs || rs.error) { $("pnStatus").textContent = "获取失败: " + ((rs && rs.error) || "未知错误"); return; }
      renderPnewsSectors(rs);
      return;
    }
    const scope = state.pnewsScope || "all";
    const min = state.pnewsMin != null ? state.pnewsMin : 2;
    const r = await fetchJson("/api/news/premarket?scope=" + scope + "&min=" + min + (force ? "&force=1" : ""));
    if (r && r.disabled) {
      $("pnStatus").textContent = "未启用（config.PREMARKET_NEWS_ENABLED=False）";
      $("pnStats").textContent = "";
      $("pnList").innerHTML = "";
      return;
    }
    if (!r || r.error) { $("pnStatus").textContent = "获取失败: " + ((r && r.error) || "未知错误"); return; }
    renderPnews(r);
  } catch (e) {
    $("pnStatus").textContent = "获取异常: " + e;
  }
}
function renderPnews(r) {
  const items = r.items || [];
  const st = r.stats || {};
  let statTxt = "更新 " + (r.fetched_at || "");
  if (st.ai_pending) statTxt += " · AI 评分中…";
  else if (st.ai_used) statTxt += " · AI 已评分";
  $("pnStatus").textContent = statTxt;
  $("pnStats").textContent = "共 " + (st.total || 0) + " 条 / 入选 " + (st.kept || 0) + " 条 · 来源: " + (st.sources || []).join("+");
  if (!items.length) {
    $("pnList").innerHTML = '<div class="placeholder small">当前条件下暂无新闻，可降低重要度筛选或点刷新</div>';
    return;
  }
  $("pnList").innerHTML = items.map(it => {
    const sc = it.score | 0;
    const lvl = sc >= 4 ? "hi" : (sc >= 3 ? "mid" : "lo");
    const stocks = (it.stocks || []).slice(0, 6).map(s => '<span class="pn-stock">' + _pnEsc(s) + "</span>").join("");
    return '<div class="pn-item ' + lvl + '">'
      + '<div class="pn-head"><span class="pn-score">' + sc + "分</span>"
      + '<span class="pn-title">' + _pnEsc(it.title) + "</span>"
      + (it.cat ? '<span class="pn-cat">' + _pnEsc(it.cat) + "</span>" : "")
      + (it.ai ? '<span class="pn-cat" title="AI 评分">AI</span>' : "")
      + "</div>"
      + (it.text ? '<div class="pn-text">' + _pnEsc(it.text) + "</div>" : "")
      + '<div class="pn-foot"><span>' + _pnEsc(it.time || "") + '</span><span class="pn-src">' + _pnEsc(it.source || "") + "</span>"
      + (it.why ? '<span class="pn-why">' + _pnEsc(it.why) + "</span>" : "")
      + stocks + _pnSecChips(it.sectors) + "</div></div>";
  }).join("");
  document.querySelectorAll("#pnList .pn-item").forEach(el =>
    el.addEventListener("click", () => el.classList.toggle("expanded")));
}

/* ============ ★ 4.8 盘前简报 + 资金流新闻高亮 ============ */
function renderSflowNewsHint(newsSec) {
  const el = $("sflowNews");
  if (!el) return;
  const names = Object.keys(newsSec || {}).sort((a, b) => (newsSec[b].score || 0) - (newsSec[a].score || 0));
  if (!names.length) { el.innerHTML = ""; return; }
  el.innerHTML = "📰 盘前新闻关联板块：" + names.slice(0, 8).map(n => {
    const v = newsSec[n] || {};
    const bb = (v.bull || v.bear) ? (v.bull + "利好" + (v.bear ? "/" + v.bear + "利空" : "")) : (v.count + "条");
    return _pnEsc(n) + "(" + bb + ")";
  }).join(" · ");
}
let briefBusy = false;
function _briefDayStr(d) {
  return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
}
async function checkBrief(force) {
  try {
    const now = new Date();
    const day = _briefDayStr(now);
    const hm = now.getHours() * 100 + now.getMinutes();
    if (!force) {
      if (hm < 600 || hm > 940) return;
      if (localStorage.getItem("pnBriefDay") === day) return;
      const lastTry = parseInt(localStorage.getItem("pnBriefTry") || "0", 10);
      if (Date.now() - lastTry < 5 * 60 * 1000) return;
    }
    if (briefBusy) return;
    briefBusy = true;
    localStorage.setItem("pnBriefTry", String(Date.now()));
    const r = await fetchJson("/api/news/brief" + (force ? "?force=1" : ""));
    briefBusy = false;
    if (!r || r.error) return;
    if (!r.enabled) {
      if (force) showBriefEmpty(r.reason || "今日非交易日");
      return;
    }
    if (!r.items || !r.items.length) {
      if (force) showBriefEmpty("暂无达标新闻（重要度≥3），可稍后再试");
      return;
    }
    localStorage.setItem("pnBriefDay", day);
    showBriefModal(r);
  } catch (e) { briefBusy = false; }
}
function showBriefEmpty(reason) {
  $("briefMeta").textContent = "";
  $("briefBody").innerHTML = '<div class="placeholder small">' + _pnEsc(reason) + "</div>";
  $("briefModal").style.display = "flex";
}
function showBriefModal(r) {
  $("briefMeta").textContent = (r.day || "") + " · 更新 " + (r.fetched_at || "") + (r.ai_pending ? " · AI 评分中…" : "");
  $("briefBody").innerHTML = r.items.map(it => {
    const lvl = it.score >= 4 ? "hi" : (it.score >= 3 ? "mid" : "");
    return '<div class="brief-item ' + lvl + '"><span class="brief-score">' + it.score + '分</span><div class="brief-main">'
      + '<div class="brief-title">' + _pnEsc(it.title) + "</div>"
      + (it.why ? '<div class="brief-why">' + _pnEsc(it.why) + "</div>" : "")
      + '<div class="brief-meta"><span>' + _pnEsc(it.time || "") + "</span><span>" + _pnEsc(it.source || "") + "</span>"
      + (it.cat ? '<span class="brief-cat">' + _pnEsc(it.cat) + "</span>" : "")
      + "</div></div></div>";
  }).join("");
  $("briefModal").style.display = "flex";
}
$("btnBriefClose").addEventListener("click", () => $("briefModal").style.display = "none");
$("btnBriefOk").addEventListener("click", () => $("briefModal").style.display = "none");
$("btnBriefGo").addEventListener("click", () => { $("briefModal").style.display = "none"; switchPage("pnews"); });
const _pnBriefBtn = $("pnBriefBtn");
if (_pnBriefBtn) _pnBriefBtn.addEventListener("click", () => checkBrief(true));
setTimeout(() => checkBrief(false), 2500);
setInterval(() => checkBrief(false), 30000);

/* ============ ★ 4.9 新闻板块视图（利好利空） ============ */
function _pnSecChips(secs) {
  return (secs || []).slice(0, 4).map(s => {
    const cls = s.dir > 0 ? "bull" : (s.dir < 0 ? "bear" : "flat");
    const arrow = s.dir > 0 ? "▲" : (s.dir < 0 ? "▼" : "");
    return '<span class="sec-chip ' + cls + '">' + _pnEsc(s.name) + arrow + "</span>";
  }).join("");
}
function renderPnewsSectors(r) {
  const secs = r.sectors || [];
  let statTxt = "更新 " + (r.fetched_at || "");
  if (r.ai_pending) statTxt += " · AI 评分中…";
  else if (r.ai_used) statTxt += " · AI 已评分";
  $("pnStatus").textContent = statTxt;
  const nb = secs.reduce((a, s) => a + (s.bull || 0), 0);
  const nk = secs.reduce((a, s) => a + (s.bear || 0), 0);
  $("pnStats").textContent = secs.length + " 个板块 · " + nb + " 利好 / " + nk + " 利空（仅含关联板块的新闻）";
  if (!secs.length) {
    $("pnList").innerHTML = '<div class="placeholder small">暂无关联板块的新闻，可切回「列表」视图查看或稍后重试</div>';
    return;
  }
  $("pnList").innerHTML = secs.map((s, i) => {
    const netCls = s.net > 0 ? "bull" : (s.net < 0 ? "bear" : "flat");
    const netTxt = s.net > 0 ? ("净利好 +" + s.net) : (s.net < 0 ? ("净利空 " + s.net) : "中性");
    const rows = (s.news || []).map(n => {
      const dCls = n.dir > 0 ? "bull" : (n.dir < 0 ? "bear" : "flat");
      const dTxt = n.dir > 0 ? "▲ 利好" : (n.dir < 0 ? "▼ 利空" : "· 相关");
      return '<div class="ps-row"><span class="ps-dir ' + dCls + '">' + dTxt + '</span>'
        + '<span class="ps-score">' + n.score + "</span>"
        + '<div class="ps-main"><div class="ps-title">' + _pnEsc(n.title) + "</div>"
        + (n.why ? '<div class="ps-why">' + _pnEsc(n.why) + "</div>" : "")
        + '<div class="ps-meta">' + _pnEsc(n.time || "") + " · " + _pnEsc(n.source || "") + "</div></div></div>";
    }).join("");
    return '<div class="ps-card' + (i < 3 ? " open" : "") + '">'
      + '<div class="ps-head"><span class="ps-name">' + _pnEsc(s.name) + "</span>"
      + (s.bull ? '<span class="ps-badge bull">' + s.bull + " 利好</span>" : "")
      + (s.bear ? '<span class="ps-badge bear">' + s.bear + " 利空</span>" : "")
      + (s.flat ? '<span class="ps-badge flat">' + s.flat + " 相关</span>" : "")
      + '<span class="ps-net ' + netCls + '">' + netTxt + "</span>"
      + '<span class="tip">' + s.count + " 条</span></div>"
      + '<div class="ps-body">' + rows + "</div></div>";
  }).join("");
  document.querySelectorAll("#pnList .ps-card .ps-head").forEach(h =>
    h.addEventListener("click", () => h.parentElement.classList.toggle("open")));
}
document.querySelectorAll("#pnViewTabs .tab").forEach(el =>
  el.addEventListener("click", () => {
    document.querySelectorAll("#pnViewTabs .tab").forEach(tb => tb.classList.remove("active"));
    el.classList.add("active");
    state.pnewsView = el.dataset.view;
    refreshPnews();
  }));


/* ============ 🎯 战法选股（T1 + T2 v2：名称列 / 连板梯队全展示 / 竞价双战法）============ */
let tctTimer = null;
let tctTab = "shouban";
let tctLadder = "全部";
let tctData = null;
// T2：四个 tab → tactics 数组下标（server 禁改，/api/tactics 固定返回
// [首板回调, 连板梯队, 竞价打板, 竞价弱转强] 四卡）
const TCT_TAB_IDX = { shouban: 0, lianban: 1, jingjia_daban: 2, jingjia_w2s: 3 };
const TCT_STAGE_CLS = {
  "首板确认": "tct-pass", "买点触发(收盘买)": "tct-hit", "持有中(等≥9%大阳,第j日)": "tct-run",
  "兑现-未板落袋": "tct-pass", "兑现-封板格局(次日开盘卖)": "tct-pass",
  "失效(14日)": "tct-fail", "不在池": "tct-dim", "不在梯队": "tct-dim",
  "首板缩量(候选)": "tct-hit", "首板放量(不候选)": "tct-dim",
  "已打板(隔夜持有)": "tct-run", "二板兑现": "tct-pass", "断板(次日开盘已卖)": "tct-fail",
  "一字(买不进)": "tct-fail",
  /* T2 连板梯队全展示（观察系一律灰，绝不复用候选金） */
  "一进二候选(首板缩量)": "tct-hit", "一进二不候选(放量或一字)": "tct-dim",
  "二进三(仅观察)": "tct-obs", "三进四(仅观察)": "tct-obs", "四进五(仅观察)": "tct-obs",
  "五板+(仅观察)": "tct-obs",
  /* T2 竞价双战法 */
  "竞价候选(≥40分)": "tct-hit", "竞价观察": "tct-dim", "竞价快照缺失(降级)": "tct-warn",
  "弱转强观察": "tct-obs", "弱转强未触发": "tct-dim"
};
// T2：ladder 与后端 _ladder_of 对齐（lbc>=5 合并"五板+"，断板单独档）
const TCT_LADDERS = ["全部", "一进二", "二进三", "三进四", "四进五", "五板+", "断板"];
function startTacticsTimer() {
  clearTimers();
  if (!tctTimer) tctTimer = setInterval(() => { if (state.page === "tactics") loadTactics(); }, 60000);
}
async function loadTactics() {
  const errEl = $("tct-err"), meta = $("tct-meta"), cov = $("tct-coverage");
  if (!errEl) return;
  try {
    const d = await fetchJson("/api/tactics");
    tctData = d;
    if (d && d.error) {
      errEl.textContent = "服务错误: " + d.error;
      tctData = null; renderTacticsEmpty();
      return;
    }
    errEl.textContent = "";
    const c = (d && d.coverage) || {};
    meta.textContent = (d && d.generated_at) ? "更新: " + d.generated_at : "";
    if (c.latest_date) {
      let t = "数据日: " + c.latest_date + " · " + (c.n_codes === undefined ? "?" : c.n_codes) + " 只";
      // R2-P0.2：红条判定升级——complete=False（<1500）或 ratio<0.9（缺口>10%）
      // 都提示。原 09-03 379 缺口 ratio≈0.84 不再静默。
      const incompl = (c.complete === false) ||
        (c.ratio !== undefined && c.total && c.ratio < 0.9);
      if (incompl) t += " ⚠数据不完整日(尾部日更缺口)";
      cov.textContent = t;
      cov.className = "tip" + (incompl ? " warn" : "");
    } else {
      cov.textContent = "";
    }
    renderTactics();
  } catch (e) {
    errEl.textContent = "服务不可用，请稍后重试";
    tctData = null;
    renderTacticsEmpty();
  }
}
function renderTacticsEmpty() {
  const hd = $("tct-head"), tb = $("tct-tbody");
  if (hd) hd.innerHTML = "<th>代码</th><th>名称</th><th>得分</th><th>阶段</th><th>明细</th>";
  if (tb) tb.innerHTML = '<tr><td colspan="5" style="color:#888">暂无数据（服务不可用或空）</td></tr>';
  const ld = $("tct-ladders");
  if (ld) ld.style.display = "none";
  const nt = $("tct-notice");
  if (nt) nt.style.display = "none";
}
function renderTactics() {
  if (!tctData || !(tctData.tactics || []).length) { renderTacticsEmpty(); return; }
  const tac = tctData.tactics;
  const t = tac[TCT_TAB_IDX[tctTab]] || tac[0];
  const isLb = t.name === "连板梯队";
  const isJingjia = (t.name === "竞价打板" || t.name === "竞价弱转强");
  // 连板子板块按钮（仅连板梯队）
  const ld = $("tct-ladders");
  if (ld) {
    if (isLb) {
      ld.style.display = "";
      ld.innerHTML = "";
      TCT_LADDERS.forEach(name => {
        const b = document.createElement("button");
        b.className = "exp-tab" + (tctLadder === name ? " active" : "");
        b.textContent = name;
        b.addEventListener("click", () => { tctLadder = name; renderTactics(); });
        ld.appendChild(b);
      });
    } else {
      ld.style.display = "none";
    }
  }
  // 竞价双战法诚实表头提示（T2 §4c：无 IS/OOS 回测卡，禁止渲染成"已验收"视觉）
  const nt = $("tct-notice");
  if (nt) {
    if (isJingjia) {
      nt.style.display = "";
      nt.textContent = (t.honest_note || "竞价双战法为系统现役评分器的公示参考，未做过 W1R/V1 级回测验收")
        + (tctTab === "jingjia_w2s" ? " · 弱转强研究结论 P34=不成立不集成，仅作观察" : "");
      nt.className = "tct-honest-note";
    } else {
      nt.style.display = "none";
    }
  }
  // 表头
  const hd = $("tct-head");
  let keyLabel = "关键信号";
  if (isLb) keyLabel = "lbc·板型·量比";
  else if (t.name === "竞价打板") keyLabel = "高开·量比·竞价额";
  else if (t.name === "竞价弱转强") keyLabel = "高开缺口";
  const heads = ["代码", "名称", "得分", "阶段", keyLabel, "明细"];
  hd.innerHTML = "";
  heads.forEach(h => {
    const th = document.createElement("th");
    if (isLb && h === "lbc·板型·量比") {
      th.textContent = h + "  ⚠20cm(30/68)未单独验证";
      th.title = "规则未在 20cm(30/68) 单独验证，30/68 候选行加 ⚠ 徽章——数据缺口如实展示，不改判据";
    } else {
      th.textContent = h;
    }
    hd.appendChild(th);
  });
  // 行（连板按子板块过滤）
  const tb = $("tct-tbody");
  tb.innerHTML = "";
  let rows = (t.statuses || []).slice();
  if (isLb && tctLadder !== "全部") {
    rows = rows.filter(s => (s.ladder || "观察") === tctLadder);
  }
  rows.forEach(s => tb.appendChild(tacticsRow(isLb, isJingjia, s)));
  if (rows.length === 0) {
    tb.innerHTML = '<tr><td colspan="6" style="color:#888">该梯队/时段暂无候选</td></tr>';
  }
  const meta = $("tct-meta");
  if (t.stages_summary) {
    const s = Object.keys(t.stages_summary).map(k => k + " " + t.stages_summary[k]).join(" / ");
    if (meta) meta.textContent += " · 规则: " + t.rule_source + " · " + s;
  }
}
function tacticsRow(isLb, isJingjia, s) {
  const tr = document.createElement("tr");
  const obsStage = /(仅观察)|观察/.test(s.stage || "");
  // 代码（20cm 标注）
  const tdC = document.createElement("td");
  tdC.appendChild(document.createTextNode(s.code));
  if (isLb && /^(30|68)/.test(s.code)) {
    const warn = document.createElement("span");
    warn.textContent = " ⚠";
    warn.className = "tct-warn";
    warn.title = "30/68 属 20cm 板，规则未单独验证";
    tdC.appendChild(warn);
  }
  tr.appendChild(tdC);
  // 名称（T2 新需求1）
  const tdN = document.createElement("td");
  tdN.textContent = s.name || "";
  tdN.className = "tct-name";
  tr.appendChild(tdN);
  // 得分
  const tdSc = document.createElement("td");
  const sc = (s.score !== undefined && s.score !== null) ? Number(s.score).toFixed(1) : "-";
  tdSc.textContent = sc;
  tdSc.style.fontWeight = "600";
  // T2：观察系一律灰，绝不复用候选金/绿
  tdSc.style.color = obsStage ? "#7c8595"
    : (Number(s.score) >= 70 ? "#5ce08c" : (Number(s.score) >= 40 ? "#ffd27a" : "#7c8595"));
  tr.appendChild(tdSc);
  // 阶段徽章
  const tdS = document.createElement("td");
  const badge = document.createElement("span");
  badge.className = "tct-badge " + (TCT_STAGE_CLS[s.stage] || "tct-dim");
  badge.textContent = s.stage;
  tdS.appendChild(badge);
  tr.appendChild(tdS);
  // 关键列
  const tdK = document.createElement("td");
  if (isLb) {
    const lbc = (s.lbc !== undefined ? s.lbc : "-");
    const bt = (s.board_type || "none");
    const ar = (s.amount_ratio !== null && s.amount_ratio !== undefined)
      ? Number(s.amount_ratio).toFixed(2) : "-";
    tdK.textContent = lbc + "板 · " + bt + " · 量比" + ar;
  } else if (tctTab === "jingjia_daban") {
    const ap = (s.apct !== undefined) ? Number(s.apct).toFixed(1) + "%" : "-";
    const vr = (s.vr !== undefined) ? Number(s.vr).toFixed(2) : "-";
    const am = (s.amount !== undefined) ? (Number(s.amount) / 1e4).toFixed(0) + "万" : "-";
    tdK.textContent = "高开" + ap + " · 量比" + vr + " · 竞价额" + am;
  } else if (tctTab === "jingjia_w2s") {
    const gp = (s.gap_pp !== undefined) ? Number(s.gap_pp).toFixed(1) + "%" : "-";
    tdK.textContent = "高开" + gp;
  } else {
    tdK.textContent = (s.signals && s.signals.length)
      ? s.signals.map(x => x.name + ":" + x.value).join(" · ") : "-";
  }
  tr.appendChild(tdK);
  // 明细
  const tdD = document.createElement("td");
  tdD.textContent = s.detail || "";
  tr.appendChild(tdD);
  return tr;
}
document.querySelectorAll("#tct-tabs .exp-tab").forEach(el =>
  el.addEventListener("click", () => {
    document.querySelectorAll("#tct-tabs .exp-tab").forEach(tb => tb.classList.remove("active"));
    el.classList.add("active");
    tctTab = el.dataset.tactic;
    tctLadder = "全部";
    renderTactics();
  }));

/* ============ 数据健康面板（D3 2026-09-13）============
 * 只读 /api/data_health；三态：ok 绿 / warn 黄 / err 红。
 * stale=true（库锁/断库降级）→ 整体灰显并显示"上次缓存"时间。
 * 判级规则（预注册，与报告 §判级 一致）：
 *   覆盖率 ratio>=0.95 ok / >=0.85 warn / else err
 *   amount 缺失 ratio==0 ok / <=0.05 warn / else err
 *   快照 deferred→err；age<=3 ok / <=7 warn / else err
 *   指数 任一 age>5 err / 任一 age>3 warn / amount 不可用→warn
 *   ML 打分 age<=7 ok / <=15 warn / >15 err（ml_scores 与 ml_pred 同规）
 *   告警 CRIT>0→err；WARN>3→err；WARN>1→warn；else ok
 */
const DH_COL = { ok: "#27ae60", warn: "#f39c12", err: "#e74c3c" };

function dhPct(x) { return x == null ? "—" : (x * 100).toFixed(1) + "%"; }

function dhCard(title, level, bodyHtml, reason) {
  const c = DH_COL[level] || "#888";
  return `<div style="border:1px solid ${c}55;border-left:4px solid ${c};border-radius:8px;padding:10px 12px;background:#1a1d24;">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
      <b style="font-size:13px">${title}</b>
      <span style="font-size:11px;color:${c};font-weight:600">${level === "ok" ? "正常" : level === "warn" ? "注意" : "异常"}</span>
    </div>
    ${bodyHtml}
    ${reason ? `<div style="margin-top:6px;font-size:12px;color:${c}">⚠ ${reason}</div>` : ""}
  </div>`;
}

function dhBar7(trend, valFn, labelFn) {
  if (!trend || !trend.length) return `<div style="font-size:12px;color:#888">无 7 日数据</div>`;
  const max = Math.max.apply(null, trend.map(t => valFn(t) || 0)) || 1;
  return `<div style="display:flex;align-items:flex-end;gap:4px;height:44px;padding-top:4px">
    ${trend.map(t => {
      const v = valFn(t) || 0;
      const h = Math.round(v / max * 36) + 2;
      return `<div style="flex:1;text-align:center">
        <div style="height:${h}px;background:#4a6fa5;border-radius:2px;min-width:6px"></div>
        <div style="font-size:9px;color:#999;margin-top:2px">${labelFn(t)}</div>
      </div>`;
    }).join("")}
  </div>`;
}

function dhHealthCards(d) {
  const cards = [];
  // 1) 日K覆盖率
  const kc = d.kline_coverage || {};
  const kt = (kc.trend7 || []).slice(-1)[0] || {};
  const kr = kt.ratio;
  const kl = kr == null ? "warn" : (kr >= 0.95 ? "ok" : (kr >= 0.85 ? "warn" : "err"));
  const kReason = kl === "err" ? `覆盖率 ${dhPct(kr)} < 85%（最新交易日 ${kt.date || kc.latest_date || "—"}）`
    : kl === "warn" ? (kr == null ? "覆盖率分母计算中（后台线程）" : `覆盖率 ${dhPct(kr)}，注意`)
    : null;
  cards.push(dhCard("日K覆盖率", kl, `
    <div style="font-size:22px;font-weight:700">${dhPct(kr)}</div>
    <div style="font-size:12px;color:#aaa">最新 ${kt.date || kc.latest_date || "—"}：${kt.rows ?? "—"}/${kc.total_codes ?? "—"} 只
      · 今日(自然日 ${d.today})${kc.today_rows ?? 0} 行</div>
    ${dhBar7(kc.trend7, t => t.rows, t => (t.date || "").slice(5))}`, kReason));

  // 2) amount 完整性
  const am = d.amount || {};
  const at = (am.trend7 || []).slice(-1)[0] || {};
  const ar = at.amount_ratio;
  const al = ar == null ? "ok" : (ar === 0 ? "ok" : (ar <= 0.05 ? "warn" : "err"));
  const aReason = al === "err" ? `amount=0 占比 ${dhPct(ar)} > 5%（缺成交额源）`
    : al === "warn" ? `amount=0 占比 ${dhPct(ar)} ≤ 5%`
    : null;
  cards.push(dhCard("amount 完整性", al, `
    <div style="font-size:22px;font-weight:700">${dhPct(ar)}</div>
    <div style="font-size:12px;color:#aaa">最新 ${at.date || am.latest_date || "—"}：amount=0 ${at.amount0 ?? "—"}/${at.rows ?? "—"} 行</div>
    ${dhBar7(am.trend7, t => t.amount0, t => (t.date || "").slice(5))}`, aReason));

  // 3) 快照
  const sn = d.snapshots || {};
  const sl = sn.deferred ? "err" : (sn.age_days == null ? "err" : (sn.age_days <= 3 ? "ok" : (sn.age_days <= 7 ? "warn" : "err")));
  const sReason = sn.deferred ? (sn.deferred_reason || "快照延迟") : null;
  cards.push(dhCard("快照状态", sl, `
    <div style="font-size:22px;font-weight:700">${sn.latest || "无快照"}</div>
    <div style="font-size:12px;color:#aaa">距今天数 ${sn.age_days == null ? "—" : sn.age_days + " 天"} · 共 ${sn.count ?? 0} 枚</div>
    <div style="margin-top:6px;font-size:11px;color:#aaa">最近 7 日：${(sn.last7 || []).map(x => `${x.date.slice(5)}${x.exists ? "●" : "○"}`).join(" ")}</div>`, sReason));

  // 4) 指数新鲜度
  const ix = (d.indices || {}).items || {};
  let iErr = 0, iWarn = 0, iRows = [];
  Object.keys(ix).forEach(code => {
    const it = ix[code] || {};
    const age = it.age_days;
    if (age == null) iErr++;
    else if (age > 5) iErr++;
    else if (age > 3) iWarn++;
    if (it.amount_available === false) iWarn++;
    iRows.push(`<div style="font-size:12px;color:#ccc;margin:2px 0">${code}：${it.last_date || "—"}（${age == null ? "—" : age + "天"}）${it.amount_available === false ? "· amount 不可用" : ""}</div>`);
  });
  const il = iErr ? "err" : (iWarn ? "warn" : "ok");
  const iReason = iErr ? "指数日K落后超过 5 天或有缺失" : (iWarn ? "指数有注意项" : null);
  cards.push(dhCard("指数新鲜度", il, `
    <div style="font-size:13px;font-weight:700">${Object.keys(ix).length} 个指数</div>
    ${iRows.join("")}`, iReason));

  // 5) ML 新鲜度
  const ml = d.ml || {};
  const m1 = ml.ml_scores || {};
  const m2 = ml.ml_pred || {};
  function mlLevel(age) { return age == null ? "warn" : (age <= 7 ? "ok" : (age <= 15 ? "warn" : "err")); }
  const m1l = mlLevel(m1.age_days), m2l = mlLevel(m2.age_days);
  const mlL = (m1l === "err" || m2l === "err") ? "err" : ((m1l === "warn" || m2l === "warn") ? "warn" : "ok");
  const mlReasons = [];
  if (m1l === "err") mlReasons.push(`ML 打分 ${m1.signal_date || "—"} 已 ${m1.age_days} 天未更新`);
  if (m2l === "err") mlReasons.push(`ml_pred 预测 ${m2.last_pred_date || "—"} 已 ${m2.age_days} 天未更新`);
  cards.push(dhCard("ML 新鲜度", mlL, `
    <div style="font-size:13px;font-weight:700">打分日期 ${m1.signal_date || "—"}</div>
    <div style="font-size:12px;color:#aaa">ml_scores：${m1.signal_date || "无"}（${m1.age_days == null ? "—" : m1.age_days + "天"}）· ${m1.n_scores ?? 0} 只
      <br>ml_pred：${m2.last_pred_date || "无"}（${m2.age_days == null ? "—" : m2.age_days + "天"}）</div>`,
    mlReasons.join("；") || null));

  // 6) 未处置告警
  const al7 = d.alerts || {};
  const alL = (al7.critical || 0) > 0 ? "err" : ((al7.warn || 0) > 3 ? "err" : ((al7.warn || 0) > 1 ? "warn" : "ok"));
  const a7Reason = (al7.critical || 0) > 0 ? `近7天 CRITICAL ${al7.critical} 条` : ((al7.warn || 0) > 1 ? `近7天 WARN ${al7.warn} 条` : null);
  cards.push(dhCard("未处置告警", alL, `
    <div style="font-size:22px;font-weight:700">${al7.warn || 0} WARN · ${al7.critical || 0} CRIT</div>
    <div style="font-size:12px;color:#aaa;margin-top:4px">${(al7.recent || []).slice(0, 4).map(r =>
      `<div style="margin:2px 0">${(r.date || "").slice(5, 16)} [${r.level}] ${r.event}${r.target_day ? " →" + r.target_day : ""}</div>`).join("") || "近7天无告警"}</div>`, a7Reason));

  // 7) 存储
  const st = d.storage || {};
  const stL = st.error ? "warn" : "ok";
  const mb = x => x == null ? "统计中" : (x / 1048576).toFixed(0) + " MB";
  cards.push(dhCard("存储占用", stL, `
    <div style="font-size:20px;font-weight:700">${mb(st.data_total_bytes)}</div>
    <div style="font-size:12px;color:#aaa">快照 ${mb(st.snapshots_bytes)} · 近7日增量 ${mb(st.growth7_bytes)}
      <br><span style="color:#777;font-size:11px">${st.growth7_note || ""}</span></div>`, st.error));
  return { cards, levels: [kl, al, sl, il, mlL, alL, stL] };
}

async function refreshDataHealth() {
  const el = $("dhCards");
  try {
    const d = await fetchJson("/api/data_health");
    $("dhUpdated").textContent = "更新时间 " + d.generated_at + (d.stale ? "（降级）" : "");
    const r = dhHealthCards(d);
    // 总状态 = 最差
    const order = ["ok", "warn", "err"];
    const worst = order[Math.max.apply(null, r.levels.map(l => order.indexOf(l)))];
    const c = DH_COL[worst];
    const staleBadge = d.stale ? `<span style="color:#888;font-size:12px;margin-left:8px">⚠ 库连接降级，显示上次缓存 ${d.generated_at}（${d.stale_reason || ""}）</span>` : "";
    $("dhStatus").innerHTML = `<div style="display:inline-block;padding:6px 14px;border-radius:20px;font-weight:700;color:#fff;background:${c}">
      ${worst === "ok" ? "🟢 数据健康" : worst === "warn" ? "🟡 有注意项" : "🔴 数据异常"}</div>${staleBadge}`;
    el.innerHTML = r.cards.join("");
    $("dhError").style.display = "none";
  } catch (e) {
    $("dhStatus").innerHTML = `<div style="display:inline-block;padding:6px 14px;border-radius:20px;font-weight:700;color:#fff;background:#888">⚪ 数据健康（断网/服务不可用）</div>`;
    el.innerHTML = `<div style="grid-column:1/-1;font-size:13px;color:#888;padding:20px;border:1px dashed #555;border-radius:8px">
      无法连接服务（${e.message}）。前端无上次缓存时显示此占位，不报错。</div>`;
  }
}
