/* W5 实验台账页 — 独立命名空间，不侵入 app.js
 * 数据源: GET /api/experiments (data/experiments_index.json 缓存; ?refresh=1 重扫)
 * 纪律: 一切解析出的文本进 DOM 一律 textContent（防 XSS）；echarts 不强制。
 */
(function () {
  "use strict";

  var NS = "ExperimentsLedger";
  var state = { data: null, filter: { q: "", status: "all", pid: "" }, sort: "date" };
  var els = {};

  function $(id) { return document.getElementById(id); }

  var STATUS_META = {
    passed:  { label: "✅ 落地/通过", cls: "exp-badge-pass" },
    failed:  { label: "❌ 否决/不通过", cls: "exp-badge-fail" },
    running: { label: "⏳ 在跑", cls: "exp-badge-run" },
    pending: { label: "⏳ 待办/挂起", cls: "exp-badge-pend" },
    partial: { label: "◐ 部分通过", cls: "exp-badge-part" },
    unknown: { label: "— 未知", cls: "exp-badge-unk" }
  };

  /* ---------- 工具 ---------- */
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;   // XSS 安全
    return e;
  }

  function esc(s) { return String(s == null ? "" : s); }

  function fmtNum(v) {
    if (v == null || isNaN(v)) return "—";
    return Number(v).toFixed(2);
  }

  /* ---------- 统计条 ---------- */
  function renderStats() {
    var c = (state.data && state.data.counts) || {};
    var cards = [
      { k: "experiments", label: "实验总数", icon: "🗂" },
      { k: "passed", label: "✅ 落地/通过", icon: "✅" },
      { k: "failed", label: "❌ 否决/不通过", icon: "❌" },
      { k: "running", label: "⏳ 在跑", icon: "⏳" },
      { k: "recent7", label: "近 7 日新增", icon: "🆕" }
    ];
    var box = $("exp-stats");
    box.innerHTML = "";
    cards.forEach(function (card) {
      var v = c[card.k];
      if (card.k === "running") v = (c.running || 0) + (c.pending || 0) + (c.partial || 0);
      var d = el("div", "exp-stat");
      var n = el("div", "exp-stat-n", v == null ? "0" : String(v));
      var l = el("div", "exp-stat-l", card.icon + " " + card.label);
      d.appendChild(n); d.appendChild(l);
      box.appendChild(d);
    });
    var meta = el("div", "exp-meta");
    meta.textContent = "索引时间 " + esc((state.data && state.data.generated_at) || "—") +
      (state.data && state.data.scan_ms != null ? " · 扫描 " + state.data.scan_ms + "ms" : "");
    box.appendChild(meta);
    fillPidOptions();
  }

  function fillPidOptions() {
    var sel = $("exp-filter-pid");
    if (!sel) return;
    var cur = state.filter.pid;
    var set = {};
    ((state.data && state.data.experiments) || []).forEach(function (e) {
      (e.p_numbers || []).forEach(function (p) { set[p] = true; });
    });
    sel.innerHTML = "";
    var opt0 = document.createElement("option");
    opt0.value = ""; opt0.textContent = "全部 P 编号";
    sel.appendChild(opt0);
    Object.keys(set).sort().forEach(function (p) {
      var o = document.createElement("option");
      o.value = p; o.textContent = p;
      sel.appendChild(o);
    });
    if (cur) sel.value = cur;
  }

  /* ---------- 迷你数值条（纯 CSS） ---------- */
  function miniBar(val, maxAbs) {
    var wrap = el("div", "exp-minibar");
    if (val == null || isNaN(val)) { wrap.textContent = "—"; return wrap; }
    maxAbs = maxAbs || 25;
    var v = Number(val);
    var pct = Math.min(100, Math.abs(v) / maxAbs * 100);
    var bar = el("div", "exp-minibar-fill" + (v >= 0 ? " pos" : " neg"));
    bar.style.width = pct.toFixed(0) + "%";
    wrap.appendChild(bar);
    wrap.appendChild(el("span", "exp-minibar-v", v >= 0 ? "+" + v.toFixed(1) : v.toFixed(1)));
    return wrap;
  }

  /* ---------- 主表 ---------- */
  function rowOf(e) {
    var tr = document.createElement("tr");
    tr.className = "exp-row";

    // 日期
    tr.appendChild(el("td", "exp-c-date", esc(e.date || "—")));
    // 名称（含类型标）
    var nameTd = el("td", "exp-c-name");
    var typeTag = el("span", "exp-type-tag exp-type-" + e.type, e.type === "bt_json" ? "回测" : e.type === "report" ? "报告" : "台账");
    nameTd.appendChild(typeTag);
    nameTd.appendChild(el("span", "", esc(e.name)));
    if (e.p_numbers && e.p_numbers.length) {
      var pn = el("span", "exp-pnums", e.p_numbers.slice(0, 4).join(" "));
      nameTd.appendChild(pn);
    }
    tr.appendChild(nameTd);

    // 快照口径
    tr.appendChild(el("td", "exp-c-snap", e.snapshot ? "📌 " + esc(e.snapshot) : "—"));

    // 状态徽章
    var st = STATUS_META[e.status] || STATUS_META.unknown;
    var stTd = el("td", "exp-c-status");
    var badge = el("span", "exp-badge " + st.cls, st.label);
    stTd.appendChild(badge);
    if (e.status_hint) stTd.appendChild(el("span", "exp-hint", " " + esc(e.status_hint)));
    tr.appendChild(stTd);

    // 结论倾向
    var concl = STATUS_META[e.conclusion] || STATUS_META.unknown;
    tr.appendChild(el("td", "exp-c-concl", concl.label.replace(/^[^ ]+/, "").trim()));

    // 关键数字（迷你条）
    var knTd = el("td", "exp-c-kn");
    var kn = e.key_numbers || {};
    var barSource = null;
    if (kn.pit_bull_loss != null) barSource = -Number(kn.pit_bull_loss);
    else if (kn.bull_loss != null) barSource = -Number(kn.bull_loss);
    else if (kn.bull_delta_pp != null) barSource = Number(kn.bull_delta_pp);
    else if (kn.delta_min != null) barSource = Number(kn.delta_min);
    if (barSource != null) knTd.appendChild(miniBar(barSource));
    var ktxt = [];
    if (kn.judge_pass) ktxt.push("判定 " + kn.judge_pass);
    if (kn.pit_pass != null) ktxt.push("PIT:" + (kn.pit_pass ? "过" : "否"));
    if (kn.static_pass != null) ktxt.push("静态:" + (kn.static_pass ? "过" : "否"));
    if (kn.delta_min != null) ktxt.push("Δ[" + fmtNum(kn.delta_min) + "," + fmtNum(kn.delta_max) + "]");
    if (kn.enabled != null) ktxt.push("落地:" + (kn.enabled ? "是" : "否"));
    if (ktxt.length) knTd.appendChild(el("div", "exp-kn-text", ktxt.join(" · ")));
    tr.appendChild(knTd);

    // 证据/出处
    var srcTd = el("td", "exp-c-src");
    srcTd.appendChild(el("div", "exp-src-path", esc(e.source || "")));
    if (e.md_path) srcTd.appendChild(el("div", "exp-src-path dim", "📄 " + esc(e.md_path)));
    tr.appendChild(srcTd);

    // 判定摘要
    var vTd = el("td", "exp-c-v");
    if (e.verdict) vTd.appendChild(el("div", "exp-verdict", esc(e.verdict)));
    if (e.prereg) vTd.appendChild(el("div", "exp-prereg", "预注册 " + esc(e.prereg)));
    tr.appendChild(vTd);

    return tr;
  }

  function applyFilters(list) {
    var f = state.filter;
    var q = f.q.trim().toLowerCase();
    return list.filter(function (e) {
      if (f.status !== "all" && e.status !== f.status) return false;
      if (f.pid && !(e.p_numbers || []).some(function (p) { return p === f.pid; })) return false;
      if (q) {
        var hay = (e.name + " " + (e.source || "") + " " + (e.verdict || "") + " " + (e.id || "")).toLowerCase();
        if (hay.indexOf(q) < 0) return false;
      }
      return true;
    });
  }

  function sortKey(e) {
    if (state.sort === "date") return (e.date || "0000-00-00");
    // 结论强度: passed > partial > pending/running > unknown > failed
    var order = { passed: 0, partial: 1, pending: 2, running: 2, unknown: 3, failed: 4 };
    return String(order[e.status] != null ? order[e.status] : 5);
  }

  function renderTable() {
    var list = (state.data && state.data.experiments) || [];
    var filtered = applyFilters(list);
    filtered.sort(function (a, b) { return sortKey(a) < sortKey(b) ? -1 : 1; });
    if (state.sort === "date") filtered.reverse();
    var tbody = $("exp-tbody");
    tbody.innerHTML = "";
    if (!filtered.length) {
      var tr = document.createElement("tr");
      tr.appendChild(el("td", "", "无匹配实验（调整过滤条件）"));
      tr.firstChild.colSpan = 7;
      tbody.appendChild(tr);
      return;
    }
    filtered.forEach(function (e) { tbody.appendChild(rowOf(e)); });
    $("exp-count").textContent = "共 " + filtered.length + " / " + list.length + " 项";
  }

  /* ---------- 声明库子页签 ---------- */
  function renderLibrary() {
    // 风险看板
    var risks = (state.data && state.data.backlog_risks) || [];
    var rbox = $("exp-risks");
    rbox.innerHTML = "";
    if (!risks.length) {
      rbox.appendChild(el("div", "exp-meta", "无风险条目"));
    } else {
      risks.forEach(function (r, i) {
        var d = el("div", "exp-risk");
        d.appendChild(el("span", "exp-risk-n", String(i)));
        d.appendChild(el("span", "exp-risk-t", r));
        rbox.appendChild(d);
      });
    }
    // P64 overstatement: 从各 bt_json 的 key_numbers 规则抽取 overstatement/beautify.overstatement_pp
    var list = (state.data && state.data.experiments) || [];
    var ovRows = [];
    list.forEach(function (e) {
      if (e.type !== "bt_json") return;
      var kn = e.key_numbers || {};
      var found = [];
      Object.keys(kn).forEach(function (k) {
        if (k.indexOf("overstatement") >= 0) found.push([k.replace(/^.*overstatement[._]?/, ""), kn[k]]);
      });
      if (found.length) {
        ovRows.push({ id: e.id, snapshot: e.snapshot || "—", items: found });
      }
    });
    var obox = $("exp-over");
    obox.innerHTML = "";
    if (!ovRows.length) {
      var n = el("div", "exp-meta", "未解析到 overstatement 数据（bt_*.json schema 不含该字段时留空，规则解析见 tools/experiment_scan.py）");
      obox.appendChild(n);
    } else {
      ovRows.forEach(function (r) {
        var d = el("div", "exp-ov-row");
        d.appendChild(el("span", "exp-ov-id", r.id + "（" + r.snapshot + "）"));
        r.items.forEach(function (it) {
          d.appendChild(el("span", "exp-ov-item", it[0] + "=" + esc(it[1])));
        });
        obox.appendChild(d);
      });
    }
  }

  /* ---------- 刷新 & 初始化 ---------- */
  function fetchData(withRefresh, done) {
    var url = "/api/experiments" + (withRefresh ? "?refresh=1" : "");
    fetch(url)
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d.error) { $("exp-err").textContent = "索引加载失败: " + d.error; return; }
        $("exp-err").textContent = "";
        state.data = d;
        renderStats(); renderTable(); renderLibrary();
        if (done) done();
      })
      .catch(function (err) {
        $("exp-err").textContent = "请求失败: " + err.message;
        if (done) done();
      });
  }

  function bindUI() {
    var q = $("exp-q");
    q.addEventListener("input", function () { state.filter.q = q.value; renderTable(); });
    var st = $("exp-filter-status");
    st.addEventListener("change", function () { state.filter.status = st.value; renderTable(); });
    var pid = $("exp-filter-pid");
    pid.addEventListener("change", function () { state.filter.pid = pid.value; renderTable(); });
    var sort = $("exp-sort");
    sort.addEventListener("change", function () { state.sort = sort.value; renderTable(); });
    $("exp-refresh").addEventListener("click", function () {
      var btn = $("exp-refresh");
      btn.textContent = "刷新中…"; btn.disabled = true;
      fetchData(true, function () { btn.textContent = "↻ 刷新"; btn.disabled = false; });
    });
    // 子页签切换
    ["tab-ledger", "tab-lib"].forEach(function (id) {
      $(id).addEventListener("click", function () {
        document.querySelectorAll(".exp-tab").forEach(function (t) { t.classList.remove("active"); });
        $(id).classList.add("active");
        $("exp-ledger-pane").style.display = id === "tab-ledger" ? "" : "none";
        $("exp-lib-pane").style.display = id === "tab-lib" ? "" : "none";
      });
    });
  }

  function init() {
    // 只初始化一次；页面由 app.js switchPage 切换（display 控制），本页预渲染
    if (window[NS]) return;
    bindUI();
    fetchData(false);
    window[NS] = { refresh: function () { fetchData(true); } };
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
