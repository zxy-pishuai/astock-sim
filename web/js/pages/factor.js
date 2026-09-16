/* ===== J5 factor：从 app.js 1515-1627 行切分（函数体逐字保留，window 桥接全局） ===== */
/* ============ 因子研究页 (v3.6) ============ */
let fcData = null;  // 当前 IC 扫描结果

async function loadFactors() {
  try {
    const d = await fetchJson("/api/factors");
    $("fcTip").textContent = `因子库 ${d.factors.length} 个 · 输入股票池后扫描`;
  } catch (e) { /* 静默 */ }
}

async function factorScan() {
  const codes = $("fcCodes").value.trim().replace(/[，\s]+/g, ",");
  const horizon = $("fcHorizon").value;
  if (!codes) { alert("请输入股票池代码"); return; }
  $("fcTip").textContent = "⏳ 扫描中…（逐因子计算截面 IC）";
  try {
    const d = await fetchJson(`/api/factor/scan?codes=${encodeURIComponent(codes)}&horizon=${horizon}`);
    fcData = d.results || [];
    $("fcTip").textContent = `${d.stocks} 只股票 · ${fcData.length} 个因子有效`;
    const tb = $("fcTable").querySelector("tbody");
    tb.innerHTML = fcData.map((r, i) => `
      <tr data-factor="${r.name}">
        <td>${i + 1}</td>
        <td class="clickable">${r.name}</td>
        <td title="${r.desc || ""}">${r.desc || ""}</td>
        <td class="num ${r.ic_mean > 0 ? "up" : "down"}">${r.ic_mean.toFixed(4)}</td>
        <td class="num">${fmt(r.icir, 3)}</td>
        <td class="num">${(r.positive_ratio * 100).toFixed(0)}%</td>
        <td class="num">${r.samples}</td>
        <td><button class="btn small" data-layer="${r.name}">分层回测</button></td>
      </tr>`).join("");
    tb.querySelectorAll("[data-layer]").forEach(b =>
      b.addEventListener("click", () => factorLayers(b.dataset.layer)));
    tb.querySelectorAll("[data-factor]").forEach(tr =>
      tr.addEventListener("click", () => factorLayers(tr.dataset.factor)));
  } catch (e) {
    $("fcTip").textContent = "❌ " + e.message;
  }
}

async function factorLayers(name) {
  const codes = $("fcCodes").value.trim().replace(/[，\s]+/g, ",");
  const horizon = $("fcHorizon").value;
  $("fcLayerTip").textContent = `分层回测：${name}（第1层=因子值最小，第5层=最大）`;
  try {
    const d = await fetchJson(`/api/factor/ic?factor=${encodeURIComponent(name)}&codes=${encodeURIComponent(codes)}&horizon=${horizon}`);
    const layers = d.layers || [];
    $("fcLayers").innerHTML = layers.map(l => {
      const v = l.avg_ret;
      const color = v >= 0
        ? `rgba(220,60,60,${Math.min(0.9, 0.2 + Math.abs(v) * 30)})`
        : `rgba(30,160,80,${Math.min(0.9, 0.2 + Math.abs(v) * 30)})`;
      return `<div class="mcell" style="width:110px;background:${color}">
        <span class="mm">第${l.layer}层</span>
        <span class="mv">${(v * 100).toFixed(2)}%</span>
        <span class="mm" style="font-size:9px">胜率${(l.up_ratio * 100).toFixed(0)}%</span>
      </div>`;
    }).join("") + `<div class="audit-item" style="width:100%;margin-top:6px">
      单调性评分：<b>${d.monotonicity != null ? d.monotonicity : "—"}</b>（1.0=完全单调，收益随因子值递增/递减）· IC=${d.ic_mean != null ? d.ic_mean : "—"}</div>`;
  } catch (e) {
    $("fcLayerTip").textContent = "❌ " + e.message;
  }
}

async function calcWeights() {
  const codes = $("fcCodes").value.trim().replace(/[，\s]+/g, ",");
  const method = $("fcMethod").value;
  if (!codes) { alert("请输入股票池代码"); return; }
  try {
    const d = await fetchJson(`/api/portfolio/weights?codes=${encodeURIComponent(codes)}&method=${method}`);
    if (d.error) { $("fcWeights").innerHTML = '<div class="audit-item">' + d.error + "</div>"; return; }
    $("fcWeights").innerHTML = (d.weights || []).map(w => `
      <div class="audit-item">${w.name}(${w.code}) <b style="color:${w.weight > 0.25 ? "var(--gold)" : ""}">${(w.weight * 100).toFixed(1)}%</b></div>
    `).join("") + `<div class="audit-item">方法：${d.method} · 权重合计 ${(d.sum * 100).toFixed(0)}% · ${d.stocks} 只</div>`;
  } catch (e) {
    $("fcWeights").innerHTML = '<div class="audit-item">❌ ' + e.message + "</div>";
  }
}
$("btnFactorScan").addEventListener("click", factorScan);
$("btnFcWeights").addEventListener("click", calcWeights);

/* ★ 4.3 LLM 因子挖掘 */
async function mineFactors() {
  const codes = $("fcCodes").value.trim().replace(/[，\s]+/g, ",");
  const n = +$("fcMineN").value || 5;
  const provider = $("fcMineProvider").value;
  if (!codes) { alert("请输入股票池代码"); return; }
  $("fcMineResult").innerHTML = '<div class="audit-item">⏳ 挖掘中（LLM提因子→IC检验）…</div>';
  try {
    const d = await fetchJson(`/api/factor/mine?codes=${encodeURIComponent(codes)}&n=${n}&horizon=5&provider=${provider}`);
    if (d.error) { $("fcMineResult").innerHTML = '<div class="audit-item">❌ ' + d.error + "</div>"; return; }
    const cands = d.candidates || [];
    $("fcMineResult").innerHTML = (cands.length ? cands.map((c, i) => `
      <div class="audit-item">${i + 1}. <b>${c.name}</b> IC=${c.ic_mean} ICIR=${c.icir} 正占比=${(c.positive_ratio * 100).toFixed(0)}%</div>`
    ).join("") : '<div class="audit-item">本轮无有效候选（IC 样本不足）</div>') +
      `<div class="audit-item" style="margin-top:4px">${d.stocks} 只股票 · 建议将高 |IC| 因子加入因子库</div>`;
  } catch (e) {
    $("fcMineResult").innerHTML = '<div class="audit-item">❌ ' + e.message + "</div>";
  }
}
async function loadFactorPool() {
  try {
    const d = await fetchJson("/api/factor/pool");
    const fs = d.factors || [];
    $("fcMineResult").innerHTML = (fs.length ? fs.map((f, i) => `
      <div class="audit-item">${i + 1}. <b>${f.expr}</b> IC=${f.ic} ICIR=${f.icir} 样本=${f.samples}（${f.created}）</div>`
    ).join("") : '<div class="audit-item">因子池为空 — 先运行挖掘</div>');
  } catch (e) {
    $("fcMineResult").innerHTML = '<div class="audit-item">❌ ' + e.message + "</div>";
  }
}
$("btnFcMine").addEventListener("click", mineFactors);
$("btnFcPool").addEventListener("click", loadFactorPool);

Object.assign(window, { loadFactors, factorScan, factorLayers, calcWeights, mineFactors, loadFactorPool });
