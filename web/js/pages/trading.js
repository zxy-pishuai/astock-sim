/* ===== J5 trading：从 app.js 132-458 行切分（函数体逐字保留，window 桥接全局） ===== */
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
    setHtml($("trMarket"), [
      ["市场环境", regIcons[o.regime] + " " + o.regime],
      ["上涨占比", (o.breadth * 100).toFixed(0) + "%"],
      ["允许仓位", o.max_pos + " 仓"],
      ["买入阈值", o.threshold + " 分"],
      ["持仓", s.positions + " 只"],
      ["现金", "¥" + fmt(s.cash, 0)],
    ].map(x => `<div><span>${x[0]}</span><b>${x[1]}</b></div>`).join(""));
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
      setHtml($("trSentiment"), rows.map(x =>
        `<div><span>${x[0]}</span><b class="${x[2] || ""}">${x[1]}</b></div>`).join(""));
      // 连板梯队
      const ladder = se.ladder || {};
      const promo = se.promotion || {};
      const ladderHtml = Object.keys(ladder).sort((a, b) => b - a).map(n =>
        `<div class="audit-item">${n}板 ×${ladder[n]}${promo[n + "进" + (parseInt(n) + 1)] != null ? ` · 晋级${(promo[n + "进" + (parseInt(n) + 1)] * 100).toFixed(0)}%` : ""}</div>`).join("");
      setHtml($("trLadder"), ladderHtml || '<div class="placeholder small">今日无涨停</div>');
      loadSentimentGate();   // Phase16：情绪仓位闸门卡片
      // 4.1 题材爆发度（涨停原因主题词）
      const themes = se.themes || se.sector_burst || {};
      const themeEntries = Object.entries(themes);
      setHtml($("trThemes"), themeEntries.length
        ? themeEntries.slice(0, 8).map(([t, c]) =>
            `<div class="audit-item">🔥 ${t} <b style="color:var(--gold)">×${c}</b></div>`).join("")
        : '<div class="placeholder small">题材数据加载中…</div>');
      // ★ 4.5 新闻情绪（场外情绪维度，异步加载）
      loadNewsSentiment();
      // ★ 4.5 业绩预增榜（异步加载，不阻塞情绪面板）
      loadEarningsBoard();
    } else {
      setHtml($("trSentiment"), '<div class="placeholder small">情绪数据加载中…</div>');
      setHtml($("trLadder"), "");
      setHtml($("trThemes"), "");
    }
    // 强势板块
    const sec = await fetchJson("/api/sectors");
    $("sectorTip").textContent = sec.map_ready ? "（真实板块映射已加载）" : "（映射加载中…）";
    setHtml($("trSectors"), sec.top.map(x => `
      <div class="sector-item">
        <span class="s-name">${x.name}</span>
        <span class="s-count">${x.count}只</span>
        <span class="s-pct up">+${x.avg_pct}%</span>
        <span class="s-leader">龙头:${x.leader_name || "—"} ${x.leader_pct ? "(" + (x.leader_pct > 0 ? "+" : "") + x.leader_pct + "%)" : ""}</span>
      </div>`).join("") || '<div class="placeholder small">板块数据加载中…</div>');
    // 竞价公示
    const au = s.auction || {};
    if (au.results && au.results.length) {
      setHtml($("trAuction"), `<div class="auction-head">${au.time} 共 ${au.results.length} 只候选（大盘竞价高开 ${((au.breadth || 0) * 100).toFixed(0)}%）</div>` +
        au.results.map((r, i) => `
        <div class="auction-item" data-code="${r.code}">
          <span class="a-rank">${i + 1}</span>
          <span class="a-name">${r.name}</span>
          <span class="a-code">${r.code}</span>
          <span class="a-pct up">${r.auction_pct > 0 ? "+" : ""}${r.auction_pct}%</span>
          <span class="a-score">${r.score}分</span>
          <span class="a-sec">${r.sector}</span>
        </div>`).join(""));
      $("trAuction").querySelectorAll(".auction-item").forEach(el =>
        el.addEventListener("click", () => { state.code = el.dataset.code; switchPage("chart"); }));
    } else {
      setHtml($("trAuction"), s.in_auction_time
        ? '<div class="placeholder small">⏳ 竞价窗口扫描中…</div>'
        : '<div class="placeholder small">等待 9:25 竞价窗口（启动引擎后自动扫描）</div>');
    }
    // ★ v3.4 两点半战法候选
    const tt = s.twothirty || {};
    if (tt.results && tt.results.length) {
      setHtml($("trTwothirty"), `<div class="auction-head">${tt.time} ${tt.msg}</div>` +
        tt.results.map((r, i) => `
        <div class="auction-item" data-code="${r.code}">
          <span class="a-rank">${i + 1}</span>
          <span class="a-name">${r.name}</span>
          <span class="a-code">${r.code}</span>
          <span class="a-pct up">${r.pct > 0 ? "+" : ""}${fmt(r.pct, 1)}%</span>
          <span class="a-score">${r.score}分</span>
          <span class="a-sec" title="${(r.signals || []).join("、")}">${(r.signals || []).slice(0, 2).join("、")}</span>
        </div>`).join(""));
      $("trTwothirty").querySelectorAll(".auction-item").forEach(el =>
        el.addEventListener("click", () => { state.code = el.dataset.code; switchPage("chart"); }));
    } else {
      setHtml($("trTwothirty"), tt.time
        ? '<div class="placeholder small">当日已扫描，暂无候选（涨2~7% + MACD金叉 + 量比>1）</div>'
        : '<div class="placeholder small">14:20-14:35 自动扫描（启动引擎）；也可点下方手动扫描</div>');
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
    setHtml($("trRisk"), rkRows.map(x => `<div><span>${x[0]}</span><b>${x[1]}</b></div>`).join(""));
    // ★ v3.7 行业敞口
    const exp = rk.sector_exposure || {};
    const expKeys = Object.keys(exp);
    setHtml($("trSectorExp"), expKeys.length
      ? expKeys.map(sec => {
          const d = exp[sec];
          return `<div class="sector-item ${d.over ? "over" : ""}">
            <span class="s-name">${sec}</span>
            <span class="s-count">${d.codes.length}只</span>
            <span class="s-pct ${d.pct > 0.5 ? "up" : ""}">${(d.pct * 100).toFixed(1)}%${d.over ? " ⚠超限" : ""}</span>
          </div>`;
        }).join("")
      : '<div class="placeholder small">当前空仓或无需监控</div>');
    // ★ v3.7 相关性告警
    const corr = rk.corr_alerts || [];
    setHtml($("trCorrAlerts"), corr.length
      ? corr.map(a => `<div class="audit-item" style="color:var(--gold)">⚠️ ${a.msg}</div>`).join("")
      : '<div class="placeholder small">持仓间无高风险相关性</div>');
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
    setHtml($("trScanTime"), (s.last_scan_time ? "上次扫描 " + s.last_scan_time : "未扫描") + " | " + (s.last_scan_msg || "") + regHtml);
    const tb = $("trScanTable").querySelector("tbody");
    setHtml(tb, (s.last_scan || []).map((r, i) => `
      <tr data-code="${r.code}">
        <td>${i + 1}</td><td class="clickable">${r.code}</td><td>${r.name}</td>
        <td class="num">${fmt(r.price)}</td><td class="num ${cls(r.pct_chg)}">${r.pct_chg > 0 ? "+" : ""}${fmt(r.pct_chg, 1)}%</td>
        <td class="num gold">${r.score}分</td>
        <td class="sig-cell" title="${(r.signals || []).join("、")}">${(r.signals || []).slice(0, 3).join("、")}</td>
      </tr>`).join(""));
    tb.querySelectorAll("tr").forEach(tr =>
      tr.addEventListener("click", () => { state.code = tr.dataset.code; switchPage("chart"); }));
    // 自选监控
    const wv = $("trWatch");
    if (s.watch_events && s.watch_events.length) {
      const icons = { "拉升": "🔥", "大跌": "🚨", "回落": "⚠️" };
      setHtml(wv, s.watch_events.slice().reverse().map(e =>
        `<div class="ev ${e.level.toLowerCase()}"><span class="ev-t">${e.t}</span>${icons[e.kind] || ""} ${e.msg}</div>`).join(""));
    } else {
      setHtml(wv, '<div class="placeholder small">启动引擎后，自选股异动（拉升/大跌/冲高回落）实时显示于此</div>');
    }
    // 事件
    const ev = $("trEvents");
    setHtml(ev, (s.events || []).slice().reverse().map(e =>
      `<div class="ev ${e.level.toLowerCase()}"><span class="ev-t">${e.t}</span>${e.msg}</div>`).join("") || '<div class="placeholder small">暂无事件</div>');
    ev.scrollTop = 0;
  } catch (e) { /* 静默 */ }
}
async function startTrTimer() {
  if (state.trTimer) clearInterval(state.trTimer);
  await TimerHub.ensureCal();
  state.trTimer = setInterval(refreshTrading, TimerHub.intervalFor("tr", 3000));
}

/* ★ Phase16 情绪仓位闸门卡片 */
async function loadSentimentGate() {
  try {
    const el = $("trGate");
    const tip = $("trGateTip");
    if (!el) return;
    // ★ K9：加载中骨架屏；pending 占位由 fetchJson 自动重试
    uiShowSkeleton(el);
    let d;
    try {
      d = await fetchJson("/api/sentiment/gate");
    } catch (e) {
      uiClearSkeleton(el);
      el.innerHTML = '<div class="placeholder small">闸门数据暂不可用</div>';
      // ★ K9：重试耗尽 → err-banner + 点此重试，不留空白（原为静默）
      uiShowErrBanner(el, () => loadSentimentGate());
      return;
    }
    uiClearSkeleton(el);
    if (d.error) { setHtml(el, '<div class="placeholder small">闸门数据不可用</div>'); return; }
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
    setHtml(el, board.length
      ? board.slice(0, 8).map(x => `
          <div class="audit-item earn-item" data-code="${x.code}">
            <span class="e-name">${x.name}</span>
            <span class="e-code">${x.code}</span>
            <b style="color:var(--up)">+${x.amp_lower != null ? x.amp_lower : "?"}%</b>
            <span class="e-date">${(x.notice_date || "").slice(5)}</span>
          </div>`).join("")
      : '<div class="placeholder small">近期无业绩预增公告</div>');
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
  setHtml(el, `<div class="ai-badge">${prov}</div><div class="ai-text">${txt}</div>`);
}
$("btnAiMorning").addEventListener("click", async () => {
  const el = $("trAi");
  setHtml(el, '<div class="placeholder small">🌅 AI 正在生成盘前晨报…（约10-30秒）</div>');
  try {
    renderAiReport(el, await fetchJson("/api/ai/morning"));
  } catch (e) { setHtml(el, '<div class="placeholder small">晨报生成失败: ' + e.message + '</div>'); }
});
$("btnAiHealth").addEventListener("click", async () => {
  const el = $("trAi");
  setHtml(el, '<div class="placeholder small">🩺 AI 正在体检持仓组合…（约10-30秒）</div>');
  try {
    renderAiReport(el, await fetchJson("/api/ai/health"));
  } catch (e) { setHtml(el, '<div class="placeholder small">体检失败: ' + e.message + '</div>'); }
});
$("btnAiMeeting").addEventListener("click", async () => {
  const el = $("cAi");
  const code = state.code, name = $("cName").textContent || code;
  setHtml(el, '<div class="placeholder small">🎓 AI 投研会议进行中…（约15-30秒）</div>');
  try {
    renderAiReport(el, await fetchJson(`/api/ai/meeting?code=${code}&name=${encodeURIComponent(name)}`));
  } catch (e) { setHtml(el, '<div class="placeholder small">会议失败: ' + e.message + '</div>'); }
});
$("btnAiAsk").addEventListener("click", async () => {
  const code = state.code, name = $("cName").textContent || code;
  const q = prompt("🤖 问 AI（关于 " + name + "）：", "这只股票现在能买吗？有什么风险？");
  if (!q) return;
  const el = $("cAi");
  setHtml(el, '<div class="placeholder small">💬 AI 分析中…（约10-30秒）</div>');
  try {
    renderAiReport(el, await fetchJson(`/api/ai/qa?code=${code}&q=${encodeURIComponent(q)}&name=${encodeURIComponent(name)}`));
  } catch (e) { setHtml(el, '<div class="placeholder small">问诊失败: ' + e.message + '</div>'); }
});

Object.assign(window, { refreshTrading, startTrTimer, loadSentimentGate, loadNewsSentiment, loadEarningsBoard, renderAiReport });
