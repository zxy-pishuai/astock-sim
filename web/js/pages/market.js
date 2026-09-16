/* ===== J5 market：从 app.js 461-592 行切分（函数体逐字保留，window 桥接全局） ===== */
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

Object.assign(window, { refreshMarket, startMarketTimer, loadWatchStrip, loadAlerts });
