# -*- coding: utf-8 -*-
"""W1｜「首板→回调→吃第二涨停」战法历史回测工具（只读快照库，零出网）

用法（统一 py -3.13 运行；数据缓存于 tmp/w1/）：
  py -3.13 tools/tactic_backtest.py --events     # 事件漏斗（§1）
  py -3.13 tools/tactic_backtest.py --grid       # IS 网格（§2）+ 输出 data/bt_tactic_shouban.json
  py -3.13 tools/tactic_backtest.py --oos        # OOS 终验（§3，只跑一次）
  py -3.13 tools/tactic_backtest.py --heatmap    # 二维热力表（§2.1）
  py -3.13 tools/tactic_backtest.py --rule       # recommended_rule 写进交付 JSON

判据预注册见 tmp/w1/pre_registered_judgment.md（跑数前落盘，跑完不改）。
红线：只读快照；生产库/现网/出网/git 写/进程 全不碰。
"""
import os, sys, json, time, argparse
import numpy as np
import pandas as pd

ROOT = r"C:\Users\26838\A股模拟盘"
OUT = os.path.join(ROOT, "tmp", "w1")
FEAT = os.path.join(OUT, "kline_feat.pkl")
REPORT_JSON = os.path.join(ROOT, "data", "bt_tactic_shouban.json")

# ---- 成本（§0.6，固定解耦） ----
BUY_COST = 0.00025 + 0.002            # 佣金0.025% + 滑点0.2%
SELL_COST = 0.00025 + 0.0005 + 0.002  # 佣金 + 印花税0.05% + 滑点

# ---- 网格定义（§0，预注册） ----
PRE_NO_ZT = [5, 7, 10]
BRK = [20, 60, 120]
VOL_MODES = ["OR", "A", "B"]
WIND_ZT = [30, 50, 80]
PULLBACK = [5, 8, 10, 12, 14]
E1_K = [3, 5, 7, 9, 11, 13]
E2_SUPPORT = ["ma5", "ma10", "fb_open", "fb_close_97"]
E2_STAB = ["yang", "vol_shrink"]
E3_YANG = [4, 5, 7]
E3_AT = ["close", "next_open"]
K_WIN = [3, 5, 8, 10, 14]
SELL_V = [1, 2, 3]
MAX_HOLD = [10, 14, 20]
STOP = ["none", "break_start", "p5", "p10"]
MIN_SAMPLE = 100

# 主参数（敏感性扫描的默认锚点）
MAIN = dict(pre=7, brk=20, vol="OR", wind=None, trend=True, pullback=10,
            V=1, H=14, S="none", K=8)

T0 = time.time()


# ---------------------------------------------------------------- 数据加载
def load_feat():
    df = pd.read_pickle(FEAT)
    df["date"] = df["date"].astype(str)
    return df


def numpy_views(df):
    return dict(
        code=df["code"].values, date=df["date"].values,
        open=df["open"].values.astype(np.float64),
        high=df["high"].values.astype(np.float64),
        low=df["low"].values.astype(np.float64),
        close=df["close"].values.astype(np.float64),
        vol=df["volume"].values.astype(np.float64),
        zt=df["is_zt"].values.astype(bool),
        chg=df["chg"].values.astype(np.float64),
        band=df["band"].values.astype(np.float64),
    )


def event_mask(df, pre=7, brk=20, vol="OR", trend=True, wind=None, wind_val=None, sent_p=None,
               start="2019-01-01", end=None):
    m = (df["date"] >= start) & df["first_%d" % pre]
    if end is not None:
        m &= df["date"] <= end
    if trend:
        m &= df["trend_ok"]
    if brk is not None:
        m &= df["brk_%d" % brk]
    if vol == "OR":
        m &= df["vol_burst_OR"]
    elif vol == "A":
        m &= df["vol_burst_A"]
    elif vol == "B":
        m &= df["vol_burst_B"]
    if wind == "zt":
        m &= df["zt_count_self"].fillna(0) >= wind_val
    elif wind == "sent":
        m &= df["sentiment_score"] >= sent_p
    return m.values


# ---------------------------------------------------------------- 每事件路径计算
def compute_paths(v, ev_idx, buy_family=("E1", "E2", "E3"), buy_kind_include=None,
                  K_list=None, V=1, H=14, S="none", pullback=10, verbose=False,
                  yang=None, yang_at="close"):
    N = len(v["close"])
    res = []
    fam = set(buy_family)
    for i, t in enumerate(ev_idx):
        if t <= 0 or t >= N - 2:
            continue
        fb_close = v["close"][t]
        fb_open = v["open"][t]
        start_price = v["close"][t - 1]
        band = v["band"][t]
        lo = t + 1
        hi = min(t + 41, N)
        wlen = hi - lo
        if wlen < 3:
            continue
        wc = v["close"][lo:hi]; wo = v["open"][lo:hi]; wl = v["low"][lo:hi]
        wv = v["vol"][lo:hi]; wz = v["zt"][lo:hi]; wchg = v["chg"][lo:hi]
        wdate = v["date"][lo:hi]
        fb_vol = v["vol"][t]

        buys = []
        if "E1" in fam:
            for k in E1_K:
                b = k - 1
                if b >= wlen:
                    continue
                if wchg[b] >= band - 1e-9:
                    continue
                buys.append(("E1_k%d" % k, b, wc[b]))
        if "E2" in fam or "E3" in fam:
            ma5_v = v["close"][max(0, t - 4):t + 1].mean()
            ma10_v = v["close"][max(0, t - 9):t + 1].mean()
        if "E2" in fam:
            for sup, sup_v in (("ma5", ma5_v), ("ma10", ma10_v),
                               ("fb_open", fb_open), ("fb_close_97", fb_close * 0.97)):
                for stab in E2_STAB:
                    for b in range(min(pullback, wlen)):
                        if abs(wl[b] - sup_v) / sup_v <= 0.02 and wc[b] >= start_price:
                            if stab == "yang" and wc[b] <= wo[b]:
                                continue
                            if stab == "vol_shrink" and not (wv[b] < 0.5 * fb_vol):
                                continue
                            if wchg[b] >= band - 1e-9:
                                continue
                            buys.append(("E2_%s_%s" % (sup, stab), b, wc[b]))
                            break
        if "E3" in fam:
            for y in E3_YANG:
                for at in E3_AT:
                    for b in range(min(pullback, wlen)):
                        prev_vol = wv[b - 1] if b > 0 else fb_vol
                        if wchg[b] >= y / 100.0 and wc[b] > fb_close and wv[b] >= 1.2 * prev_vol:
                            if wchg[b] >= band - 1e-9:
                                continue
                            if at == "close":
                                bp = wc[b]
                            else:
                                bp = wo[b + 1] if b + 1 < wlen else wc[b]
                            buys.append(("E3_%dpct_%s" % (y, at), b, bp))
                            break

        if buy_kind_include is not None:
            buys = [x for x in buys if x[0] in buy_kind_include]
        for (bk, b, buy_price) in buys:
            if buy_price <= 0:
                continue
            win_days = {}
            for K in K_list:
                k_win = 0
                for j in range(b, min(b + K, wlen)):
                    if wz[j]:
                        k_win = j - b + 1
                        break
                win_days[K] = k_win
            ret, mae, exit_day = _sell_path(wc, wo, wl, wz, wchg, b, buy_price, start_price,
                                            V, H, S, band, yang=yang, yang_at=yang_at)
            r = (1 - SELL_COST) / (1 + BUY_COST) * ret - 1   # ret 为卖出/买入倍率 → 扣费净收益
            res.append(dict(
                code=v["code"][t], fb_date=v["date"][t], t=t, buy_kind=bk,
                buy_date=wdate[b], buy_price=buy_price,
                ret=float(r), mae=float(mae), exit_day=int(exit_day),
                win=win_days, S=S, V=V, H=H,
            ))
        if verbose and (i + 1) % 2000 == 0:
            print("  %d/%d 事件 %.0fs" % (i + 1, len(ev_idx), time.time() - T0), flush=True)
    return res


def _sell_path(wc, wo, wl, wz, wchg, b, buy_price, start_price, V, H, S, band,
               yang=None, yang_at="close"):
    """卖出路径。yang=None → 原"二板目标"逻辑（V1/V2/V3）；
    yang=数值 → 用户澄清的"反弹兑现"模式：持有期间首个 chg>=yang（大阳或涨停）即卖，
    yang_at=close 当日收盘 / next 次日开盘；无触发则 H 日强卖。止损优先于目标。"""
    if S == "break_start":
        stop_p = start_price
    elif S == "p5":
        stop_p = buy_price * 0.95
    elif S == "p10":
        stop_p = buy_price * 0.90
    else:
        stop_p = None
    wlen = len(wc)
    worst = 0.0
    for j in range(b, wlen):
        dd = wl[j] / buy_price - 1
        if dd < worst:
            worst = dd
        if stop_p is not None and wl[j] <= stop_p:
            return stop_p / buy_price, worst, j - b + 1
        if yang is not None:
            if wchg[j] >= yang:
                if yang_at == "M":
                    # §0c 人类混合口径：大阳日未封涨停→当日收盘卖；封住涨停→次日开盘卖（格局溢价）
                    if wz[j] and j + 1 < wlen:
                        return wo[j + 1] / buy_price, worst, j - b + 2
                    return wc[j] / buy_price, worst, j - b + 1
                if yang_at == "next":
                    if j + 1 < wlen:
                        return wo[j + 1] / buy_price, worst, j - b + 2
                    return wc[j] / buy_price, worst, j - b + 1
                return wc[j] / buy_price, worst, j - b + 1
        else:
            if wz[j]:
                if V == 1:
                    return wc[j] / buy_price, worst, j - b + 1
                elif V == 2:
                    if j + 1 < wlen:
                        return wo[j + 1] / buy_price, worst, j - b + 2
                    return wc[j] / buy_price, worst, j - b + 1
        if j - b + 1 >= H:
            return wc[j] / buy_price, worst, j - b + 1
    return wc[-1] / buy_price, worst, wlen - b


# ---------------------------------------------------------------- 汇总指标
def summarize(rows, k_key=8, min_sample=MIN_SAMPLE):
    if len(rows) < 1:
        return None
    ret = np.array([r["ret"] for r in rows], dtype=np.float64)
    winb = np.array([1 if r["win"].get(k_key, 0) > 0 else 0 for r in rows])
    mae = np.array([r["mae"] for r in rows])
    prof = ret[ret > 0]; loss = ret[ret < 0]
    yr = np.array([r["fb_date"][:4] for r in rows])
    by_year = {}
    for y in sorted(set(yr.tolist())):
        mm = yr == y
        if mm.sum() >= 20:
            by_year[str(y)] = round(float(winb[mm].mean()), 4)
    return dict(
        n=len(rows), sample_ok=len(rows) >= min_sample,
        winA=round(float((ret > 0).mean()), 4),
        winB=round(float(winb.mean()), 4),
        avg_profit=round(float(prof.mean()), 4) if len(prof) else 0.0,
        avg_loss=round(float(loss.mean()), 4) if len(loss) else 0.0,
        pl_ratio=round(float(prof.mean() / abs(loss.mean())), 4) if len(prof) and len(loss) and loss.mean() != 0 else None,
        exp=round(float(ret.mean()), 4),
        p50=round(float(np.percentile(ret, 50)), 4),
        p90=round(float(np.percentile(ret, 90)), 4),
        mae_mean=round(float(mae.mean()), 4),
        mae_p90=round(float(np.percentile(mae, 90)), 4),
        by_year=by_year,
    )


# ---------------------------------------------------------------- 事件漏斗
def cmd_events():
    df = load_feat()
    sent_p50 = float(df.loc[df["date"] >= "2019-01-01", "sentiment_score"].quantile(0.50))
    sent_p70 = float(df.loc[df["date"] >= "2019-01-01", "sentiment_score"].quantile(0.70))
    f = {"sent_p50": sent_p50, "sent_p70": sent_p70}
    base = df[(df["date"] >= "2019-01-01") & df["first_7"]]
    f["first_board"] = int(len(base))
    f["trend"] = int(base["trend_ok"].sum())
    b1 = base[base["trend_ok"]]
    f["brk20"] = int(b1["brk_20"].sum()); f["brk60"] = int(b1["brk_60"].sum()); f["brk120"] = int(b1["brk_120"].sum())
    b2 = b1[b1["brk_20"]]
    f["vol_OR"] = int(b2["vol_burst_OR"].sum()); f["vol_A"] = int(b2["vol_burst_A"].sum()); f["vol_B"] = int(b2["vol_burst_B"].sum())
    b3 = b2[b2["vol_burst_OR"]]
    for th in WIND_ZT:
        f["wind_zt_%d" % th] = int((b3["zt_count_self"].fillna(0) >= th).sum())
    f["wind_sent_p50"] = int((b3["sentiment_score"] >= sent_p50).sum())
    f["wind_sent_p70"] = int((b3["sentiment_score"] >= sent_p70).sum())
    for pre in PRE_NO_ZT:
        f["first_pre%d" % pre] = int(((df["date"] >= "2019-01-01") & df["first_%d" % pre]).sum())
    # 主池内 E2/E3 进场数 + K 内二板兑现（事件层，用 compute_paths 轻量跑）
    v = numpy_views(df)
    ev = np.flatnonzero(event_mask(df, pre=7, brk=20, vol="OR", trend=True))
    rows = compute_paths(v, ev, buy_family=("E2", "E3"), K_list=[8], V=1, H=14, S="none",
                         pullback=10, verbose=False)
    if rows:
        e2 = [r for r in rows if r["buy_kind"].startswith("E2")]
        e3 = [r for r in rows if r["buy_kind"].startswith("E3")]
        f["entry_e2"] = len(e2)
        f["entry_e3"] = len(e3)
        f["win2nd_e2"] = int(sum(1 for r in e2 if r["win"][8] > 0))
        f["win2nd_e3"] = int(sum(1 for r in e3 if r["win"][8] > 0))
    print(json.dumps(f, ensure_ascii=False, indent=1))
    return f


# ---------------------------------------------------------------- IS 网格
def cmd_grid():
    df = load_feat()
    v = numpy_views(df)
    sent_p50 = float(df.loc[df["date"] >= "2019-01-01", "sentiment_score"].quantile(0.50))
    sent_p70 = float(df.loc[df["date"] >= "2019-01-01", "sentiment_score"].quantile(0.70))
    IS = "2019-01-01"
    IS_END = "2023-12-31"
    out = {"sent_p50": sent_p50, "sent_p70": sent_p70, "grid": {}}

    def run_ev(pre, brk, vol, wind, wind_val, sent_p, label, fam=("E1",), kinc=None,
               V=1, H=14, S="none", K=8, pullback=10):
        m = event_mask(df, pre=pre, brk=brk, vol=vol, trend=True, wind=wind,
                       wind_val=wind_val, sent_p=sent_p, start=IS, end=IS_END)
        ev = np.flatnonzero(m)
        rows = compute_paths(v, ev, buy_family=fam, buy_kind_include=kinc, K_list=[K],
                             V=V, H=H, S=S, pullback=pullback)
        return rows

    # --- 1) 三关敏感性（固定 E1_k7 买点, K=8, V1/H14/none）---
    sens = {}
    base_rows = run_ev(7, 20, "OR", None, None, None, "base", fam=("E1",), kinc=["E1_k7"])
    sens["base"] = summarize(base_rows, 8)
    for pre in PRE_NO_ZT:
        if pre == 7:
            continue
        r = run_ev(pre, 20, "OR", None, None, None, "pre%d" % pre, fam=("E1",), kinc=["E1_k7"])
        sens["pre_%d" % pre] = summarize(r, 8)
    for b in BRK:
        if b == 20:
            continue
        r = run_ev(7, b, "OR", None, None, None, "brk%d" % b, fam=("E1",), kinc=["E1_k7"])
        sens["brk_%d" % b] = summarize(r, 8)
    for vm in VOL_MODES:
        if vm == "OR":
            continue
        r = run_ev(7, 20, vm, None, None, None, "vol%s" % vm, fam=("E1",), kinc=["E1_k7"])
        sens["vol_%s" % vm] = summarize(r, 8)
    for th in WIND_ZT:
        r = run_ev(7, 20, "OR", "zt", th, None, "wind%d" % th, fam=("E1",), kinc=["E1_k7"])
        sens["wind_zt%d" % th] = summarize(r, 8)
    for p, pn in ((sent_p50, "p50"), (sent_p70, "p70")):
        r = run_ev(7, 20, "OR", "sent", None, p, "windsent" + pn, fam=("E1",), kinc=["E1_k7"])
        sens["wind_sent_%s" % pn] = summarize(r, 8)
    out["sens"] = sens
    print("三关敏感性完成 %.0fs" % (time.time() - T0), flush=True)

    # --- 2) 买点对比（主池 IS, K=8, V1/H14/none, pullback=10）---
    m = event_mask(df, pre=7, brk=20, vol="OR", trend=True, start=IS, end=IS_END)
    ev = np.flatnonzero(m)
    rows_all = compute_paths(v, ev, buy_family=("E1", "E2", "E3"), K_list=[8],
                             V=1, H=14, S="none", pullback=10, verbose=True)
    kinds = sorted(set(r["buy_kind"] for r in rows_all))
    buy_cmp = {}
    for kd in kinds:
        rr = [r for r in rows_all if r["buy_kind"] == kd]
        buy_cmp[kd] = summarize(rr, 8)
    out["buy_cmp"] = buy_cmp
    print("买点对比完成 %.0fs" % (time.time() - T0), flush=True)
    # 最优买点判定：winB 显著（>=0.18，即明显高于 base ~15%）且 sample_ok 的候选中 exp 最高
    # （E3 大阳系列 winB 显著领先；在同族内再挑钱口径最好的）
    base_winB = sens["base"]["winB"] if sens.get("base") else 0.15
    thr = max(0.18, base_winB + 0.03)
    cand = [(k, vv) for k, vv in buy_cmp.items() if vv and vv["sample_ok"] and vv["winB"] >= thr]
    if not cand:  # 退化：winB 最高者
        cand = [(k, vv) for k, vv in buy_cmp.items() if vv and vv["sample_ok"]]
    cand.sort(key=lambda x: (-x[1]["exp"], -x[1]["winB"]))
    best_kind = cand[0][0] if cand else None
    out["best_buy_kind"] = best_kind
    out["best_buy_note"] = "winB>=%.2f 且 exp 最高" % thr if best_kind else "无可信组合"
    print("最优买点(winB>=%.2f,exp最高): %s" % (thr, best_kind), flush=True)

    # --- 2.5) 最优买点 × 风口叠加（钱口径提升关键）---
    wind_cmp = {}
    if best_kind:
        for th in (50, 80):
            for wname, wval, sp in (("zt", th, None),):
                mw = event_mask(df, pre=7, brk=20, vol="OR", trend=True, wind="zt",
                                wind_val=wval, start=IS, end=IS_END)
                evw = np.flatnonzero(mw)
                rows_w = compute_paths(v, evw, buy_family=("E1", "E2", "E3"),
                                       buy_kind_include=[best_kind], K_list=[8],
                                       V=1, H=14, S="none", pullback=10)
                wind_cmp["%s%d" % (wname, wval)] = summarize(rows_w, 8)
    out["wind_on_best"] = wind_cmp
    print("风口叠加完成 %.0fs" % (time.time() - T0), flush=True)
    # 风口叠加是否优于无风口？
    best_base = buy_cmp.get(best_kind, {})
    best_wind_k = None
    for wk, wv in wind_cmp.items():
        if wv and wv["sample_ok"] and wv["exp"] > best_base.get("exp", 0) + 0.005:
            if best_wind_k is None or wv["exp"] > wind_cmp[best_wind_k]["exp"]:
                best_wind_k = wk
    out["best_wind_addon"] = best_wind_k
    print("风口叠加最优:", best_wind_k, flush=True)

    # --- 3) 最优买点（+可选风口叠加）扫 K / V / H / S（IS）---
    k_scan, s_scan = {}, {}
    if best_kind:
        if best_wind_k:
            evsel = np.flatnonzero(event_mask(df, pre=7, brk=20, vol="OR", trend=True,
                                              wind="zt", wind_val=int(best_wind_k[2:]),
                                              start=IS, end=IS_END))
        else:
            evsel = ev
        for K in K_WIN:
            rows_k = compute_paths(v, evsel, buy_family=("E1", "E2", "E3"),
                                   buy_kind_include=[best_kind], K_list=[K],
                                   V=1, H=14, S="none", pullback=10)
            k_scan["K%d" % K] = summarize(rows_k, K)
        out["k_scan"] = k_scan
        for V in SELL_V:
            for H in MAX_HOLD:
                for S in STOP:
                    rows = compute_paths(v, evsel, buy_family=("E1", "E2", "E3"),
                                         buy_kind_include=[best_kind], K_list=[8],
                                         V=V, H=H, S=S, pullback=10)
                    key = "V%d_H%d_S%s" % (V, H, S)
                    s_scan[key] = summarize(rows, 8)
        out["vhs_scan"] = s_scan
        print("V/H/S 扫描完成 %.0fs" % (time.time() - T0), flush=True)
        # 最优 VHS = 期望最高且 n>=100
        cand2 = [(k, vv) for k, vv in s_scan.items() if vv and vv["sample_ok"]]
        cand2.sort(key=lambda x: -x[1]["exp"])
        best_vhs = cand2[0][0] if cand2 else "V1_H14_Snone"
        out["best_vhs"] = best_vhs
        out["best_vhs_note"] = "exp 最高且 n>=100" if best_vhs else ""
        print("最优卖点/止损(exp最高):", best_vhs, flush=True)

    with open(REPORT_JSON, "w", encoding="utf-8") as fp:
        json.dump(out, fp, ensure_ascii=False, indent=1)
    with open(os.path.join(OUT, "grid_snapshot.json"), "w", encoding="utf-8") as fp:
        json.dump(out, fp, ensure_ascii=False, indent=1)
    print("已写 %s (+grid_snapshot.json)" % REPORT_JSON, flush=True)
    return out


# ---------------------------------------------------------------- OOS 终验
def cmd_oos(best_kind=None, best_vhs="V1_H14_Snone"):
    df = load_feat()
    v = numpy_views(df)
    best_kind = best_kind or "E1_k7"
    V, H, S = int(best_vhs[1]), int(best_vhs.split("_H")[1].split("_")[0]), best_vhs.split("S")[1]
    m = event_mask(df, pre=7, brk=20, vol="OR", trend=True, start="2024-01-01",
                   end="2026-08-31")
    ev = np.flatnonzero(m)
    rows = compute_paths(v, ev, buy_family=("E1", "E2", "E3"), buy_kind_include=[best_kind],
                         K_list=K_WIN, V=V, H=H, S=S, pullback=10, verbose=True)
    out = {"best_kind": best_kind, "best_vhs": best_vhs, "OOS": {}}
    for K in K_WIN:
        out["OOS"]["K%d" % K] = summarize([r for r in rows if r["buy_kind"] == best_kind], K)
    with open(REPORT_JSON, "w", encoding="utf-8") as fp:
        json.dump(out, fp, ensure_ascii=False, indent=1)
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return out


# ---------------------------------------------------------------- 二维热力
def cmd_heatmap():
    df = load_feat()
    v = numpy_views(df)
    hm = {}
    # 基线：全体首板（不过三关）
    m0 = event_mask(df, pre=7, brk=None, vol=None, trend=False, start="2019-01-01",
                    end="2023-12-31")
    rows0 = compute_paths(v, np.flatnonzero(m0), buy_family=("E1",), K_list=K_WIN,
                          V=1, H=14, S="none", pullback=10)
    hm["base_all_first"] = heatmap_from_rows(rows0)
    # 过三关版
    m1 = event_mask(df, pre=7, brk=20, vol="OR", trend=True, start="2019-01-01",
                    end="2023-12-31")
    rows1 = compute_paths(v, np.flatnonzero(m1), buy_family=("E1",), K_list=K_WIN,
                          V=1, H=14, S="none", pullback=10)
    hm["passed_gates"] = heatmap_from_rows(rows1)
    with open(os.path.join(OUT, "heatmap.json"), "w", encoding="utf-8") as fp:
        json.dump(hm, fp, ensure_ascii=False, indent=1)
    print(json.dumps(hm, ensure_ascii=False, indent=1))
    return hm


def heatmap_from_rows(rows):
    # 行=首板后第 k 日买入(E1_k)，列=其后 K 日内出二板概率
    hm = {}
    for kd in sorted({r["buy_kind"] for r in rows}):
        rr = [r for r in rows if r["buy_kind"] == kd]
        row = {"n": len(rr)}
        for K in K_WIN:
            row["K%d" % K] = round(sum(1 for r in rr if r["win"][K] > 0) / len(rr), 4) if rr else None
        hm[kd] = row
    return hm


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", action="store_true")
    ap.add_argument("--grid", action="store_true")
    ap.add_argument("--oos", action="store_true")
    ap.add_argument("--heatmap", action="store_true")
    ap.add_argument("--rule", action="store_true")
    a = ap.parse_args()
    if a.events:
        cmd_events()
    elif a.grid:
        cmd_grid()
    elif a.oos:
        cmd_oos()
    elif a.heatmap:
        cmd_heatmap()
    elif a.rule:
        print("rule 在报告阶段生成 recommended_rule 并入交付 JSON")
    else:
        print("未指定命令")
