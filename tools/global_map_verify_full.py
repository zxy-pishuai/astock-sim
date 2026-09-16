# -*- coding: utf-8 -*-
"""★ D1：全球市场→A股映射规则的"真实全史"复验。

背景（任务书）：Phase39 复验时外部序列被源头截断——VIX 仅 2018-11 起（CBOE 在线
窗口）、WTI 降级用新浪 USO 代理。Phase63 全球源加固后，global_kline 已回补：
VIX 9258 根（1990 起，CBOE 官方源）、CL 7029 根（1999 起，东财 CL00Y 主源）。
本工具用真实全史重跑现行全部映射规则 + 港股科技±1% 变体，判定口径与 Phase39
完全一致（同保留门槛）；通过者列为前瞻台账候选。

复验范围（对应 app/global_market.py ASHARE_MAP_RULES 全部现行规则）：
  ① 半导体链   纳指 ±1.5% 且 英伟达 ±3%（bull/bear）
  ② 港股科技   恒科 ±2%（bull/bear）
  ③ 港股科技±1% 变体（Phase57 实验组首次回测）（bull/bear）
  ④ 油气       WTI ±3%（真实 CL00Y，不再用 USO）（bull/bear）
  ⑤ 全球避险   VIX > 25（flag 口径，绝对价位）
  ⑥ 汇率       USDCNH 日内 >0.3%——继续跳过：全球/免费渠道仍无可靠历史，
               global_kline 仅 4 行增量（<30 判定下限），报告如实注明。

口径不变（继承 Phase39）：外盘 d 收盘→t=d 后首个 A 股交易日（PIT）；
R1=close_t/open_t-1，R5=close_{t+4}/open_t-1；代理等权逐日在场成员；
无条件基线=全样本同口径；n≥30 且 R5 命中率≥55% 且方向超额≥1pp → 保留建议；
命中≥50% 且超额>0 时看 ±50% 阈值扰动决定"调阈值/下线"；否则下线。

与 Phase39 的实现差异（均为数据修正，非口径变更）：
  - VIX/CL 直接读本地 global_kline 全史（本工具内该表严格只读 mode=ro），
    不可得时才退回在线源并如实标注；不再施加 Phase39 的 2018-11 截断守卫。
  - 纳指/恒科/英伟达沿用 Phase39 同一数据源（腾讯 ifzq / 新浪美股日线），
    这些序列在 Phase39 已覆盖可评估窗（A 股全市场日线自 2019 年起，
    更早年份库内仅零星个股，代理组合不存在——这是窗口的真实边界）。

输出：data/global_map_verify_full.json（含 vs Phase39 对照与前瞻台账候选清单）。
红线：只读库（kline/global_kline 均 ro 连接）、只新增文件，不写任何表。
"""
import json
import os
import sqlite3
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)
_TOOLS = os.path.join(BASE, "tools")
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import global_map_verify as gmv          # 复用 Phase39 口径函数（只 import，main 不执行）
from app import config as C              # 只读：路径

OUT_JSON = os.path.join(BASE, "data", "global_map_verify_full.json")
OLD_JSON = gmv.OUT_JSON                  # Phase39 结果文件（对照用，只读）

GK_VIX_SYM = "VIX"
GK_CL_SYM = "CL"

# 全史注入后的 PIT 映射守卫：外盘日 d 只允许映射到日历间隔 <=14 天的下一个
# A 股交易日（14 天覆盖春节/国庆+调休的最长休市，实测窗口内最大真实间隔≈10 天）。
# 早于评估窗的历史日期因间隔数年而被自然拒绝——不会把 2008 年的触发"传送"
# 到序列首个交易日（这是移除 Phase39 截断守卫后必须补上的对齐纪律）。
_MAX_GAP_DAYS = 14


def _install_next_day_guard():
    """用带间隔上限的 next_ashare_day 替换 gmv 模块内的同名函数：
    collect_triggers 内部按模块全局名解析调用，替换模块属性即可全链生效。
    先捕获原函数引用（替换后再经模块属性调用会命中 _guarded 自身）。"""
    import datetime

    _orig_next = gmv.next_ashare_day

    def _guarded(d, ashare_dates_set, ashare_dates_sorted):
        t = _orig_next(d, ashare_dates_set, ashare_dates_sorted)
        if t is None:
            return None
        try:
            dd = datetime.date.fromisoformat(d)
            tt = datetime.date.fromisoformat(t)
        except ValueError:
            return t
        return t if 0 <= (tt - dd).days <= _MAX_GAP_DAYS else None

    gmv.next_ashare_day = _guarded


def _gk_bars(sym):
    """global_kline 只读取全史 [(date, close)] 升序。表缺失/异常返回 []。"""
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % C.DB_FILE, uri=True, timeout=15)
        try:
            rows = conn.execute(
                "SELECT date, close FROM global_kline WHERE sym=? "
                "ORDER BY date", (sym,)).fetchall()
        finally:
            conn.close()
        return sorted({d: float(c) for d, c in rows
                       if c is not None and c > 0}.items())
    except Exception:
        return []


def _dedup(bars):
    return sorted({d: float(c) for d, c in bars if c is not None}.items())


def _verdict_for(blk, direction, perturb):
    """Phase39 的判定逻辑原样复刻（verdict_for 在原脚本为 main 内嵌函数）：
    事先写死——保留/调阈值/下线。"""
    n = blk.get("count", 0)
    if n < 30:
        return "样本不足(%d)，不下结论" % n
    hit5, ex5 = blk["hit_r5"], blk["excess_r5_pp"]
    if hit5 >= 0.55 and ex5 >= 1.0:
        return "建议保留"
    if hit5 >= 0.50 and ex5 > 0:
        ok = [p for p in perturb
              if p.get("hit_r5") is not None and p["hit_r5"] >= 0.52
              and p.get("excess_r5_pp", 0) > 0]
        if len(ok) == len([p for p in perturb if p.get("count", 0) >= 30]) \
                and any(p.get("count", 0) >= 30 for p in perturb):
            return "建议调阈值（方向稳健但当前阈值不敏感）"
        return "建议下线（阈值扰动下不稳健）"
    return "建议下线（无方向信息）"


def run_rules(pcts, groups, all_dates, base_r1, base_r5, r1, r5):
    """五条方向型规则（含 ±1% 变体）。结构与 Phase39 完全一致。"""
    rules = [
        {"name": "半导体链", "series": ["usIXIC", "usNVDA"], "logic": "and",
         "thr": {"usIXIC": 1.5, "usNVDA": 3.0}, "proxy": "半导体链"},
        {"name": "港股科技±2%", "series": ["hkHSTECH"], "logic": "or",
         "thr": {"hkHSTECH": 2.0}, "proxy": "港股科技"},
        {"name": "港股科技±1%(变体)", "series": ["hkHSTECH"], "logic": "or",
         "thr": {"hkHSTECH": 1.0}, "proxy": "港股科技"},
        {"name": "油气(WTI真实)", "series": ["CL"], "logic": "or",
         "thr": {"CL": 3.0}, "proxy": "油气"},
    ]
    ashare_set = set(all_dates)
    out = []
    for rule in rules:
        entry = {"rule": rule["name"], "thresholds": rule["thr"],
                 "proxy": rule["proxy"],
                 "members": len(groups.get(rule["proxy"], []))}
        bulls, bears = gmv.collect_triggers(rule, pcts, all_dates, ashare_set)
        for tag, lst, direction in [("bull", bulls, 1), ("bear", bears, -1)]:
            blk = gmv.stat_block(lst, r1, r5, base_r1, base_r5, direction)
            blk["direction"] = "利多" if direction > 0 else "利空"
            perturb = []
            for mult in (0.5, 1.5):
                pl, pr = gmv.collect_triggers(rule, pcts, all_dates,
                                              ashare_set, mult=mult)
                pb = gmv.stat_block(pl if direction > 0 else pr,
                                    r1, r5, base_r1, base_r5, direction)
                perturb.append({"mult": mult, "count": pb.get("count", 0),
                                "hit_r5": pb.get("hit_r5"),
                                "excess_r5_pp": pb.get("excess_r5_pp")})
            blk["perturb"] = perturb
            blk["verdict"] = _verdict_for(blk, direction, perturb)
            entry[tag] = blk
        out.append(entry)
        print("%-16s 利多 n=%-4d[%s] 利空 n=%-4d[%s]" % (
            entry["rule"], entry["bull"]["count"],
            entry["bull"]["verdict"][:10], entry["bear"]["count"],
            entry["bear"]["verdict"][:10]), flush=True)
    return out


def main():
    t0 = time.time()
    notes = []
    _install_next_day_guard()

    # ---- A 股代理组合与基线（与 Phase39 同函数、同口径） ----
    groups, meta = gmv.load_proxy_members()
    for g, mem in groups.items():
        print("代理[%s]: %d 只成员" % (g, len(mem)), flush=True)
    all_members = sorted({c for mem in groups.values() for c, _, _ in mem})
    all_dates, idx, data = gmv.load_ashare_series(all_members)
    r1, r5 = gmv.build_portfolio(all_dates, idx, data)
    valid = [d for d in all_dates
             if r1.get(d) is not None and r5.get(d) is not None]
    base_r1 = sum(r1[d] for d in valid) / len(valid)
    base_r5 = sum(r5[d] for d in valid) / len(valid)
    ashare_set = set(all_dates)
    print("A股组合序列: %d 天（%s~%s）；无条件基线 R1=%.3f%% R5=%.3f%%" % (
        len(valid), valid[0], valid[-1], base_r1 * 100, base_r5 * 100),
        flush=True)

    # ---- 外盘序列（优先 global_kline 真实全史，缺时才在线退回并标注） ----
    bars_map = {}

    def put(key, bars):
        bars = _dedup(bars)
        if not bars:
            raise RuntimeError("%s 空序列" % key)
        bars_map[key] = {"bars": bars, "pct": gmv.pct_series(bars)}
        print("  %-9s %d 根（%s~%s）" % (
            key, len(bars), bars[0][0], bars[-1][0]), flush=True)

    print("拉取外盘序列（global_kline 优先）...", flush=True)

    # 纳指 / 恒生科技 / 英伟达：与 Phase39 同源（已覆盖 A 股可评估窗）
    _end = time.strftime("%Y-%m-%d")
    put("usIXIC", gmv._tx_kline("us.IXIC", end=_end))
    time.sleep(1.2)
    nv_raw = gmv._sina_us_daily("NVDA")
    nv, fixes = gmv._fix_splits(list(nv_raw))
    if fixes:
        print("  NVDA 拆股修正:", fixes, flush=True)
    put("usNVDA", nv)
    time.sleep(1.2)
    put("hkHSTECH", gmv._tx_kline("hkHSTECH", end=_end))
    time.sleep(1.2)

    # VIX：global_kline 全史（CBOE 官方源回补，1990 起）
    vix_src = "global_kline(sym=%s,CBOE官方全史)" % GK_VIX_SYM
    vix_bars = _gk_bars(GK_VIX_SYM)
    if len(vix_bars) < 30:
        notes.append("VIX 回退在线 CBOE（global_kline 行数=%d）" % len(vix_bars))
        vix_src = "cboe在线(窗口受限降级)"
        vix_bars = gmv.fetch_cboe_vix()
    put("usVIX_close", vix_bars)
    time.sleep(1.2)

    # WTI：global_kline 真实 CL00Y 全史（1999 起）；不再使用 USO 代理
    cl_src = "global_kline(sym=%s,东财CL00Y全史)" % GK_CL_SYM
    cl_bars = _gk_bars(GK_CL_SYM)
    if len(cl_bars) < 30:
        notes.append("CL 回退东财在线（global_kline 行数=%d）" % len(cl_bars))
        try:
            cl_bars = gmv.fetch_em_close("102.CL00Y")[1]
            cl_src = "eastmoney 102.CL00Y(在线)"
        except Exception as e:
            notes.append("东财 WTI 失败(%s)，降级新浪 USO（口径注记：ETF 含展期损耗）"
                         % str(e)[:40])
            cl_src = "sina USO(WTI期货ETF代理,降级)"
            cl_bars = gmv._sina_us_daily("USO")
    put("CL", cl_bars)

    pcts = {k: v["pct"] for k, v in bars_map.items() if k != "usVIX_close"}

    # ---- 规则统计（同保留门槛） ----
    print("规则统计（同 Phase39 保留门槛）...", flush=True)
    out_rules = run_rules(pcts, groups, all_dates, base_r1, base_r5, r1, r5)

    # ---- VIX>25（flag 口径，绝对价位） ----
    vix_close = dict(bars_map["usVIX_close"]["bars"])
    vt, seen_vt = [], set()
    for d in sorted(vix_close):
        if vix_close[d] > 25.0:
            t = gmv.next_ashare_day(d, ashare_set, all_dates)
            if t and t not in seen_vt and r1.get(t) is not None \
                    and r5.get(t) is not None:
                seen_vt.add(t)
                vt.append((t, r1[t], r5[t]))
    vix_blk = {"count": len(vt),
               "note": "VIX>25 为无方向避险提示，不设方向命中率；对照无条件基线看均值偏移"}
    if vt:
        m1 = sum(x[1] for x in vt) / len(vt)
        m5 = sum(x[2] for x in vt) / len(vt)
        ex5 = (m5 - base_r5) * 100
        vix_blk.update({
            "mean_r1": round(m1, 5), "mean_r5": round(m5, 5),
            "down_ratio_r1": round(sum(1 for x in vt if x[1] < 0) / len(vt), 4),
            "excess_r5_pp": round(ex5, 3),
            "verdict": ("建议保留（触发后显著走低）"
                        if len(vt) >= 30 and ex5 <= -1.0
                        else "维持纯展示（触发后相对基线无避险性偏移）"),
            "samples": [{"t": t, "r1": round(a, 5), "r5": round(b, 5)}
                        for t, a, b in vt[:gmv.SAMPLE_N]],
        })
    else:
        vix_blk["verdict"] = "样本不足"
    out_rules.append({"rule": "全球避险(VIX>25)", "trigger": vix_blk})
    print("%-16s flag n=%-4d[%s]" % ("全球避险(VIX>25)", vix_blk["count"],
                                     vix_blk["verdict"][:12]), flush=True)

    # ---- 汇率：如实跳过 ----
    fx_n = len(_gk_bars("USDCNH"))
    out_rules.append({
        "rule": "汇率(USDCNH日内>0.3%)", "skipped": True,
        "reason": "仍无可回测历史：免费渠道仅有实时报价（Phase35/39 实测），"
                  "global_kline USDCNH 仅 %d 行增量(<30)，日内口径亦无法从日K还原"
                  % fx_n})

    # ---- 与 Phase39 对照 ----
    compare = {}
    try:
        with open(OLD_JSON, encoding="utf-8") as f:
            old = json.load(f)
        for r in old.get("rules") or []:
            nm = r.get("rule", "")
            for tag in ("bull", "bear"):
                b = r.get(tag)
                if b:
                    compare["%s|%s" % (nm, tag)] = {
                        "count": b.get("count"), "hit_r5": b.get("hit_r5"),
                        "excess_r5_pp": b.get("excess_r5_pp"),
                        "verdict": (b.get("verdict") or "")}
            tr = r.get("trigger")
            if tr:
                compare["%s|flag" % nm] = {
                    "count": tr.get("count"),
                    "hit_r5": None,
                    "excess_r5_pp": tr.get("excess_r5_pp"),
                    "verdict": (tr.get("verdict") or "")}
    except Exception as e:
        notes.append("Phase39 对照读取失败: %s" % e)

    def attach_compare(rules):
        alias = {"港股科技±2%": "港股科技",
                 "油气(WTI真实)": "油气",
                 "半导体链": "半导体链"}
        for e in rules:
            if e.get("skipped"):
                continue
            if "trigger" in e:
                k = "%s|flag" % e["rule"]
                e["phase39"] = compare.get(k)
                continue
            old_nm = alias.get(e["rule"])
            if not old_nm:
                continue
            for tag in ("bull", "bear"):
                ok = "%s|%s" % (old_nm, tag)
                if ok in compare:
                    e[tag]["phase39"] = compare[ok]

    attach_compare(out_rules)

    # ---- 前瞻台账候选（同保留门槛通过者） ----
    candidates = []
    for e in out_rules:
        if e.get("skipped"):
            continue
        if "trigger" in e:
            if str(e["trigger"].get("verdict", "")).startswith("建议保留"):
                candidates.append({
                    "rule": e["rule"], "direction": "flag",
                    "n": e["trigger"].get("count"),
                    "excess_r5_pp": e["trigger"].get("excess_r5_pp"),
                    "verdict": e["trigger"].get("verdict")})
            continue
        for tag, dr in (("bull", "利多"), ("bear", "利空")):
            blk = e.get(tag) or {}
            if str(blk.get("verdict", "")).startswith("建议保留"):
                candidates.append({
                    "rule": e["rule"], "direction": dr,
                    "n": blk.get("count"), "hit_r5": blk.get("hit_r5"),
                    "excess_r5_pp": blk.get("excess_r5_pp"),
                    "verdict": blk.get("verdict")})

    src_note = {
        "series": {
            "usIXIC": "腾讯 ifzq us.IXIC 分页（同 Phase39，2019-01 起，"
                      "覆盖 A 股可评估窗）",
            "usNVDA": "新浪美股日线全史+拆股修正（同 Phase39）",
            "hkHSTECH": "腾讯 ifzq hkHSTECH（指数基期 2020-07-27 起，天然上限）",
            "usVIX_close": vix_src + "：%d 根（%s~%s）" % (
                len(bars_map["usVIX_close"]["bars"]),
                bars_map["usVIX_close"]["bars"][0][0],
                bars_map["usVIX_close"]["bars"][-1][0]),
            "CL": cl_src + "：%d 根（%s~%s）" % (
                len(bars_map["CL"]["bars"]),
                bars_map["CL"]["bars"][0][0],
                bars_map["CL"]["bars"][-1][0]),
        },
        "window_boundary": "A 股全市场日线库内自 2019 年起（2018 及以前仅零星个票），"
                           "故触发评估窗与 Phase39 相同；'全史'修正作用于外盘序列的"
                           "完整性与真实性（VIX 不再 2018-11 截断、WTI 弃 USO 用真实 "
                           "CL00Y），其早于评估窗的部分按 PIT 映射自然并入首个交易日。",
        "notes": notes,
    }

    doc = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "task": "D1 全球映射规则真实全史复验",
        "phase_ref": "Phase39 口径继承 + Phase63 数据修正",
        "method": gmv_verdict_method(),
        "baseline": {"r1_pp": round(base_r1 * 100, 3),
                     "r5_pp": round(base_r5 * 100, 3), "days": len(valid)},
        "proxies": {g: {"count": len(mem),
                        "sample": [(c, n) for c, n, _ in mem[:15]]}
                    for g, mem in groups.items()},
        "meta": meta,
        "sources": src_note,
        "rules": out_rules,
        "forward_candidates": candidates,
        "candidates_note": ("" if candidates else
                            "本轮无规则方向达到'保留'门槛 → 无新增前瞻台账候选，"
                            "既有「港股科技±1%」实验组照常按前瞻记账积累。"),
        "elapsed_sec": round(time.time() - t0, 1),
    }
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    print("\n已写出 %s（耗时 %.0fs）" % (OUT_JSON, time.time() - t0), flush=True)
    print("前瞻台账候选: %d 条" % len(candidates))
    for cd in candidates:
        print("  ★ %s [%s] n=%s hit_r5=%s excess=%spp" % (
            cd["rule"], cd["direction"], cd.get("n"), cd.get("hit_r5"),
            cd.get("excess_r5_pp")), flush=True)


def gmv_verdict_method():
    return ("与 Phase39 一致：外盘T收盘→A股T+1交易日(PIT)；R1=close/open-1,"
            "R5=close_{t+4}/open_t-1；代理等权逐日在场成员；无条件基线=全样本同口径；"
            "保留门槛 n>=30 且 R5命中率>=55% 且方向超额>=1pp；扰动 ±50% 决定调阈值/下线")


if __name__ == "__main__":
    main()
