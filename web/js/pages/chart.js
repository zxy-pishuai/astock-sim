/* ===== J5 chart：从 app.js 594-909 行切分（函数体逐字保留，window 桥接全局） ===== */
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
  if (window.startChartDepth) startChartDepth(state.code);   // ★D-S4 十档跟随
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

Object.assign(window, { loadChart, renderChart, loadMoneyflow, renderKline, minuteAxisTemplate, renderMinute, renderInfo, renderScore, startLiveTimer, openTrade });
