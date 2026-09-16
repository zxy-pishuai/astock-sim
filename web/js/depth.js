// ★D-S2/D-S4 十档盘口面板（验收方 2026-09-16）：
//   持仓页手动输入 + K线页自动跟随当前票，3 秒轮询 /api/depth。
// 经典脚本（非 module）——供 main.js 的 switchPage/clearTimers 以 window.* 调用。
let d10Timer = null, d10Code = "";
let d10ChartTimer = null, d10ChartCode = "";

function d10Html(d) {
  if (!d || d.error) return '<div class="placeholder small">盘口暂不可用</div>';
  const fmtW = (v) => v == null ? "—" : (Math.abs(v) >= 1e8 ? (v / 1e8).toFixed(1) + "亿" :
    Math.abs(v) >= 1e4 ? (v / 1e4).toFixed(1) + "万" : String(v));
  let bids = "", asks = "";
  for (let i = 4; i >= 0; i--) {
    const b = (d.bids || [])[i] || { p: 0, v: 0 };
    const a2 = (d.asks || [])[i] || { p: 0, v: 0 };
    asks += `<tr><td style="color:#e05252">卖${i + 1} ${(+a2.p).toFixed(2)} 量=${fmtW(a2.v)}</td></tr>`;
    bids += `<tr><td style="color:#2e9e5b">买${i + 1} ${(+b.p).toFixed(2)} 量=${fmtW(b.v)}</td></tr>`;
  }
  const bs = (d.bids || []).reduce((s, x) => s + (x.v || 0), 0);
  const as = (d.asks || []).reduce((s, x) => s + (x.v || 0), 0);
  const bal = (bs + as) > 0 ? (100 * bs / (bs + as)).toFixed(0) : "—";
  const au = d.auction;
  const aucLine = au && !au.no_data
    ? `<div class="tip" style="margin-top:4px">📊 竞价撤单率 <b>${(100 * au.cancel_ratio).toFixed(0)}%</b>（峰值挂 ${fmtW(au.peak_vol)} ${au.peak_dir > 0 ? "买" : "卖"}撤 → 余 ${fmtW(au.final_vol)}）</div>`
    : (au && au.no_data ? '<div class="tip" style="margin-top:4px">竞价撤单：当日无数据</div>' : '');
  return `
    <div><b>${d.name || d.code}</b> <span>${d.price}（${d.pct}%）</span> <span class="tip">档源=${d.src}</span></div>
    <table class="grid small" style="margin-top:6px">
      <thead><tr><th style="text-align:left">卖档（压力）</th></tr></thead>
      <tbody>${asks}</tbody>
      <thead><tr><th style="text-align:left">买档（支撑/接力）</th></tr></thead>
      <tbody>${bids}</tbody>
    </table>
    <div class="tip" style="margin-top:6px">内/外盘：卖 ${fmtW(d.inner)} / 买 ${fmtW(d.outer)}｜十档买比 ${bal}%</div>${aucLine}`;
}

async function refreshDepth() {
  let d = null;
  try { d = await (await fetch(`/api/depth?code=${encodeURIComponent(d10Code)}`)).json(); }
  catch (e) {}
  $("d10Body").innerHTML = d10Html(d);
}

async function refreshChartDepth() {
  let d = null;
  try { d = await (await fetch(`/api/depth?code=${encodeURIComponent(d10ChartCode)}`)).json(); }
  catch (e) {}
  const el = $("d10ChartBody");
  if (el) el.innerHTML = d10Html(d);
}

function stopDepth() {
  if (d10Timer) { clearInterval(d10Timer); d10Timer = null; }
}

function stopChartDepth() {
  if (d10ChartTimer) { clearInterval(d10ChartTimer); d10ChartTimer = null; }
}

function startDepthTimer() {
  const el = $("d10Code");
  if (el && !el.value) el.value = d10Code || localStorage.d10Code || "";
  if (!d10Code && el) d10Code = (el.value || "").trim();
  if (!d10Code) return;
  refreshDepth();
  stopDepth();
  d10Timer = setInterval(refreshDepth, 3000);
}

// K线页：跟随当前看的票（main.js / pages/chart.js 调用，state.code 为实参）
function startChartDepth(code) {
  code = String(code || "").replace(/\D/g, "").slice(-6);
  if (code.length !== 6) return;
  d10ChartCode = code;
  refreshChartDepth();
  stopChartDepth();
  d10ChartTimer = setInterval(refreshChartDepth, 3000);
}

const d10Go = $("d10Go");
if (d10Go) {
  d10Go.addEventListener("click", () => {
    d10Code = ($("d10Code").value || "").replace(/\D/g, "").slice(0, 6);
    if (d10Code.length === 6) {
      localStorage.d10Code = d10Code;
      startDepthTimer();
    }
  });
}
