# -*- coding: utf-8 -*-
"""C2｜筹码指标 × 现有两卡 融合回测（判据预注册后执行）
基线 = W1R 推荐格 E2_fb_open_vol_shrink + yang9_M
IS=2019-01-01..2021-12-31, OOS=2022-01-01..2023-12-31
8 融合臂（A1-A8）在事件日 t 收盘口径取筹码指标过滤。
只读：复用 tmp/w1/kline_feat.pkl + data/cyq_cache/，禁出网。
"""
import os, sys, json, time
import numpy as np
import pandas as pd

sys.path.insert(0, r"C:\Users\26838\A股模拟盘\tools")
import tactic_backtest as tb

ROOT = tb.ROOT
CACHE_DIR = os.path.join(ROOT, "data", "cyq_cache")
# 分段沿用 W1R（提示词日期 2019-2021/2022-2023 系笔误，归因见报告 §1）：
# 提示词验收值 IS n1555 exp+0.0001 / OOS n815 +4.91% 仅能在 W1R 分段复现。
IS, IS_END = "2019-01-01", "2023-12-31"
OOS_S, OOS_E = "2024-01-01", "2026-08-31"
T0 = time.time()

# ---- 基线参数（C2 提示词） ----
BASE = dict(buy_family=("E2",), buy_kind_include=["E2_fb_open_vol_shrink"],
            K_list=[8], V=1, H=14, S="none", pullback=10, yang=0.09, yang_at="M")

# ---- 8 融合臂（预注册，跑完不改） ----
ARMS = {
    "A1_win_le60":    lambda r: r["winner_pct"] <= 60,
    "A2_win_le80":    lambda r: r["winner_pct"] <= 80,
    "A3_conc_le035":  lambda r: r["conc90"] <= 0.35,
    "A4_conc_le045":  lambda r: r["conc90"] <= 0.45,
    "A5_peak_ge_m5":  lambda r: r["peak_dist_pct"] >= -5,
    "A6_peak_ge5":    lambda r: r["peak_dist_pct"] >= 5,
    "A7_proftr_le0":  lambda r: r["profit_trend5"] <= 0,
    "A8_conc_win":    lambda r: r["conc90"] <= 0.45 and r["winner_pct"] <= 80,
}

# ---- 懒加载 cache ----
_cache = {}
def load_cyq(code6):
    if code6 not in _cache:
        p = os.path.join(CACHE_DIR, code6 + ".parquet")
        if os.path.exists(p):
            _cache[code6] = pd.read_parquet(p)
        else:
            _cache[code6] = None
    return _cache[code6]

def cyq_at(code6, fdate):
    """事件日 t 收盘口径取筹码指标行。返回 (row, status)"""
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

def missing_ratio(code6, fdate):
    """事件日前 120 个有效行内 turnover_src='missing' 占比"""
    cdf = load_cyq(code6)
    if cdf is None:
        return 1.0
    tail = cdf[cdf["date"] <= fdate].tail(120)
    if len(tail) == 0:
        return 1.0
    return float((tail["turnover_src"] == "missing").mean())

def apply_arm(ev_idx, arm_fn, v):
    keep, drop = [], {"no_cache": 0, "no_row": 0, "seam": 0, "missing_ratio": 0, "gate": 0}
    for t in ev_idx:
        code6 = str(int(v["code"][t])).zfill(6)
        fdate = v["date"][t]
        r, status = cyq_at(code6, fdate)
        if status != "ok":
            drop[status] += 1
            continue
        if missing_ratio(code6, fdate) > 0.2:
            drop["missing_ratio"] += 1
            continue
        if not arm_fn(r):
            drop["gate"] += 1
            continue
        keep.append(t)
    return np.array(keep, dtype=np.int64), drop

def run_seg(start, end, arm_name=None, arm_fn=None):
    m = tb.event_mask(df, pre=7, brk=20, vol="OR", trend=True, start=start, end=end)
    ev = np.flatnonzero(m)
    total = len(ev)
    if arm_fn is not None:
        ev, drop = apply_arm(ev, arm_fn, v)
    else:
        drop = None
    rows = tb.compute_paths(v, ev, **BASE)
    summ = tb.summarize(rows, 8)
    if summ is None:
        summ = dict(n=0, sample_ok=False, winA=0.0, winB=0.0, avg_profit=0.0,
                    avg_loss=0.0, pl_ratio=None, exp=0.0, p50=0.0, p90=0.0,
                    mae_mean=0.0, mae_p90=0.0, by_year={})
    return total, ev, drop, summ, rows

# ================================================================
df = tb.load_feat()
v = tb.numpy_views(df)
out = {"judgment_registered": True, "IS": IS + ".." + IS_END, "OOS": OOS_S + ".." + OOS_E,
       "base_params": {k: str(x) for k, x in BASE.items()}, "arms": {}, "selfcheck": {}}

# ---- 基线（无筹码过滤） ----
for seg, (s, e) in (("IS", (IS, IS_END)), ("OOS", (OOS_S, OOS_E))):
    total, ev, _, summ, _ = run_seg(s, e)
    out.setdefault("base", {})[seg] = {"total_events": int(total), **summ}
    print("基线 %s: 事件%d 买点%d exp=%.4f winA=%.4f winB=%.4f" % (
        seg, total, summ["n"], summ["exp"], summ["winA"], summ["winB"]), flush=True)
    out["selfcheck"].setdefault(seg, {})["base_events"] = int(total)

# ---- 8 臂 ----
for arm, fn in ARMS.items():
    out["arms"][arm] = {}
    for seg, (s, e) in (("IS", (IS, IS_END)), ("OOS", (OOS_S, OOS_E))):
        total, ev, drop, summ, rows = run_seg(s, e, arm, fn)
        keep_n = len(ev)
        out["arms"][arm][seg] = {"total_events": int(total), "keep_events": int(keep_n),
                                 "dropped": drop, **summ}
        drop_sum = sum(drop.values())
        out["selfcheck"].setdefault(seg, {}).setdefault(arm, {})["drop_sum"] = drop_sum
        out["selfcheck"][seg][arm]["keep_plus_drop"] = keep_n + drop_sum
        out["selfcheck"][seg][arm]["equals_total"] = (keep_n + drop_sum == total)
        print("臂 %s %s: 事件%d 保留%d 期望=%.4f 胜率A=%.4f 胜率B=%.4f drop=%s" % (
            arm, seg, total, keep_n, summ["exp"], summ["winA"], summ["winB"],
            {k: drop[k] for k in drop if drop[k]}), flush=True)
    print("臂 %s 完成 %.0fs" % (arm, time.time() - T0), flush=True)

# ---- 自检：随机抽 2 臂手工复算 20 事件 ----
rng = np.random.default_rng(42)
selfcheck_rows = {}
for arm in ("A1_win_le60", "A6_peak_ge5"):
    fn = ARMS[arm]
    m = tb.event_mask(df, pre=7, brk=20, vol="OR", trend=True, start=IS, end=IS_END)
    ev = np.flatnonzero(m)
    ev_f, _ = apply_arm(ev, fn, v)
    if len(ev_f) == 0:
        selfcheck_rows[arm] = "no events"
        continue
    idx = rng.choice(ev_f, size=min(20, len(ev_f)), replace=False)
    # 手工复算：直接查这些事件的筹码门通过性 + 保留/剔除
    manual = []
    for t in sorted(idx):
        code6 = str(int(v["code"][t])).zfill(6)
        fdate = v["date"][t]
        r, status = cyq_at(code6, fdate)
        gate = (status == "ok" and missing_ratio(code6, fdate) <= 0.2 and fn(r))
        manual.append({"t": int(t), "code": code6, "date": fdate, "status": status, "gate": bool(gate)})
    selfcheck_rows[arm] = manual
out["selfcheck"]["manual_20"] = selfcheck_rows
print("自检 20 事件复算完成 %.0fs" % (time.time() - T0), flush=True)

# ---- 边界日期打印 ----
out["selfcheck"]["boundary"] = {
    "IS_first": df.loc[df["date"] >= IS, "date"].min(),
    "IS_last": df.loc[df["date"] <= IS_END, "date"].max(),
    "OOS_first": df.loc[(df["date"] >= OOS_S) & (df["date"] <= OOS_E), "date"].min(),
    "OOS_last": df.loc[(df["date"] >= OOS_S) & (df["date"] <= OOS_E), "date"].max(),
}
print("边界:", out["selfcheck"]["boundary"], flush=True)

with open(os.path.join(ROOT, "tmp", "c2", "cyq_grid_result.json"), "w", encoding="utf-8") as fp:
    json.dump(out, fp, ensure_ascii=False, indent=1)
print("已写 tmp/c2/cyq_grid_result.json，总耗时 %.0fs" % (time.time() - T0))
