# -*- coding: utf-8 -*-
"""C3 自检：2 臂 × 20 样本手工复算 + n 守恒。

手工复算：对每个选中的样本，直接用 kline_feat 的 open/close/volume 独立重算
apct / vr / 臂命中，再对卖出路径重算（独立复制 _sell_path 逻辑，非调用）比对 ret。
n 守恒：samples.n = 事件总数 − 各 drop 计数（四臂 + 基线）。
"""
import os, sys, json
import numpy as np
import pandas as pd

sys.path.insert(0, r"C:\Users\26838\A股模拟盘\tools")
import tactic_backtest as tb
import auction_backtest as ab


def manual_sell(wc, wo, wl, wz, wchg, b, buy_price, H=14, yang=0.09):
    """独立复算 yang9_M 卖出倍率（不调用 tb._sell_path）"""
    wlen = len(wc)
    for j in range(b, wlen):
        if wchg[j] >= yang:
            if wz[j] and j + 1 < wlen:
                return wo[j + 1] / buy_price, j - b + 2
            return wc[j] / buy_price, j - b + 1
        if j - b + 1 >= H:
            return wc[j] / buy_price, j - b + 1
    return wc[-1] / buy_price, wlen - b


def run_selfcheck():
    print("=== C3 自检：2 臂 × 20 样本手工复算 + n 守恒 ===")
    D = ab._load_data()
    df, v = D["df"], D["v"]
    out = {"manual": {}, "n_conserve": {}}
    rng = np.random.default_rng(99)
    for aname in ("daban_A1", "rzq_B1"):
        arm = ab.ARMS[aname]
        sam, drop = ab.build_samples(ab.OOS_S, ab.OOS_E, arm=arm)
        picks = rng.choice(len(sam), size=min(20, len(sam)), replace=False)
        rows = []
        for i in picks:
            s = sam[i]
            t = s["t"]
            if t + 1 >= len(df):
                continue
            r_cur = df.iloc[t]
            r_nxt = df.iloc[t + 1]
            # 独立重算 apct / vr
            apct_m = r_nxt["open"] / r_cur["close"] - 1
            vr_m = r_nxt["volume"] / r_cur["volume"] if r_cur["volume"] > 0 else 0.0
            lo, hi = arm["apct"]
            hit_m = (lo <= apct_m < hi) and vr_m >= arm["vr"]
            # 独立重算卖出
            lo2, hi2 = t + 1, min(t + 41, len(df))
            wc = df["close"].values[lo2:hi2]; wo = df["open"].values[lo2:hi2]
            wl = df["low"].values[lo2:hi2]
            wz = df["is_zt"].values[lo2:hi2].astype(bool)
            wchg = df["chg"].values[lo2:hi2]
            bp = r_nxt["open"]
            ret_m, exit_m = manual_sell(wc, wo, wl, wz, wchg, 0, bp)
            r_m = (1 - tb.SELL_COST) / (1 + tb.BUY_COST) * ret_m - 1
            rows.append(dict(code=s["code"], buy_date=s["buy_date"], apct_impl=s["apct"],
                             apct_manual=float(apct_m), vr_impl=s["vr"], vr_manual=float(vr_m),
                             hit_impl=True, hit_manual=bool(hit_m),
                             ret_impl=s["ret"], ret_manual=float(r_m),
                             ok=bool(abs(s["ret"] - r_m) < 1e-9 and hit_m)))
        out["manual"][aname] = rows
        n_ok = sum(1 for r in rows if r["ok"])
        print("  %s 手工复算 %d/%d 一致" % (aname, n_ok, len(rows)))
        # n 守恒
        total_ev = int(tb.event_mask(df, pre=7, brk=20, vol="OR", trend=True,
                                     start=ab.OOS_S, end=ab.OOS_E).sum())
        consumed = drop["no_arm"] + drop["zt_open"] + drop["no_cache"] + \
                   drop["no_row"] + drop["seam"] + drop["no_next"] + len(sam)
        out["n_conserve"][aname] = dict(total_events=total_ev, n=len(sam),
                                        drop_sum=sum(drop.values()),
                                        consumed=consumed,
                                        ok=bool(consumed == total_ev))
        print("  %s n 守恒: events=%d consumed=%d → %s" % (
            aname, total_ev, consumed, "OK" if consumed == total_ev else "FAIL"))
    json.dump(out, open(os.path.join(ab.ROOT, "tmp", "c3", "selfcheck.json"),
                        "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("saved tmp/c3/selfcheck.json")
