/* ===== J5 pnews：从 app.js 2132-2323 行切分（函数体逐字保留，window 桥接全局） ===== */
/* ============ ★ 4.7 盘前新闻 ============ */
let pnReady = false;
function _pnEsc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;" }[c]));
}
function startPnewsTimer() {
  clearTimers();
  if (!state.pnewsTimer) state.pnewsTimer = setInterval(() => refreshPnews(false), 60000);
}
function initPnews() {
  if (!pnReady) { pnReady = true; }
  refreshPnews(false);
}
async function refreshPnews(force) {
  const view = state.pnewsView || "list";
  try {
    $("pnStatus").textContent = "加载中…";
    if (view === "sector") {
      const rs = await fetchJson("/api/news/sectors");
      if (!rs || rs.error) { $("pnStatus").textContent = "获取失败: " + ((rs && rs.error) || "未知错误"); return; }
      renderPnewsSectors(rs);
      return;
    }
    const scope = state.pnewsScope || "all";
    const min = state.pnewsMin != null ? state.pnewsMin : 2;
    // ★ K9：premarket 加载中骨架屏；pending 占位由 fetchJson 自动重试
    uiShowSkeleton($("pnList"));
    let r;
    try {
      r = await fetchJson("/api/news/premarket?scope=" + scope + "&min=" + min + (force ? "&force=1" : ""));
    } catch (e) {
      uiClearSkeleton($("pnList"));
      $("pnStatus").textContent = "获取异常: " + e;
      // ★ K9：重试耗尽 → err-banner + 点此重试，不留空白
      uiShowErrBanner($("pnList"), () => refreshPnews(force));
      return;
    }
    uiClearSkeleton($("pnList"));
    if (r && r.disabled) {
      $("pnStatus").textContent = "未启用（config.PREMARKET_NEWS_ENABLED=False）";
      $("pnStats").textContent = "";
      $("pnList").innerHTML = "";
      return;
    }
    if (!r || r.error) { $("pnStatus").textContent = "获取失败: " + ((r && r.error) || "未知错误"); return; }
    renderPnews(r);
  } catch (e) {
    $("pnStatus").textContent = "获取异常: " + e;
  }
}
function renderPnews(r) {
  const items = r.items || [];
  const st = r.stats || {};
  let statTxt = "更新 " + (r.fetched_at || "");
  if (st.ai_pending) statTxt += " · AI 评分中…";
  else if (st.ai_used) statTxt += " · AI 已评分";
  $("pnStatus").textContent = statTxt;
  $("pnStats").textContent = "共 " + (st.total || 0) + " 条 / 入选 " + (st.kept || 0) + " 条 · 来源: " + (st.sources || []).join("+");
  if (!items.length) {
    $("pnList").innerHTML = '<div class="placeholder small">当前条件下暂无新闻，可降低重要度筛选或点刷新</div>';
    return;
  }
  $("pnList").innerHTML = items.map(it => {
    const sc = it.score | 0;
    const lvl = sc >= 4 ? "hi" : (sc >= 3 ? "mid" : "lo");
    const stocks = (it.stocks || []).slice(0, 6).map(s => '<span class="pn-stock">' + _pnEsc(s) + "</span>").join("");
    return '<div class="pn-item ' + lvl + '">'
      + '<div class="pn-head"><span class="pn-score">' + sc + "分</span>"
      + '<span class="pn-title">' + _pnEsc(it.title) + "</span>"
      + (it.cat ? '<span class="pn-cat">' + _pnEsc(it.cat) + "</span>" : "")
      + (it.ai ? '<span class="pn-cat" title="AI 评分">AI</span>' : "")
      + "</div>"
      + (it.text ? '<div class="pn-text">' + _pnEsc(it.text) + "</div>" : "")
      + '<div class="pn-foot"><span>' + _pnEsc(it.time || "") + '</span><span class="pn-src">' + _pnEsc(it.source || "") + "</span>"
      + (it.why ? '<span class="pn-why">' + _pnEsc(it.why) + "</span>" : "")
      + stocks + _pnSecChips(it.sectors) + "</div></div>";
  }).join("");
  document.querySelectorAll("#pnList .pn-item").forEach(el =>
    el.addEventListener("click", () => el.classList.toggle("expanded")));
}

/* ============ ★ 4.8 盘前简报 + 资金流新闻高亮 ============ */
function renderSflowNewsHint(newsSec) {
  const el = $("sflowNews");
  if (!el) return;
  const names = Object.keys(newsSec || {}).sort((a, b) => (newsSec[b].score || 0) - (newsSec[a].score || 0));
  if (!names.length) { el.innerHTML = ""; return; }
  el.innerHTML = "📰 盘前新闻关联板块：" + names.slice(0, 8).map(n => {
    const v = newsSec[n] || {};
    const bb = (v.bull || v.bear) ? (v.bull + "利好" + (v.bear ? "/" + v.bear + "利空" : "")) : (v.count + "条");
    return _pnEsc(n) + "(" + bb + ")";
  }).join(" · ");
}
let briefBusy = false;
function _briefDayStr(d) {
  return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
}
async function checkBrief(force) {
  try {
    const now = new Date();
    const day = _briefDayStr(now);
    const hm = now.getHours() * 100 + now.getMinutes();
    if (!force) {
      if (hm < 600 || hm > 940) return;
      if (localStorage.getItem("pnBriefDay") === day) return;
      const lastTry = parseInt(localStorage.getItem("pnBriefTry") || "0", 10);
      if (Date.now() - lastTry < 5 * 60 * 1000) return;
    }
    if (briefBusy) return;
    briefBusy = true;
    localStorage.setItem("pnBriefTry", String(Date.now()));
    const r = await fetchJson("/api/news/brief" + (force ? "?force=1" : ""));
    briefBusy = false;
    if (!r || r.error) return;
    if (!r.enabled) {
      if (force) showBriefEmpty(r.reason || "今日非交易日");
      return;
    }
    if (!r.items || !r.items.length) {
      if (force) showBriefEmpty("暂无达标新闻（重要度≥3），可稍后再试");
      return;
    }
    localStorage.setItem("pnBriefDay", day);
    showBriefModal(r);
  } catch (e) { briefBusy = false; }
}
function showBriefEmpty(reason) {
  $("briefMeta").textContent = "";
  $("briefBody").innerHTML = '<div class="placeholder small">' + _pnEsc(reason) + "</div>";
  $("briefModal").style.display = "flex";
}
function showBriefModal(r) {
  $("briefMeta").textContent = (r.day || "") + " · 更新 " + (r.fetched_at || "") + (r.ai_pending ? " · AI 评分中…" : "");
  $("briefBody").innerHTML = r.items.map(it => {
    const lvl = it.score >= 4 ? "hi" : (it.score >= 3 ? "mid" : "");
    return '<div class="brief-item ' + lvl + '"><span class="brief-score">' + it.score + '分</span><div class="brief-main">'
      + '<div class="brief-title">' + _pnEsc(it.title) + "</div>"
      + (it.why ? '<div class="brief-why">' + _pnEsc(it.why) + "</div>" : "")
      + '<div class="brief-meta"><span>' + _pnEsc(it.time || "") + "</span><span>" + _pnEsc(it.source || "") + "</span>"
      + (it.cat ? '<span class="brief-cat">' + _pnEsc(it.cat) + "</span>" : "")
      + "</div></div></div>";
  }).join("");
  $("briefModal").style.display = "flex";
}
$("btnBriefClose").addEventListener("click", () => $("briefModal").style.display = "none");
$("btnBriefOk").addEventListener("click", () => $("briefModal").style.display = "none");
$("btnBriefGo").addEventListener("click", () => { $("briefModal").style.display = "none"; switchPage("pnews"); });
const _pnBriefBtn = $("pnBriefBtn");
if (_pnBriefBtn) _pnBriefBtn.addEventListener("click", () => checkBrief(true));
setTimeout(() => checkBrief(false), 2500);
state.pnBriefTimer = setInterval(() => checkBrief(false), 30000);

/* ============ ★ 4.9 新闻板块视图（利好利空） ============ */
function _pnSecChips(secs) {
  return (secs || []).slice(0, 4).map(s => {
    const cls = s.dir > 0 ? "bull" : (s.dir < 0 ? "bear" : "flat");
    const arrow = s.dir > 0 ? "▲" : (s.dir < 0 ? "▼" : "");
    return '<span class="sec-chip ' + cls + '">' + _pnEsc(s.name) + arrow + "</span>";
  }).join("");
}
function renderPnewsSectors(r) {
  const secs = r.sectors || [];
  let statTxt = "更新 " + (r.fetched_at || "");
  if (r.ai_pending) statTxt += " · AI 评分中…";
  else if (r.ai_used) statTxt += " · AI 已评分";
  $("pnStatus").textContent = statTxt;
  const nb = secs.reduce((a, s) => a + (s.bull || 0), 0);
  const nk = secs.reduce((a, s) => a + (s.bear || 0), 0);
  $("pnStats").textContent = secs.length + " 个板块 · " + nb + " 利好 / " + nk + " 利空（仅含关联板块的新闻）";
  if (!secs.length) {
    $("pnList").innerHTML = '<div class="placeholder small">暂无关联板块的新闻，可切回「列表」视图查看或稍后重试</div>';
    return;
  }
  $("pnList").innerHTML = secs.map((s, i) => {
    const netCls = s.net > 0 ? "bull" : (s.net < 0 ? "bear" : "flat");
    const netTxt = s.net > 0 ? ("净利好 +" + s.net) : (s.net < 0 ? ("净利空 " + s.net) : "中性");
    const rows = (s.news || []).map(n => {
      const dCls = n.dir > 0 ? "bull" : (n.dir < 0 ? "bear" : "flat");
      const dTxt = n.dir > 0 ? "▲ 利好" : (n.dir < 0 ? "▼ 利空" : "· 相关");
      return '<div class="ps-row"><span class="ps-dir ' + dCls + '">' + dTxt + '</span>'
        + '<span class="ps-score">' + n.score + "</span>"
        + '<div class="ps-main"><div class="ps-title">' + _pnEsc(n.title) + "</div>"
        + (n.why ? '<div class="ps-why">' + _pnEsc(n.why) + "</div>" : "")
        + '<div class="ps-meta">' + _pnEsc(n.time || "") + " · " + _pnEsc(n.source || "") + "</div></div></div>";
    }).join("");
    return '<div class="ps-card' + (i < 3 ? " open" : "") + '">'
      + '<div class="ps-head"><span class="ps-name">' + _pnEsc(s.name) + "</span>"
      + (s.bull ? '<span class="ps-badge bull">' + s.bull + " 利好</span>" : "")
      + (s.bear ? '<span class="ps-badge bear">' + s.bear + " 利空</span>" : "")
      + (s.flat ? '<span class="ps-badge flat">' + s.flat + " 相关</span>" : "")
      + '<span class="ps-net ' + netCls + '">' + netTxt + "</span>"
      + '<span class="tip">' + s.count + " 条</span></div>"
      + '<div class="ps-body">' + rows + "</div></div>";
  }).join("");
  document.querySelectorAll("#pnList .ps-card .ps-head").forEach(h =>
    h.addEventListener("click", () => h.parentElement.classList.toggle("open")));
}
document.querySelectorAll("#pnViewTabs .tab").forEach(el =>
  el.addEventListener("click", () => {
    document.querySelectorAll("#pnViewTabs .tab").forEach(tb => tb.classList.remove("active"));
    el.classList.add("active");
    state.pnewsView = el.dataset.view;
    refreshPnews();
  }));

Object.assign(window, { startPnewsTimer, initPnews, refreshPnews, renderPnews, checkBrief, showBriefEmpty, showBriefModal, renderSflowNewsHint, renderPnewsSectors, _pnEsc, _pnSecChips });
