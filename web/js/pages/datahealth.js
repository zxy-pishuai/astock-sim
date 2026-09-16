/* ===== J5 datahealth：从 app.js 2538-2693 行切分（函数体逐字保留，window 桥接全局） ===== */
/* ============ 数据健康面板（D3 2026-09-13）============
 * 只读 /api/data_health；三态：ok 绿 / warn 黄 / err 红。
 * stale=true（库锁/断库降级）→ 整体灰显并显示"上次缓存"时间。
 * 判级规则（预注册，与报告 §判级 一致）：
 *   覆盖率 ratio>=0.95 ok / >=0.85 warn / else err
 *   amount 缺失 ratio==0 ok / <=0.05 warn / else err
 *   快照 deferred→err；age<=3 ok / <=7 warn / else err
 *   指数 任一 age>5 err / 任一 age>3 warn / amount 不可用→warn
 *   ML 打分 age<=7 ok / <=15 warn / >15 err（ml_scores 与 ml_pred 同规）
 *   告警 CRIT>0→err；WARN>3→err；WARN>1→warn；else ok
 */
const DH_COL = { ok: "#27ae60", warn: "#f39c12", err: "#e74c3c" };

function dhPct(x) { return x == null ? "—" : (x * 100).toFixed(1) + "%"; }

function dhCard(title, level, bodyHtml, reason) {
  const c = DH_COL[level] || "#888";
  return `<div style="border:1px solid ${c}55;border-left:4px solid ${c};border-radius:8px;padding:10px 12px;background:#1a1d24;">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
      <b style="font-size:13px">${title}</b>
      <span style="font-size:11px;color:${c};font-weight:600">${level === "ok" ? "正常" : level === "warn" ? "注意" : "异常"}</span>
    </div>
    ${bodyHtml}
    ${reason ? `<div style="margin-top:6px;font-size:12px;color:${c}">⚠ ${reason}</div>` : ""}
  </div>`;
}

function dhBar7(trend, valFn, labelFn) {
  if (!trend || !trend.length) return `<div style="font-size:12px;color:#888">无 7 日数据</div>`;
  const max = Math.max.apply(null, trend.map(t => valFn(t) || 0)) || 1;
  return `<div style="display:flex;align-items:flex-end;gap:4px;height:44px;padding-top:4px">
    ${trend.map(t => {
      const v = valFn(t) || 0;
      const h = Math.round(v / max * 36) + 2;
      return `<div style="flex:1;text-align:center">
        <div style="height:${h}px;background:#4a6fa5;border-radius:2px;min-width:6px"></div>
        <div style="font-size:9px;color:#999;margin-top:2px">${labelFn(t)}</div>
      </div>`;
    }).join("")}
  </div>`;
}

function dhHealthCards(d) {
  const cards = [];
  // 1) 日K覆盖率
  const kc = d.kline_coverage || {};
  const kt = (kc.trend7 || []).slice(-1)[0] || {};
  const kr = kt.ratio;
  const kl = kr == null ? "warn" : (kr >= 0.95 ? "ok" : (kr >= 0.85 ? "warn" : "err"));
  const kReason = kl === "err" ? `覆盖率 ${dhPct(kr)} < 85%（最新交易日 ${kt.date || kc.latest_date || "—"}）`
    : kl === "warn" ? (kr == null ? "覆盖率分母计算中（后台线程）" : `覆盖率 ${dhPct(kr)}，注意`)
    : null;
  cards.push(dhCard("日K覆盖率", kl, `
    <div style="font-size:22px;font-weight:700">${dhPct(kr)}</div>
    <div style="font-size:12px;color:#aaa">最新 ${kt.date || kc.latest_date || "—"}：${kt.rows ?? "—"}/${kc.total_codes ?? "—"} 只
      · 今日(自然日 ${d.today})${kc.today_rows ?? 0} 行</div>
    ${dhBar7(kc.trend7, t => t.rows, t => (t.date || "").slice(5))}`, kReason));

  // 2) amount 完整性
  const am = d.amount || {};
  const at = (am.trend7 || []).slice(-1)[0] || {};
  const ar = at.amount_ratio;
  const al = ar == null ? "ok" : (ar === 0 ? "ok" : (ar <= 0.05 ? "warn" : "err"));
  const aReason = al === "err" ? `amount=0 占比 ${dhPct(ar)} > 5%（缺成交额源）`
    : al === "warn" ? `amount=0 占比 ${dhPct(ar)} ≤ 5%`
    : null;
  cards.push(dhCard("amount 完整性", al, `
    <div style="font-size:22px;font-weight:700">${dhPct(ar)}</div>
    <div style="font-size:12px;color:#aaa">最新 ${at.date || am.latest_date || "—"}：amount=0 ${at.amount0 ?? "—"}/${at.rows ?? "—"} 行</div>
    ${dhBar7(am.trend7, t => t.amount0, t => (t.date || "").slice(5))}`, aReason));

  // 3) 快照
  const sn = d.snapshots || {};
  const sl = sn.deferred ? "err" : (sn.age_days == null ? "err" : (sn.age_days <= 3 ? "ok" : (sn.age_days <= 7 ? "warn" : "err")));
  const sReason = sn.deferred ? (sn.deferred_reason || "快照延迟") : null;
  cards.push(dhCard("快照状态", sl, `
    <div style="font-size:22px;font-weight:700">${sn.latest || "无快照"}</div>
    <div style="font-size:12px;color:#aaa">距今天数 ${sn.age_days == null ? "—" : sn.age_days + " 天"} · 共 ${sn.count ?? 0} 枚</div>
    <div style="margin-top:6px;font-size:11px;color:#aaa">最近 7 日：${(sn.last7 || []).map(x => `${x.date.slice(5)}${x.exists ? "●" : "○"}`).join(" ")}</div>`, sReason));

  // 4) 指数新鲜度
  const ix = (d.indices || {}).items || {};
  let iErr = 0, iWarn = 0, iRows = [];
  Object.keys(ix).forEach(code => {
    const it = ix[code] || {};
    const age = it.age_days;
    if (age == null) iErr++;
    else if (age > 5) iErr++;
    else if (age > 3) iWarn++;
    if (it.amount_available === false) iWarn++;
    iRows.push(`<div style="font-size:12px;color:#ccc;margin:2px 0">${code}：${it.last_date || "—"}（${age == null ? "—" : age + "天"}）${it.amount_available === false ? "· amount 不可用" : ""}</div>`);
  });
  const il = iErr ? "err" : (iWarn ? "warn" : "ok");
  const iReason = iErr ? "指数日K落后超过 5 天或有缺失" : (iWarn ? "指数有注意项" : null);
  cards.push(dhCard("指数新鲜度", il, `
    <div style="font-size:13px;font-weight:700">${Object.keys(ix).length} 个指数</div>
    ${iRows.join("")}`, iReason));

  // 5) ML 新鲜度
  const ml = d.ml || {};
  const m1 = ml.ml_scores || {};
  const m2 = ml.ml_pred || {};
  function mlLevel(age) { return age == null ? "warn" : (age <= 7 ? "ok" : (age <= 15 ? "warn" : "err")); }
  const m1l = mlLevel(m1.age_days), m2l = mlLevel(m2.age_days);
  const mlL = (m1l === "err" || m2l === "err") ? "err" : ((m1l === "warn" || m2l === "warn") ? "warn" : "ok");
  const mlReasons = [];
  if (m1l === "err") mlReasons.push(`ML 打分 ${m1.signal_date || "—"} 已 ${m1.age_days} 天未更新`);
  if (m2l === "err") mlReasons.push(`ml_pred 预测 ${m2.last_pred_date || "—"} 已 ${m2.age_days} 天未更新`);
  cards.push(dhCard("ML 新鲜度", mlL, `
    <div style="font-size:13px;font-weight:700">打分日期 ${m1.signal_date || "—"}</div>
    <div style="font-size:12px;color:#aaa">ml_scores：${m1.signal_date || "无"}（${m1.age_days == null ? "—" : m1.age_days + "天"}）· ${m1.n_scores ?? 0} 只
      <br>ml_pred：${m2.last_pred_date || "无"}（${m2.age_days == null ? "—" : m2.age_days + "天"}）</div>`,
    mlReasons.join("；") || null));

  // 6) 未处置告警
  const al7 = d.alerts || {};
  const alL = (al7.critical || 0) > 0 ? "err" : ((al7.warn || 0) > 3 ? "err" : ((al7.warn || 0) > 1 ? "warn" : "ok"));
  const a7Reason = (al7.critical || 0) > 0 ? `近7天 CRITICAL ${al7.critical} 条` : ((al7.warn || 0) > 1 ? `近7天 WARN ${al7.warn} 条` : null);
  cards.push(dhCard("未处置告警", alL, `
    <div style="font-size:22px;font-weight:700">${al7.warn || 0} WARN · ${al7.critical || 0} CRIT</div>
    <div style="font-size:12px;color:#aaa;margin-top:4px">${(al7.recent || []).slice(0, 4).map(r =>
      `<div style="margin:2px 0">${(r.date || "").slice(5, 16)} [${r.level}] ${r.event}${r.target_day ? " →" + r.target_day : ""}</div>`).join("") || "近7天无告警"}</div>`, a7Reason));

  // 7) 存储
  const st = d.storage || {};
  const stL = st.error ? "warn" : "ok";
  const mb = x => x == null ? "统计中" : (x / 1048576).toFixed(0) + " MB";
  cards.push(dhCard("存储占用", stL, `
    <div style="font-size:20px;font-weight:700">${mb(st.data_total_bytes)}</div>
    <div style="font-size:12px;color:#aaa">快照 ${mb(st.snapshots_bytes)} · 近7日增量 ${mb(st.growth7_bytes)}
      <br><span style="color:#777;font-size:11px">${st.growth7_note || ""}</span></div>`, st.error));
  return { cards, levels: [kl, al, sl, il, mlL, alL, stL] };
}

async function refreshDataHealth() {
  const el = $("dhCards");
  try {
    const d = await fetchJson("/api/data_health");
    $("dhUpdated").textContent = "更新时间 " + d.generated_at + (d.stale ? "（降级）" : "");
    const r = dhHealthCards(d);
    // 总状态 = 最差
    const order = ["ok", "warn", "err"];
    const worst = order[Math.max.apply(null, r.levels.map(l => order.indexOf(l)))];
    const c = DH_COL[worst];
    const staleBadge = d.stale ? `<span style="color:#888;font-size:12px;margin-left:8px">⚠ 库连接降级，显示上次缓存 ${d.generated_at}（${d.stale_reason || ""}）</span>` : "";
    $("dhStatus").innerHTML = `<div style="display:inline-block;padding:6px 14px;border-radius:20px;font-weight:700;color:#fff;background:${c}">
      ${worst === "ok" ? "🟢 数据健康" : worst === "warn" ? "🟡 有注意项" : "🔴 数据异常"}</div>${staleBadge}`;
    el.innerHTML = r.cards.join("");
    $("dhError").style.display = "none";
  } catch (e) {
    $("dhStatus").innerHTML = `<div style="display:inline-block;padding:6px 14px;border-radius:20px;font-weight:700;color:#fff;background:#888">⚪ 数据健康（断网/服务不可用）</div>`;
    el.innerHTML = `<div style="grid-column:1/-1;font-size:13px;color:#888;padding:20px;border:1px dashed #555;border-radius:8px">
      无法连接服务（${e.message}）。前端无上次缓存时显示此占位，不报错。</div>`;
  }
}


Object.assign(window, { refreshDataHealth });
