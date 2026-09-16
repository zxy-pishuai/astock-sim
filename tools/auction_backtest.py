# -*- coding: utf-8 -*-
"""C3｜竞价打板 / 竞价弱转强 战法回测卡（预注册口径）

主口径 = 日线近似（min5 覆盖仅 2025-08-18 起，IS 2019-2023 无分钟数据，见 §0a）。
事件宇宙：event_mask(pre=7, brk=20, vol='OR', trend=True)，IS=2019-01-01..2023-12-31、
OOS=2024-01-01..2026-08-31（沿用 C2b 已证口径）。
交易：事件 t → t+1 开盘买入（apct 开盘缺口 + 量比 proxy 过滤）→ yang9_M 卖出
      （tb._sell_path yang=0.09, yang_at="M", V=1, H=14, S="none"），扣费同 C2b。
剔除（与 C2b 基线同口径）：t+1 收盘涨停 / 开盘即封板（买不进）+ cyq no_cache/seam。
量比 proxy（日线近似声明）：vr = vol[t+1] / vol[t]（相邻日量比），非真实竞价量比。

四臂（预注册，跑完不改）：
  daban_A1   打板 [3%,9.8%) × 量比≥1.5
  daban_A2   打板 [2%,7%)  × 量比≥1.2
  rzq_B1     弱转强 [−3%,+1%] × 量比≥1.2
  rzq_B2     弱转强 [−5%,0%]  × 量比≥1.5

判过：某臂 IS 与 OOS 双段期望 ≥ 基线−0.1pp 且至少一段期望+0.5pp 或胜率+2pp（n≥300），
      OOS 分年（2024/2025/2026）方向不反转。否则 null（两卡维持"公示参考"徽章）。

用法（统一 py -3.13；禁出网；只读）：
  py -3.13 tools/auction_backtest.py --baseline   # 复核 C2b 盲买基线 4432/3093
  py -3.13 tools/auction_backtest.py --arms       # 四臂 × IS/OOS + 分年 → tmp/c3/auction_result.json
  py -3.13 tools/auction_backtest.py --min5       # min5 可得窗口触发一致率抽验
  py -3.13 tools/auction_backtest.py --selfcheck  # 2 臂 × 20 样本手工复算 + n 守恒
"""
import os, sys, json, time, argparse, sqlite3
import numpy as np
import pandas as pd

ROOT = r"C:\Users\26838\A股模拟盘"
sys.path.insert(0, os.path.join(ROOT, "tools"))
import tactic_backtest as tb

CACHE_DIR = os.path.join(ROOT, "data", "cyq_cache")
MIN5_DB = "file:data/min5.db?mode=ro&immutable=1"
IS, IS_END = "2019-01-01", "2023-12-31"
OOS_S, OOS_E = "2024-01-01", "2026-08-31"

ARMS = {
    "daban_A1": dict(kind="daban", apct=(0.03, 0.098), vr=1.5),
    "daban_A2": dict(kind="daban", apct=(0.02, 0.07), vr=1.2),
    "rzq_B1": dict(kind="rzq", apct=(-0.03, 0.01), vr=1.2),
    "rzq_B2": dict(kind="rzq", apct=(-0.05, 0.0), vr=1.5),
}

# C2b 基线（引用值，--baseline 复核）
BASE = {"IS": {"n": 4432, "exp": -0.01873098758826158, "winA": 0.42035198555956677},
        "OOS": {"n": 3093, "exp": 0.025435975959035736, "winA": 0.4361461364371161}}

_cyq_cache = {}
_df_cache = {}


def _load_data():
    if "v" not in _df_cache:
        df = tb.load_feat()
        _df_cache["df"] = df
        _df_cache["v"] = tb.numpy_views(df)
    return _df_cache


def cyq_ok(code6, date):
    if code6 not in _cyq_cache:
        p = os.path.join(CACHE_DIR, code6 + ".parquet")
        _cyq_cache[code6] = pd.read_parquet(p) if os.path.exists(p) else None
    cdf = _cyq_cache[code6]
    if cdf is None:
        return False, "no_cache"
    sub = cdf[cdf["date"] == date]
    if len(sub) == 0:
        return False, "no_row"
    if sub.iloc[0]["seam_flag"] == 1:
        return False, "seam"
    return True, "ok"


def build_samples(start, end, arm=None):
    """arm=None → 盲买基线（无 apct/量比过滤）；arm=dict → 臂过滤。
    事件 t → t+1 开盘买入 → yang9_M 卖出。剔除与 C2b 同口径。"""
    D = _load_data()
    df, v = D["df"], D["v"]
    ev = np.flatnonzero(tb.event_mask(df, pre=7, brk=20, vol="OR", trend=True,
                                      start=start, end=end))
    samples = []
    drop = {"no_next": 0, "zt_open": 0, "no_cache": 0, "no_row": 0, "seam": 0,
            "no_arm": 0}
    for t in ev:
        if t + 1 >= len(v["close"]):
            drop["no_next"] += 1
            continue
        if arm is not None and arm["kind"] == "rzq" and not v["zt"][t]:
            drop["no_arm"] += 1
            continue
        prev_close = v["close"][t]
        op = v["open"][t + 1]
        if prev_close <= 0 or op <= 0:
            drop["no_arm"] += 1
            continue
        apct = op / prev_close - 1
        vr = v["vol"][t + 1] / v["vol"][t] if v["vol"][t] > 0 else 0.0
        if arm is not None:
            lo, hi = arm["apct"]
            if not (lo <= apct < hi):
                drop["no_arm"] += 1
                continue
            if vr < arm["vr"]:
                drop["no_arm"] += 1
                continue
        # t+1 涨停(买不进)剔除——与基线一致（chg≥band 收盘涨停）
        if v["chg"][t + 1] >= v["band"][t + 1] - 1e-9:
            drop["zt_open"] += 1
            continue
        # 开盘即封板剔除（日线近似：开盘价触及涨停价；四臂 apct 上限恒 < band 故理论不触发，
        # 盲买基线不设此条以精确复刻 C2b）
        if arm is not None and apct >= v["band"][t + 1] - 1e-9:
            drop["zt_open"] += 1
            continue
        bp = op
        lo2, hi2 = t + 1, min(t + 41, len(v["close"]))
        if hi2 - lo2 < 3:
            drop["no_next"] += 1
            continue
        wc = v["close"][lo2:hi2]; wo = v["open"][lo2:hi2]; wl = v["low"][lo2:hi2]
        wz = v["zt"][lo2:hi2]; wchg = v["chg"][lo2:hi2]
        ret, mae, exit_day = tb._sell_path(wc, wo, wl, wz, wchg, 0, bp, prev_close,
                                           V=1, H=14, S="none", band=v["band"][t + 1],
                                           yang=0.09, yang_at="M")
        r_net = (1 - tb.SELL_COST) / (1 + tb.BUY_COST) * ret - 1
        code6 = str(int(v["code"][t])).zfill(6)
        ok, st = cyq_ok(code6, v["date"][t + 1])
        if st == "no_cache":
            drop["no_cache"] += 1
            continue
        if st == "no_row":
            drop["no_row"] += 1
            continue
        if st == "seam":
            drop["seam"] += 1
            continue
        samples.append(dict(code=code6, t=int(t), ev_date=v["date"][t],
                            buy_date=v["date"][t + 1],
                            ret=float(r_net), mae=float(mae), exit_day=int(exit_day),
                            apct=float(apct), vr=float(vr)))
    return samples, drop


def summarize(samples):
    if not samples:
        return dict(n=0, exp=None, winA=None, mae=None, yearly={})
    r = np.array([s["ret"] for s in samples])
    m = np.array([s["mae"] for s in samples])
    yearly = {}
    for y in ("2024", "2025", "2026"):
        ys = [s for s in samples if s["buy_date"].startswith(y)]
        if ys:
            yr = np.array([s["ret"] for s in ys])
            yearly[y] = dict(n=int(len(yr)), exp=float(yr.mean()),
                             winA=float((yr > 0).mean()))
    return dict(n=len(samples), exp=float(r.mean()), winA=float((r > 0).mean()),
                mae=float(m.mean()), yearly=yearly)


def run_baseline():
    print("=== C3 基线复核（盲买 E1，与 C2b 同口径）===")
    out = {}
    for seg, (s, e) in (("IS", (IS, IS_END)), ("OOS", (OOS_S, OOS_E))):
        sam, drop = build_samples(s, e)
        st = summarize(sam)
        out[seg] = {"n": st["n"], "exp": st["exp"], "winA": st["winA"], "drop": drop}
        ref = BASE[seg]
        print("  %s: n=%d(引%d) exp=%.4f(引%.4f) winA=%.4f(引%.4f) drop=%s" % (
            seg, st["n"], ref["n"], st["exp"], ref["exp"], st["winA"], ref["winA"],
            {k: drop[k] for k in drop if drop[k]}))
    json.dump(out, open(os.path.join(ROOT, "tmp", "c3", "baseline_check.json"),
                        "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return out


def run_arms():
    print("=== C3 四臂 × IS/OOS + 分年 ===")
    out = {"judgment_registered": True, "base": BASE,
           "arms": {}, "judgment": {}}
    for aname, arm in ARMS.items():
        out["arms"][aname] = {"config": arm}
        for seg, (s, e) in (("IS", (IS, IS_END)), ("OOS", (OOS_S, OOS_E))):
            sam, drop = build_samples(s, e, arm=arm)
            st = summarize(sam)
            out["arms"][aname][seg] = {"n": st["n"], "exp": st["exp"],
                                       "winA": st["winA"], "mae": st["mae"],
                                       "yearly": st["yearly"], "drop": drop}
            print("  %s %s: n=%d exp=%.4f winA=%.4f mae=%.4f yearly=%s" % (
                aname, seg, st["n"], st["exp"], st["winA"], st["mae"],
                {y: "%.4f/%d" % (v["exp"], v["n"]) for y, v in st["yearly"].items()}))
    # 判定
    for aname, arm in out["arms"].items():
        is_e = arm["IS"]["exp"]; oos_e = arm["OOS"]["exp"]
        is_w = arm["IS"]["winA"]; oos_w = arm["OOS"]["winA"]
        is_n = arm["IS"]["n"]; oos_n = arm["OOS"]["n"]
        c1 = (is_e >= BASE["IS"]["exp"] - 0.001 and oos_e >= BASE["OOS"]["exp"] - 0.001)
        boost = False
        if is_n >= 300 and (is_e >= BASE["IS"]["exp"] + 0.005 or is_w >= BASE["IS"]["winA"] + 0.02):
            boost = True
        if oos_n >= 300 and (oos_e >= BASE["OOS"]["exp"] + 0.005 or oos_w >= BASE["OOS"]["winA"] + 0.02):
            boost = True
        # 分年方向不反转：2024/2025/2026 与 OOS 总方向一致（exp>基线或接近）
        yr = arm["OOS"]["yearly"]
        main_sign = 1 if oos_e >= 0 else -1
        no_rev = all(abs(v["exp"] - BASE["OOS"]["exp"]) <= 0.01 or
                     (v["exp"] >= BASE["OOS"]["exp"] - 0.001) for v in yr.values())
        out["judgment"][aname] = dict(cond_exp=bool(c1), cond_boost=bool(boost),
                                      cond_year=bool(no_rev),
                                      passed=bool(c1 and boost and no_rev))
        print("  → %s: cond_exp=%s cond_boost=%s cond_year=%s → %s" % (
            aname, c1, boost, no_rev, "PASS" if (c1 and boost and no_rev) else "null"))
    json.dump(out, open(os.path.join(ROOT, "tmp", "c3", "auction_result.json"),
                        "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("saved tmp/c3/auction_result.json")
    return out


if __name__ == "__main__":
    a = argparse.ArgumentParser()
    a.add_argument("--baseline", action="store_true")
    a.add_argument("--arms", action="store_true")
    a.add_argument("--min5", action="store_true")
    a.add_argument("--selfcheck", action="store_true")
    args = a.parse_args()
    if args.baseline:
        run_baseline()
    if args.arms:
        run_arms()
    if args.min5:
        from min5_check import run_min5
        run_min5()
    if args.selfcheck:
        from selfcheck import run_selfcheck
        run_selfcheck()
