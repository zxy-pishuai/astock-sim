/* ===== J5 portfolio：从 app.js 911-1016 行切分（函数体逐字保留，window 桥接全局） ===== */
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

Object.assign(window, { refreshPortfolio, startPosTimer, renderPortfolioEquity });
