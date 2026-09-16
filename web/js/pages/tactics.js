/* ===== J5 tactics：从 app.js 2326-2536 行切分（函数体逐字保留，window 桥接全局） ===== */
/* ============ 🎯 战法选股（T1 + T2 v2：名称列 / 连板梯队全展示 / 竞价双战法）============ */
let tctTimer = null;
let tctTab = "shouban";
let tctLadder = "全部";
let tctData = null;
// T2：四个 tab → tactics 数组下标（server 禁改，/api/tactics 固定返回
// [首板回调, 连板梯队, 竞价打板, 竞价弱转强] 四卡）
const TCT_TAB_IDX = { shouban: 0, lianban: 1, jingjia_daban: 2, jingjia_w2s: 3 };
const TCT_STAGE_CLS = {
  "首板确认": "tct-pass", "买点触发(收盘买)": "tct-hit", "持有中(等≥9%大阳,第j日)": "tct-run",
  "兑现-未板落袋": "tct-pass", "兑现-封板格局(次日开盘卖)": "tct-pass",
  "失效(14日)": "tct-fail", "不在池": "tct-dim", "不在梯队": "tct-dim",
  "首板缩量(候选)": "tct-hit", "首板放量(不候选)": "tct-dim",
  "已打板(隔夜持有)": "tct-run", "二板兑现": "tct-pass", "断板(次日开盘已卖)": "tct-fail",
  "一字(买不进)": "tct-fail",
  /* T2 连板梯队全展示（观察系一律灰，绝不复用候选金） */
  "一进二候选(首板缩量)": "tct-hit", "一进二不候选(放量或一字)": "tct-dim",
  "二进三(仅观察)": "tct-obs", "三进四(仅观察)": "tct-obs", "四进五(仅观察)": "tct-obs",
  "五板+(仅观察)": "tct-obs",
  /* T2 竞价双战法 */
  "竞价候选(≥40分)": "tct-hit", "竞价观察": "tct-dim", "竞价快照缺失(降级)": "tct-warn",
  "弱转强观察": "tct-obs", "弱转强未触发": "tct-dim"
};
// T2：ladder 与后端 _ladder_of 对齐（lbc>=5 合并"五板+"，断板单独档）
const TCT_LADDERS = ["全部", "一进二", "二进三", "三进四", "四进五", "五板+", "断板"];
function startTacticsTimer() {
  clearTimers();
  if (!state.tctTimer) state.tctTimer = setInterval(() => { if (state.page === "tactics") loadTactics(); }, 60000);
}
async function loadTactics() {
  const errEl = $("tct-err"), meta = $("tct-meta"), cov = $("tct-coverage");
  if (!errEl) return;
  const tb = $("tct-tbody");
  // ★ K9：加载中骨架屏（.skeleton 由 J5 提供）；pending 占位由 fetchJson 自动重试
  uiShowSkeleton(tb);
  try {
    const d = await fetchJson("/api/tactics");
    uiClearSkeleton(tb);
    tctData = d;
    if (d && d.error) {
      errEl.textContent = "服务错误: " + d.error;
      tctData = null; renderTacticsEmpty();
      return;
    }
    errEl.textContent = "";
    const c = (d && d.coverage) || {};
    meta.textContent = (d && d.generated_at) ? "更新: " + d.generated_at : "";
    if (c.latest_date) {
      let t = "数据日: " + c.latest_date + " · " + (c.n_codes === undefined ? "?" : c.n_codes) + " 只";
      // R2-P0.2：红条判定升级——complete=False（<1500）或 ratio<0.9（缺口>10%）
      // 都提示。原 09-03 379 缺口 ratio≈0.84 不再静默。
      const incompl = (c.complete === false) ||
        (c.ratio !== undefined && c.total && c.ratio < 0.9);
      if (incompl) t += " ⚠数据不完整日(尾部日更缺口)";
      cov.textContent = t;
      cov.className = "tip" + (incompl ? " warn" : "");
    } else {
      cov.textContent = "";
    }
    renderTactics();
  } catch (e) {
    uiClearSkeleton(tb);
    errEl.textContent = "服务不可用，请稍后重试";
    tctData = null;
    renderTacticsEmpty();
    // ★ K9：重试耗尽 → err-banner + 点此重试，不留空白
    uiShowErrBanner(tb, () => loadTactics());
  }
}
function renderTacticsEmpty() {
  const hd = $("tct-head"), tb = $("tct-tbody");
  if (hd) hd.innerHTML = "<th>代码</th><th>名称</th><th>得分</th><th>阶段</th><th>明细</th>";
  if (tb) tb.innerHTML = '<tr><td colspan="5" style="color:#888">暂无数据（服务不可用或空）</td></tr>';
  const ld = $("tct-ladders");
  if (ld) ld.style.display = "none";
  const nt = $("tct-notice");
  if (nt) nt.style.display = "none";
}
function renderTactics() {
  if (!tctData || !(tctData.tactics || []).length) { renderTacticsEmpty(); return; }
  const tac = tctData.tactics;
  const t = tac[TCT_TAB_IDX[tctTab]] || tac[0];
  const isLb = t.name === "连板梯队";
  const isJingjia = (t.name === "竞价打板" || t.name === "竞价弱转强");
  // 连板子板块按钮（仅连板梯队）
  const ld = $("tct-ladders");
  if (ld) {
    if (isLb) {
      ld.style.display = "";
      ld.innerHTML = "";
      TCT_LADDERS.forEach(name => {
        const b = document.createElement("button");
        b.className = "exp-tab" + (tctLadder === name ? " active" : "");
        b.textContent = name;
        b.addEventListener("click", () => { tctLadder = name; renderTactics(); });
        ld.appendChild(b);
      });
    } else {
      ld.style.display = "none";
    }
  }
  // 竞价双战法诚实表头提示（T2 §4c：无 IS/OOS 回测卡，禁止渲染成"已验收"视觉）
  const nt = $("tct-notice");
  if (nt) {
    if (isJingjia) {
      nt.style.display = "";
      nt.textContent = (t.honest_note || "竞价双战法为系统现役评分器的公示参考，未做过 W1R/V1 级回测验收")
        + (tctTab === "jingjia_w2s" ? " · 弱转强研究结论 P34=不成立不集成，仅作观察" : "");
      nt.className = "tct-honest-note";
    } else {
      nt.style.display = "none";
    }
  }
  // 表头
  const hd = $("tct-head");
  let keyLabel = "关键信号";
  if (isLb) keyLabel = "lbc·板型·量比";
  else if (t.name === "竞价打板") keyLabel = "高开·量比·竞价额";
  else if (t.name === "竞价弱转强") keyLabel = "高开缺口";
  const heads = ["代码", "名称", "得分", "阶段", keyLabel, "明细"];
  hd.innerHTML = "";
  heads.forEach(h => {
    const th = document.createElement("th");
    if (isLb && h === "lbc·板型·量比") {
      th.textContent = h + "  ⚠20cm(30/68)未单独验证";
      th.title = "规则未在 20cm(30/68) 单独验证，30/68 候选行加 ⚠ 徽章——数据缺口如实展示，不改判据";
    } else {
      th.textContent = h;
    }
    hd.appendChild(th);
  });
  // 行（连板按子板块过滤）
  const tb = $("tct-tbody");
  tb.innerHTML = "";
  let rows = (t.statuses || []).slice();
  if (isLb && tctLadder !== "全部") {
    rows = rows.filter(s => (s.ladder || "观察") === tctLadder);
  }
  rows.forEach(s => tb.appendChild(tacticsRow(isLb, isJingjia, s)));
  if (rows.length === 0) {
    tb.innerHTML = '<tr><td colspan="6" style="color:#888">该梯队/时段暂无候选</td></tr>';
  }
  const meta = $("tct-meta");
  if (t.stages_summary) {
    const s = Object.keys(t.stages_summary).map(k => k + " " + t.stages_summary[k]).join(" / ");
    if (meta) meta.textContent += " · 规则: " + t.rule_source + " · " + s;
  }
}
function tacticsRow(isLb, isJingjia, s) {
  const tr = document.createElement("tr");
  const obsStage = /(仅观察)|观察/.test(s.stage || "");
  // 代码（20cm 标注）
  const tdC = document.createElement("td");
  tdC.appendChild(document.createTextNode(s.code));
  if (isLb && /^(30|68)/.test(s.code)) {
    const warn = document.createElement("span");
    warn.textContent = " ⚠";
    warn.className = "tct-warn";
    warn.title = "30/68 属 20cm 板，规则未单独验证";
    tdC.appendChild(warn);
  }
  tr.appendChild(tdC);
  // 名称（T2 新需求1）
  const tdN = document.createElement("td");
  tdN.textContent = s.name || "";
  tdN.className = "tct-name";
  tr.appendChild(tdN);
  // 得分
  const tdSc = document.createElement("td");
  const sc = (s.score !== undefined && s.score !== null) ? Number(s.score).toFixed(1) : "-";
  tdSc.textContent = sc;
  tdSc.style.fontWeight = "600";
  // T2：观察系一律灰，绝不复用候选金/绿
  tdSc.style.color = obsStage ? "#7c8595"
    : (Number(s.score) >= 70 ? "#5ce08c" : (Number(s.score) >= 40 ? "#ffd27a" : "#7c8595"));
  tr.appendChild(tdSc);
  // 阶段徽章
  const tdS = document.createElement("td");
  const badge = document.createElement("span");
  badge.className = "tct-badge " + (TCT_STAGE_CLS[s.stage] || "tct-dim");
  badge.textContent = s.stage;
  tdS.appendChild(badge);
  tr.appendChild(tdS);
  // 关键列
  const tdK = document.createElement("td");
  if (isLb) {
    const lbc = (s.lbc !== undefined ? s.lbc : "-");
    const bt = (s.board_type || "none");
    const ar = (s.amount_ratio !== null && s.amount_ratio !== undefined)
      ? Number(s.amount_ratio).toFixed(2) : "-";
    tdK.textContent = lbc + "板 · " + bt + " · 量比" + ar;
  } else if (tctTab === "jingjia_daban") {
    const ap = (s.apct !== undefined) ? Number(s.apct).toFixed(1) + "%" : "-";
    const vr = (s.vr !== undefined) ? Number(s.vr).toFixed(2) : "-";
    const am = (s.amount !== undefined) ? (Number(s.amount) / 1e4).toFixed(0) + "万" : "-";
    tdK.textContent = "高开" + ap + " · 量比" + vr + " · 竞价额" + am;
  } else if (tctTab === "jingjia_w2s") {
    const gp = (s.gap_pp !== undefined) ? Number(s.gap_pp).toFixed(1) + "%" : "-";
    tdK.textContent = "高开" + gp;
  } else {
    tdK.textContent = (s.signals && s.signals.length)
      ? s.signals.map(x => x.name + ":" + x.value).join(" · ") : "-";
  }
  tr.appendChild(tdK);
  // 明细
  const tdD = document.createElement("td");
  tdD.textContent = s.detail || "";
  tr.appendChild(tdD);
  return tr;
}
document.querySelectorAll("#tct-tabs .exp-tab").forEach(el =>
  el.addEventListener("click", () => {
    document.querySelectorAll("#tct-tabs .exp-tab").forEach(tb => tb.classList.remove("active"));
    el.classList.add("active");
    tctTab = el.dataset.tactic;
    tctLadder = "全部";
    renderTactics();
  }));

Object.assign(window, { startTacticsTimer, loadTactics, renderTacticsEmpty, renderTactics, tacticsRow });
