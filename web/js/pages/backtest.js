/* ===== J5 backtest：从 app.js 1018-1513 行切分（函数体逐字保留，window 桥接全局） ===== */
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

Object.assign(window, { initBacktestDates, parseOptGrid, loadReports, renderBacktestResult, renderMonthlyHeat });
