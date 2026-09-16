/* ===== J5 sflow：从 app.js 2018-2130 行切分（函数体逐字保留，window 桥接全局） ===== */
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

Object.assign(window, { startSflowTimer, initSectorFlow, refreshSectorFlow, renderSflowStats, renderSflowLists, renderSectorFlow });
