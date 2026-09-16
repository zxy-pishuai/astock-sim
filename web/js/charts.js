/* ===== A股模拟盘 Pro — ECharts 图表封装 =====
 * K线：主图(蜡烛+MA/BOLL) + 成交量 + MACD/RSI，dataZoom 三图联动缩放
 * 分时：价格线+均价线+昨收基准，实时增量刷新
 * 净值：策略 vs 基准双线
 */
"use strict";

const RED = "#e74c5a", GREEN = "#2ecc71", GOLD = "#f0b90b",
      BLUE = "#4c9aff", PURPLE = "#9b7bff", CYAN = "#35c8d8", ORANGE = "#e67e22";

function calcSMA(vals, p) {
  const out = [];
  for (let i = 0; i < vals.length; i++) {
    if (i < p - 1) { out.push(null); continue; }
    let s = 0;
    for (let j = i - p + 1; j <= i; j++) s += vals[j];
    out.push(+(s / p).toFixed(3));
  }
  return out;
}
function calcEMA(vals, p) {
  const out = [];
  if (vals.length < p) return vals.map(() => null);
  const k = 2 / (p + 1);
  let prev = null, sum = 0;
  for (let i = 0; i < vals.length; i++) {
    if (i < p - 1) { out.push(null); continue; }
    if (i === p - 1) { for (let j = 0; j < p; j++) sum += vals[j]; prev = sum / p; }
    else prev = (vals[i] - prev) * k + prev;
    out.push(+prev.toFixed(3));
  }
  return out;
}
function calcMACD(closes) {
  const fast = calcEMA(closes, 12), slow = calcEMA(closes, 26);
  const dif = closes.map((_, i) => (fast[i] != null && slow[i] != null) ? +(fast[i] - slow[i]).toFixed(4) : null);
  const valid = dif.map((d, i) => d == null ? 0 : d);
  const dea = calcEMA(valid, 9).map((v, i) => dif[i] == null ? null : v);
  const hist = dif.map((d, i) => d != null && dea[i] != null ? +((d - dea[i]) * 2).toFixed(4) : null);
  return { dif, dea, hist };
}
function calcRSI(closes, p = 14) {
  const out = [];
  if (closes.length < p + 1) return closes.map(() => null);
  let gains = [], losses = [];
  for (let i = 0; i < p; i++) { out.push(null); }
  for (let i = 1; i <= p; i++) {
    const d = closes[i] - closes[i - 1];
    gains.push(Math.max(d, 0)); losses.push(Math.max(-d, 0));
  }
  for (let i = p; i < closes.length; i++) {
    if (i > p) {
      const d = closes[i] - closes[i - 1];
      gains.shift(); gains.push(Math.max(d, 0));
      losses.shift(); losses.push(Math.max(-d, 0));
    }
    const ag = gains.reduce((a, b) => a + b, 0) / p;
    const al = losses.reduce((a, b) => a + b, 0) / p;
    out.push(al === 0 ? 100 : +(100 - 100 / (1 + ag / al)).toFixed(2));
  }
  return out;
}
function calcBOLL(closes, p = 20, k = 2) {
  const mid = calcSMA(closes, p);
  const up = [], low = [];
  for (let i = 0; i < closes.length; i++) {
    if (i < p - 1) { up.push(null); low.push(null); continue; }
    const w = closes.slice(i - p + 1, i + 1);
    const m = mid[i];
    const sd = Math.sqrt(w.reduce((a, x) => a + (x - m) ** 2, 0) / p);
    up.push(+(m + k * sd).toFixed(3));
    low.push(+(m - k * sd).toFixed(3));
  }
  return { mid, up, low };
}

/* ============ K线图 ============ */
class KlineChart {
  constructor(dom) {
    this.dom = dom;
    this.chart = echarts.init(dom);
    this.cache = null;   // 最近一次渲染数据（供增量更新）
    this._resize = () => this.resize();
    window.addEventListener("resize", this._resize);
  }
  resize() { this.chart.resize(); }
  /* ★ 4.5 修复：销毁 echarts 实例并移除监听（K线/分时共用DOM，切换时必须互斥销毁） */
  dispose() {
    try {
      if (this.chart) { this.chart.dispose(); this.chart = null; }
    } catch (e) { /* 已销毁 */ }
    window.removeEventListener("resize", this._resize);
  }

  buildOption(d) {
    // d: {dates, kdata, ma:[{name,data}], boll:{up,mid,low}或null, vol:[{v,up}],
    //      sub:'macd'|'rsi'|'none', subData:{...}}
    const dates = d.dates, kdata = d.kdata;
    const volColors = d.vol.map(v => v.up ? RED : GREEN);
    const subSeries = [], grids = [{ left: 56, right: 14, top: 14, height: "52%" }],
          xAxes = [{ type: "category", data: dates, gridIndex: 0, boundaryGap: true }];
    let gIdx = 1;

    // 副图1 成交量
    grids.push({ left: 56, right: 14, top: "58%", height: "13%" });
    xAxes.push({ type: "category", data: dates, gridIndex: 1, boundaryGap: true, axisLabel: { show: false } });

    // 副图2 MACD/RSI
    if (d.sub !== "none") {
      grids.push({ left: 56, right: 14, top: "74%", height: "16%" });
      xAxes.push({ type: "category", data: dates, gridIndex: 2, boundaryGap: true });
    }
    const yAxes = [
      { gridIndex: 0, scale: true, position: "left", splitLine: { lineStyle: { color: "#1d2434" } } },
      { gridIndex: 1, position: "left", splitLine: { show: false } },
    ];
    if (d.sub !== "none") yAxes.push({ gridIndex: 2, position: "left", splitLine: { lineStyle: { color: "#1d2434" } } });

    const xIdxAll = d.sub !== "none" ? [0, 1, 2] : [0, 1];
    const dataZoom = [
      { type: "inside", xAxisIndex: xIdxAll, start: 55, end: 100, zoomOnMouseWheel: true, moveOnMouseMove: true },
      { type: "slider", xAxisIndex: xIdxAll, bottom: 4, height: 16, start: 55, end: 100,
        borderColor: "#2c3650", backgroundColor: "#151a24", fillerColor: "rgba(76,154,255,.12)",
        textStyle: { color: "#8b96a8" } },
    ];

    // 主图 series
    const mainSeries = [{
      name: "K线", type: "candlestick", data: kdata, xAxisIndex: 0, yAxisIndex: 0,
      itemStyle: { color: RED, color0: GREEN, borderColor: RED, borderColor0: GREEN },
    }];
    (d.ma || []).forEach(m => {
      const col = { MA5: GOLD, MA10: BLUE, MA20: PURPLE, MA60: CYAN }[m.name] || CYAN;
      mainSeries.push({ name: m.name, type: "line", data: m.data, xAxisIndex: 0, yAxisIndex: 0,
        symbol: "none", lineStyle: { width: 1, color: col }, itemStyle: { color: col } });
    });
    if (d.boll) {
      mainSeries.push({ name: "BOLL上", type: "line", data: d.boll.up, xAxisIndex: 0, yAxisIndex: 0, symbol: "none", lineStyle: { width: 1, color: "#5b6577" } });
      mainSeries.push({ name: "BOLL中", type: "line", data: d.boll.mid, xAxisIndex: 0, yAxisIndex: 0, symbol: "none", lineStyle: { width: 1, color: "#5b6577", type: "dashed" } });
      mainSeries.push({ name: "BOLL下", type: "line", data: d.boll.low, xAxisIndex: 0, yAxisIndex: 0, symbol: "none", lineStyle: { width: 1, color: "#5b6577" } });
    }

    const series = [
      ...mainSeries,
      { name: "成交量", type: "bar", data: d.vol.map(v => v.v), xAxisIndex: 1, yAxisIndex: 1,
        itemStyle: { color: p => volColors[p.dataIndex] }, barWidth: "60%" },
    ];
    if (d.sub === "macd") {
      const m = d.subData;
      series.push({ name: "DIF", type: "line", data: m.dif, xAxisIndex: 2, yAxisIndex: 2, symbol: "none", lineStyle: { width: 1, color: GOLD } });
      series.push({ name: "DEA", type: "line", data: m.dea, xAxisIndex: 2, yAxisIndex: 2, symbol: "none", lineStyle: { width: 1, color: BLUE } });
      series.push({ name: "MACD", type: "bar", data: m.hist, xAxisIndex: 2, yAxisIndex: 2,
        itemStyle: { color: p => (m.hist[p.dataIndex] || 0) >= 0 ? RED : GREEN }, barWidth: "55%" });
    } else if (d.sub === "rsi") {
      const r = d.subData.rsi;
      series.push({ name: "RSI", type: "line", data: r, xAxisIndex: 2, yAxisIndex: 2, symbol: "none", lineStyle: { width: 1, color: PURPLE } });
      series.push({ name: "RSI70", type: "line", data: r.map(() => 70), xAxisIndex: 2, yAxisIndex: 2, symbol: "none", lineStyle: { width: 1, color: "#5b6577", type: "dashed" } });
      series.push({ name: "RSI30", type: "line", data: r.map(() => 30), xAxisIndex: 2, yAxisIndex: 2, symbol: "none", lineStyle: { width: 1, color: "#5b6577", type: "dashed" } });
    }

    return {
      backgroundColor: "transparent",
      animation: false,
      axisPointer: { link: [{ xAxisIndex: "all" }], label: { backgroundColor: "#2c3650" } },
      tooltip: { trigger: "axis", axisPointer: { type: "cross" },
        backgroundColor: "#1a2030", borderColor: "#2c3650", textStyle: { color: "#d6dde8", fontSize: 11 } },
      legend: { data: ["K线", "MA5", "MA10", "MA20", "MA60", "DIF", "DEA", "MACD"],
        top: 0, left: 60, textStyle: { color: "#8b96a8", fontSize: 11 }, itemWidth: 14, itemHeight: 8 },
      grid: grids, xAxis: xAxes, yAxis: yAxes, dataZoom, series,
    };
  }

  render(d) {
    this.cache = d;
    this.chart.setOption(this.buildOption(d), true);
  }

  /* 增量更新：保留缩放窗口，仅替换数据 */
  update(d) {
    this.cache = d;
    const opt = this.buildOption(d);
    // 保留 dataZoom 当前窗口
    const zoom = this.chart.getOption().dataZoom;
    if (zoom && zoom[0] && zoom[0].start != null) {
      opt.dataZoom[0].start = zoom[0].start; opt.dataZoom[0].end = zoom[0].end;
      opt.dataZoom[1].start = zoom[1].start; opt.dataZoom[1].end = zoom[1].end;
    }
    this.chart.setOption(opt, true);
  }
}

/* ============ 分时图 ============ */
class MinuteChart {
  constructor(dom) {
    this.chart = echarts.init(dom);
    this.cache = null;
    this._resize = () => this.resize();
    window.addEventListener("resize", this._resize);
  }
  resize() { this.chart.resize(); }
  /* ★ 4.5 修复：销毁 echarts 实例并移除监听 */
  dispose() {
    try {
      if (this.chart) { this.chart.dispose(); this.chart = null; }
    } catch (e) { /* 已销毁 */ }
    window.removeEventListener("resize", this._resize);
  }

  render(d) {
    // d: {times, prices, avgs, vols, yest, refLabel}
    // ★ 4.5 修复：标准A股分时图 —— 以昨收为 0 轴的涨跌幅百分比对称轴，
    //   0 轴（昨收基准线）居中清晰显示，上下对称（±1/±2/±3/±5%…）
    this.cache = d;
    const base = d.yest > 0 ? d.yest : d.prices[0];
    const times = d.times, prices = d.prices;
    // 相对昨收的涨跌幅（%）
    const pct = prices.map(p => (base > 0 && p != null && !isNaN(p)) ? (p - base) / base * 100 : null);  // ★ 无数据时槽保持 null（原为 0 → 未开盘时段会画出贴 0 轴平线）
    const avgPct = d.avgs.map(a => (base > 0 && a != null && !isNaN(a)) ? (a - base) / base * 100 : null);
    // 对称刻度：取数据最大 |涨跌幅|，向上归整到标准档位（至少 ±1%）
    let maxAbs = 1;
    for (const v of pct) {
      if (v != null && isFinite(v) && Math.abs(v) > maxAbs) maxAbs = Math.abs(v);
    }
    const ticks = [1, 2, 3, 4, 5, 7, 10];
    let span = 1;
    for (const t of ticks) { if (maxAbs <= t) { span = t; break; } span = t; }
    if (span < 1) span = 1;
    // ★ 颜色取最后一个有效点（尾部 null 不参与）
    let lastPct = 0;
    for (let i = pct.length - 1; i >= 0; i--) { if (pct[i] != null) { lastPct = pct[i]; break; } }
    const lineColor = lastPct >= 0 ? RED : GREEN;
    this.chart.setOption({
      backgroundColor: "transparent", animation: false,
      title: { text: d.refLabel || "", right: 14, top: 0, textStyle: { color: "#8b96a8", fontSize: 11, fontWeight: "normal" } },
      tooltip: {
        trigger: "axis",
        formatter: params => {
          if (!params || !params.length) return "";
          const idx = params[0].dataIndex;
          const t = times[idx] || "";
          const px = prices[idx], p = pct[idx];
          const avg = d.avgs[idx], v = d.vols[idx];
          return `<b>${t}</b><br>` +
            `价格: ${px != null ? px.toFixed(2) : "-"} ` +
            `<span style="color:${p >= 0 ? "#e74c5a" : "#2ecc71"}">(${p >= 0 ? "+" : ""}${(p || 0).toFixed(2)}%)</span><br>` +
            `均价: ${avg != null ? avg.toFixed(2) : "-"}<br>` +
            `成交量: ${v != null ? Number(v).toFixed(0) : "-"}`;
        },
        backgroundColor: "#1a2030", borderColor: "#2c3650", textStyle: { color: "#d6dde8", fontSize: 11 },
      },
      axisPointer: { link: [{ xAxisIndex: "all" }], label: { backgroundColor: "#2c3650" } },
      grid: [
        { left: 62, right: 14, top: 14, height: "55%" },
        { left: 62, right: 14, top: "62%", height: "22%" },
      ],
      xAxis: [
        { type: "category", data: times, gridIndex: 0, boundaryGap: false, axisLine: { lineStyle: { color: "#2c3650" } }, axisLabel: { show: false } },
        { type: "category", data: times, gridIndex: 1, boundaryGap: false, axisLine: { lineStyle: { color: "#2c3650" } }, axisLabel: { color: "#8b96a8", fontSize: 10 } },
      ],
      yAxis: [
        {
          gridIndex: 0, position: "left", min: -span, max: span,
          // 0 轴高亮：0 值刻度加粗（A股分时 0 轴居中）
          axisLabel: {
            color: "#8b96a8", fontSize: 10,
            formatter: v => {
              if (v === 0) return "0.00%";
              return (v > 0 ? "+" : "") + v + "%";
            },
            rich: { z: { color: "#f0b90b", fontWeight: "bold" } },
          },
          splitLine: { lineStyle: { color: "#1d2434" } },
        },
        { gridIndex: 1, position: "left", splitLine: { show: false }, axisLabel: { color: "#8b96a8", fontSize: 10 } },
      ],
      series: [
        {
          name: "价格", type: "line", data: pct, xAxisIndex: 0, yAxisIndex: 0, symbol: "none",
          lineStyle: { width: 1.4, color: lineColor }, areaStyle: { color: lineColor, opacity: 0.08 },
          // ★ 0 轴（昨收基准线）：明显实线 + 标签
          markLine: {
            silent: true, symbol: "none",
            data: [{ yAxis: 0 }],
            lineStyle: { color: "#8b96a8", type: "solid", width: 1.4 },
            label: { show: true, formatter: "0.00% 昨收", position: "insideEndTop",
                     color: "#8b96a8", fontSize: 10 },
          },
        },
        { name: "均价", type: "line", data: avgPct, xAxisIndex: 0, yAxisIndex: 0, symbol: "none",
          lineStyle: { width: 1, color: GOLD } },
        { name: "成交量", type: "bar", data: d.vols, xAxisIndex: 1, yAxisIndex: 1, barWidth: "60%",
          itemStyle: { color: p => (pct[p.dataIndex] || 0) >= 0 ? RED : GREEN } },
      ],
    }, true);
  }
}

/* ============ 净值曲线 ============ */
function renderEquityChart(dom, dates, equity, bench, benchName) {
  const chart = echarts.getInstanceByDom(dom) || echarts.init(dom);
  const series = [
    { name: "策略净值", type: "line", data: equity, symbol: "none",
      lineStyle: { width: 1.6, color: BLUE }, areaStyle: { color: BLUE, opacity: 0.1 } },
  ];
  const legend = ["策略净值"];
  if (bench) {
    series.push({ name: benchName || "基准(等权)", type: "line", data: bench, symbol: "none",
      lineStyle: { width: 1, color: "#5b6577", type: "dashed" } });
    legend.push(benchName || "基准(等权)");
  }
  chart.setOption({
    backgroundColor: "transparent", animation: false,
    tooltip: { trigger: "axis",
      formatter: params => {
        if (!params || !params.length) return "";
        const p = params[0];
        const lines = params.map(x => x.marker + " " + x.seriesName + ": " +
          (x.value != null ? (x.value * 100).toFixed(2) + "%" : "-"));
        return (p.axisValue || "") + "<br>" + lines.join("<br>");
      },
      backgroundColor: "#1a2030", borderColor: "#2c3650", textStyle: { color: "#d6dde8", fontSize: 11 } },
    legend: { top: 0, left: 8, textStyle: { color: "#8b96a8", fontSize: 11 }, itemWidth: 14, itemHeight: 8 },
    grid: { left: 56, right: 16, top: 30, bottom: 30 },
    xAxis: { type: "category", data: dates, axisLine: { lineStyle: { color: "#2c3650" } },
      axisLabel: { color: "#8b96a8", fontSize: 10 } },
    yAxis: { type: "value", scale: true, axisLabel: { formatter: v => (v * 100).toFixed(0) + "%", color: "#8b96a8", fontSize: 10 },
      splitLine: { lineStyle: { color: "#1d2434" } } },
    dataZoom: [{ type: "inside", start: 0, end: 100 }],
    series,
  }, true);
  chart.resize();
  return chart;
}
