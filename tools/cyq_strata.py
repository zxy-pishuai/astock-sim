# -*- coding: utf-8 -*-
"""C2b｜筹码分位分层检验（C2 的换尺重测，全新预注册）
设计：盲买宇宙（E1）+ 五分位分层 + 买点日采样。
- 事件：event_mask(pre=7,brk=20,vol='OR',trend=True)，IS=2019-2023 / OOS=2024-2026
- 交易：事件 t 次一交易日开盘买入（t+1 开盘），yang9_M 卖出（_sell_path 语义）
- 筹码：买点日(t+1)收盘行指标主采样；事件日(t)对照列；seam=1 剔除；无 cache 行剔除
- 分层：4 指标 × IS/OOS 五分位（IS 切点映射 OOS）
- 判过：Q5-Q1 同号且 OOS|Δ|≥2pp；Spearman ρ 同号且 OOS|ρ|≥0.5；分年不反转；每档 OOS n≥500
只读；禁出网。
"""
import os, sys, json, time
import numpy as np
import pandas as pd
from scipy import stats

sys.path.insert(0, r"C:\Users\26838\A股模拟盘\tools")
import tactic_backtest as tb

ROOT = tb.ROOT
CACHE_DIR = os.path.join(ROOT, "data", "cyq_cache")
IS, IS_END = "2019-01-01", "2023-12-31"
OOS_S, OOS_E = "2024-01-01", "2026-08-31"
T0 = time.time()

INDICATORS = ["winner_pct", "conc90", "peak_dist_pct", "peak_density"]

_cache = {}
def load_cyq(code6):
    if code6 not in _cache:
        p = os.path.join(CACHE_DIR, code6 + ".parquet")
        _cache[code6] = pd.read_parquet(p) if os.path.exists(p) else None
    return _cache[code6]

def cyq_at(code6, fdate):
    cdf = load_cyq(code6)
    if cdf is None:
        return None, "no_cache"
    sub = cdf[cdf["date"] == fdate]
    if len(sub) == 0:
        return None, "no_row"
    r = sub.iloc[0]
    if r["seam_flag"] == 1:
        return None, "seam"
    return r, "ok"

def build_samples(start, end):
    """E1 盲买样本：事件 t → t+1 开盘买入 → yang9_M 卖出。返回样本 dict 列表。"""
    ev = np.flatnonzero(tb.event_mask(df, pre=7, brk=20, vol="OR", trend=True, start=start, end=end))
    samples = []
    drop = {"no_next": 0, "zt_open": 0, "no_cache": 0, "no_row": 0, "seam": 0}
    for t in ev:
        if t + 1 >= len(v["close"]):
            drop["no_next"] += 1
            continue
        # 买入：t+1 开盘；t+1 涨停（买不进）剔除
        if v["chg"][t + 1] >= v["band"][t + 1] - 1e-9:
            drop["zt_open"] += 1
            continue
        bp = v["open"][t + 1]
        if bp <= 0:
            drop["zt_open"] += 1
            continue
        # 窗口：t+1 .. t+40（同 compute_paths 40 天窗）
        lo, hi = t + 1, min(t + 41, len(v["close"]))
        if hi - lo < 3:
            drop["no_next"] += 1
            continue
        wc = v["close"][lo:hi]; wo = v["open"][lo:hi]; wl = v["low"][lo:hi]
        wz = v["zt"][lo:hi]; wchg = v["chg"][lo:hi]
        start_price = v["close"][t]
        ret, mae, exit_day = tb._sell_path(wc, wo, wl, wz, wchg, 0, bp, start_price,
                                           V=1, H=14, S="none", band=v["band"][t + 1],
                                           yang=0.09, yang_at="M")
        r_net = (1 - tb.SELL_COST) / (1 + tb.BUY_COST) * ret - 1
        code6 = str(int(v["code"][t])).zfill(6)
        buy_date = v["date"][t + 1]
        ev_date = v["date"][t]
        # 筹码采样：买点日(t+1) 主 + 事件日(t) 对照
        r_buy, st_buy = cyq_at(code6, buy_date)
        r_ev, st_ev = cyq_at(code6, ev_date)
        if st_buy == "no_cache" or st_ev == "no_cache":
            drop["no_cache"] += 1
            continue
        if st_buy == "no_row":
            drop["no_row"] += 1
            continue
        if st_buy == "seam":
            drop["seam"] += 1
            continue
        samples.append(dict(
            code=code6, t=int(t), ev_date=ev_date, buy_date=buy_date,
            ret=float(r_net), mae=float(mae), exit_day=int(exit_day),
            buy_ind={k: float(r_buy[k]) for k in INDICATORS},
            ev_ind={k: (float(r_ev[k]) if st_ev == "ok" else np.nan) for k in INDICATORS},
        ))
    return samples, drop

def strata_table(samples, ind, cuts):
    """按 IS 切点 cuts 分 5 档，输出每档 n/期望/胜率A/MAE"""
    vals = np.array([s["buy_ind"][ind] for s in samples])
    n = len(samples)
    labels = np.digitize(vals, cuts)  # 0..4 → Q1..Q5
    rows = {}
    for q in range(5):
        idx = labels == q
        if idx.sum() == 0:
            rows["Q%d" % (q + 1)] = dict(n=0, exp=None, winA=None, mae=None)
            continue
        r = np.array([samples[i]["ret"] for i in np.flatnonzero(idx)])
        m = np.array([samples[i]["mae"] for i in np.flatnonzero(idx)])
        rows["Q%d" % (q + 1)] = dict(
            n=int(idx.sum()), exp=float(r.mean()), winA=float((r > 0).mean()),
            mae=float(m.mean()))
    return rows

def spearman_rho(q_means):
    """分位序 1..5 vs 各档期望的 Spearman ρ（Q 档可能 n=0 → 用有效档）"""
    xs, ys = [], []
    for q in range(1, 6):
        e = q_means.get("Q%d" % q, {}).get("exp")
        if e is not None:
            xs.append(q); ys.append(e)
    if len(xs) < 3:
        return None
    rho, p = stats.spearmanr(xs, ys)
    return float(rho)

# ================================================================
df = tb.load_feat()
v = tb.numpy_views(df)
out = {"judgment_registered": True, "IS": IS + ".." + IS_END, "OOS": OOS_S + ".." + OOS_E,
       "indicators": INDICATORS, "samples": {}, "strata": {}, "monotonic": {},
       "selfcheck": {}}

# ---- 盲买基线 + 样本（一次性构建，供分层复用）----
sample_store = {}
for seg, (s, e) in (("IS", (IS, IS_END)), ("OOS", (OOS_S, OOS_E))):
    sam, drop = build_samples(s, e)
    sample_store[seg] = sam
    rets = np.array([x["ret"] for x in sam])
    out["samples"][seg] = {"n": len(sam), "exp": float(rets.mean()),
                           "winA": float((rets > 0).mean()), "mae": float(np.mean([x["mae"] for x in sam])),
                           "drop": drop}
    print("盲买 %s: n=%d exp=%.4f winA=%.4f drop=%s" % (seg, len(sam), rets.mean(),
          (rets > 0).mean(), {k: drop[k] for k in drop if drop[k]}), flush=True)
    out["selfcheck"].setdefault(seg, {})["sample_n"] = len(sam)
    out["selfcheck"][seg]["drop_sum"] = sum(drop.values())

# ---- 4 指标 × IS/OOS 五分位分层 ----

for ind in INDICATORS:
    is_sam = sample_store["IS"]
    oos_sam = sample_store["OOS"]
    # IS 切点（q20..q80）
    is_vals = np.array([x["buy_ind"][ind] for x in is_sam])
    cuts = [float(np.percentile(is_vals, p)) for p in (20, 40, 60, 80)]
    # 防边界重叠：去重
    cuts = sorted(set(cuts))
    if len(cuts) < 4:
        print("警告 %s: IS 切点不足 %s" % (ind, cuts))
        cuts = [float(np.percentile(is_vals, p)) for p in (20, 40, 60, 80)]
    out["strata"][ind] = {"is_cuts": cuts}
    out["selfcheck"][ind] = {"is_cuts": cuts}
    for seg in ("IS", "OOS"):
        sam = sample_store[seg]
        tab = strata_table(sam, ind, cuts)
        out["strata"][ind][seg] = tab
        # 分年（OOS）
        if seg == "OOS":
            yearly = {}
            for y in ("2024", "2025", "2026"):
                ysam = [x for x in sam if x["buy_date"].startswith(y)]
                if ysam:
                    yearly[y] = strata_table(ysam, ind, cuts)
            out["strata"][ind]["OOS_yearly"] = yearly
    # 单调性
    is_t, oos_t = out["strata"][ind]["IS"], out["strata"][ind]["OOS"]
    is_q = {k: is_t[k]["exp"] for k in ("Q1", "Q5") if is_t[k]["exp"] is not None}
    oos_q = {k: oos_t[k]["exp"] for k in ("Q1", "Q5") if oos_t[k]["exp"] is not None}
    is_d = (is_q.get("Q5") or 0) - (is_q.get("Q1") or 0)
    oos_d = (oos_q.get("Q5") or 0) - (oos_q.get("Q1") or 0)
    rho_is = spearman_rho(is_t)
    rho_oos = spearman_rho(oos_t)
    # 分年方向不反转：各年 Q5-Q1 与 OOS 总方向，至少 2/3 年同向且无一年反向>1pp
    yearly_d = {}
    yr_signs = []
    for y, tab in out["strata"][ind].get("OOS_yearly", {}).items():
        d = (tab.get("Q5", {}).get("exp") or 0) - (tab.get("Q1", {}).get("exp") or 0)
        yearly_d[y] = d
        yr_signs.append(np.sign(d))
    main_sign = np.sign(oos_d) if oos_d != 0 else 1
    same_dir = sum(1 for s in yr_signs if s == main_sign)
    no_reverse = all(abs(d) <= 0.01 or np.sign(d) == main_sign for d in yearly_d.values())
    cond_a = (np.sign(is_d) == np.sign(oos_d)) and abs(oos_d) >= 0.02
    cond_b = (rho_is is not None and rho_oos is not None and
              np.sign(rho_is) == np.sign(rho_oos) and abs(rho_oos) >= 0.5)
    cond_c = (same_dir >= 2) and no_reverse
    cond_d = all(oos_t[k]["n"] >= 500 for k in ("Q1", "Q2", "Q3", "Q4", "Q5"))
    out["monotonic"][ind] = dict(
        is_delta=float(is_d), oos_delta=float(oos_d), same_sign_a=bool(np.sign(is_d) == np.sign(oos_d)),
        rho_is=rho_is, rho_oos=rho_oos, same_sign_b=bool(rho_is is not None and rho_oos is not None and
        np.sign(rho_is) == np.sign(rho_oos)),
        yearly_delta=yearly_d, same_dir_years=int(same_dir), no_reverse=bool(no_reverse),
        cond_a=bool(cond_a), cond_b=bool(cond_b), cond_c=bool(cond_c), cond_d=bool(cond_d),
        passed=bool(cond_a and cond_b and cond_c and cond_d))
    print("指标 %s: IS Δ=%.4f OOS Δ=%.4f ρIS=%s ρOOS=%s 条件A=%s B=%s C=%s D=%s → %s" % (
        ind, is_d, oos_d, "%.3f" % rho_is if rho_is is not None else None,
        "%.3f" % rho_oos if rho_oos is not None else None, cond_a, cond_b, cond_c, cond_d,
        "PASS" if (cond_a and cond_b and cond_c and cond_d) else "null"), flush=True)

# ---- 自检：随机 2 指标 × 20 事件手工复算（买点日采样 gate 与分层）----
rng = np.random.default_rng(99)
manual = {}
for ind in ("winner_pct", "peak_dist_pct"):
    cuts = out["strata"][ind]["is_cuts"]
    sam = sample_store["OOS"]
    picks = rng.choice(len(sam), size=min(20, len(sam)), replace=False)
    rows = []
    for i in picks:
        s = sam[i]
        q_man = int(np.digitize(s["buy_ind"][ind], cuts))  # 0..4
        rows.append({"buy_date": s["buy_date"], "code": s["code"], "ind": ind,
                     "val": s["buy_ind"][ind], "quintile_manual": q_man + 1,
                     "exp_in_q": out["strata"][ind]["OOS"]["Q%d" % (q_man + 1)]["exp"]})
    manual[ind] = rows
out["selfcheck"]["manual_20"] = manual

# n 守恒检查
for ind in INDICATORS:
    for seg in ("IS", "OOS"):
        qn = [out["strata"][ind][seg]["Q%d" % k]["n"] for k in range(1, 6)]
        out["selfcheck"][ind]["n_sum_%s" % seg] = int(sum(qn))
        out["selfcheck"][ind]["n_total_%s" % seg] = out["samples"][seg]["n"]
        out["selfcheck"][ind]["n_ok_%s" % seg] = (sum(qn) == out["samples"][seg]["n"])
print("自检完成 %.0fs" % (time.time() - T0), flush=True)

with open(os.path.join(ROOT, "tmp", "c2b", "cyq_strata_result.json"), "w", encoding="utf-8") as fp:
    json.dump(out, fp, ensure_ascii=False, indent=1)
print("已写 tmp/c2b/cyq_strata_result.json，总耗时 %.0fs" % (time.time() - T0))
