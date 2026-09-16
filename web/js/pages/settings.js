/* ===== J5 settings：从 app.js 1685-1774 行切分（函数体逐字保留，window 桥接全局） ===== */
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

Object.assign(window, { loadSettings, loadUpdateStatus });
