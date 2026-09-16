/* ===== J5 audit：从 app.js 1629-1683 行切分（函数体逐字保留，window 桥接全局） ===== */
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

Object.assign(window, { refreshAudit });
