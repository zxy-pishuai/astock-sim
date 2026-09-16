# -*- coding: utf-8 -*-
"""W2 ml 影子组合全史回测（研究·轻算力，纯向量化，无 multiprocessing）

口径（与报告 §0 预注册一致，勿改判据）：
- 只读快照 data/snapshots/2026-08-31/market.db（钉死）；ml_pred 为成品信号表
- 池 = 每日 ml_pred 实际覆盖票（截面排序，天然 PIT 近似）
- (N=10,20,30) x (H=5,10,20) 9 组：signal 日按 score 降序取 top-N（不足用实际数）
- 非重叠 H 日持有期：次开进次出（o2o 主口径，期级精确收益）+ c2c 对照
- 成本：滑点 0.001 + 佣金 0.00025 + 印花税卖出 0.0005 + 过户费 0.00001(沪市)
  换手成本按实际换入换出只数计（等权 1/N）
- 基准：当日 ml_pred 全池等权日度(close-to-close)；global_kline 无 A 股指数已注明
- 日度净值曲线（close 基准近似，供 MDD）；年化/超额以期级 o2o 精确收益为主口径
- 输出：data/bt_ml_shadow.json（9 组全量指标 + 月度摘要）

用法：tools\\ml_sidecar\\.venv\\Scripts\\python.exe tools/ml_shadow_portfolio.py
（需 pandas/numpy；不 import lightgbm）
"""
import json
import os
import sqlite3
import time
from datetime import datetime

import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAP = os.path.join(BASE, "data", "snapshots", "2026-08-31", "market.db")
OUT = os.path.join(BASE, "data", "bt_ml_shadow.json")

# ---- 成本（config 现值：COMMISSION_RATE=0.00025, SLIPPAGE=0.001,
#      STAMP_TAX_RATE=0.0005(卖出), TRANSFER_FEE_RATE=0.00001(沪市双边)）----
SLIPPAGE = 0.001
COMMISSION = 0.00025
STAMP_TAX = 0.0005   # 仅卖出
TRANSFER = 0.00001   # 仅沪市（6 开头）双边
N_LIST = [10, 20, 30]
H_LIST = [5, 10, 20]
WINDOW2 = "2025-06-01"   # 权威窗口起点（滚动训练未见未来）

# 预注册接线判据（P72：跑数前落盘，跑完不修改）
CRITERIA = {
    "接线候选": "2025-06 后窗口内，(N,H) 至少一组同时满足："
                "(a) 扣费年化超额(o2o 主口径, 相对全池等权基准) >= +5pp；"
                "(b) 策略 MDD <= 基准 MDD + 5pp；"
                "(c) 日度横截面 IC(score vs 未来H日收益, Spearman) 均值>0 且 t>2",
    "不达": "影子组合结论'ML 接线证据不足'，台账 1/20 继续养",
    "权威窗口": "2025-06-01 后（全史仅对照，2019 std 异常须剔除）",
    "解读": "IC = 每日横截面 score 与未来 H 日收益(c2c) 的 Spearman 秩相关，窗口内日度序列均值与 t 值",
}


def buy_cost(code):
    return SLIPPAGE + COMMISSION + (TRANSFER if str(code).startswith("6") else 0.0)


def sell_cost(code):
    return SLIPPAGE + COMMISSION + STAMP_TAX + (TRANSFER if str(code).startswith("6") else 0.0)


def load():
    con = sqlite3.connect("file:%s?mode=ro" % SNAP.replace("\\", "/"), uri=True, timeout=60)
    ml = pd.read_sql("SELECT date, code, score FROM ml_pred ORDER BY date, code", con)
    kl = pd.read_sql(
        "SELECT code, date, open, close FROM kline WHERE period='day' "
        "AND date>='2018-12-01' AND date<='2026-08-31'", con)
    con.close()
    return ml, kl


def build_calendar_and_prices(ml, kl):
    """交易日轴 = ml_pred 唯一日期；价格长表供 merge。"""
    cal = sorted(ml["date"].unique())
    mpcodes = set(ml["code"].unique())
    kl = kl[kl["code"].isin(mpcodes)].copy()
    op = kl.pivot_table(index="date", columns="code", values="open", aggfunc="last")
    cl = kl.pivot_table(index="date", columns="code", values="close", aggfunc="last")
    # 长表（date, code, open/close）用于向量化 merge
    op_long = op.stack().rename("open").rename_axis(["date", "code"]).reset_index()
    cl_long = cl.stack().rename("close").rename_axis(["date", "code"]).reset_index()
    return cal, op_long, cl_long, op, cl


def forward_returns(ml, cal, op_long, cl_long, H):
    """为每 (code,date) 算 o2o 与 c2c 的 H 日前瞻收益（向量化 merge）。"""
    cali = {d: i for i, d in enumerate(cal)}
    idx = ml["date"].map(cali)
    ok = (idx + H + 1) < len(cal)
    m = ml[ok].copy()
    i = idx[ok]
    m["sig_date"] = m["date"]
    m["en_d"] = [cal[j] for j in (i + 1).tolist()]
    m["ex_d"] = [cal[j] for j in (i + H + 1).tolist()]
    m["tH_d"] = [cal[j] for j in (i + H).tolist()]
    m = m.drop(columns=["date"])
    # 信号日 close（c2c 起点）
    cl_sig = cl_long.rename(columns={"date": "date_x", "close": "close_sig"})
    m = m.merge(cl_sig, left_on=["code", "sig_date"], right_on=["code", "date_x"], how="left")
    m = m.drop(columns=["date_x"])
    # entry open
    op_en = op_long.rename(columns={"date": "date_x", "open": "open_en"})
    m = m.merge(op_en, left_on=["code", "en_d"], right_on=["code", "date_x"], how="left")
    m = m.drop(columns=["date_x"])
    # exit open
    op_ex = op_long.rename(columns={"date": "date_x", "open": "open_ex"})
    m = m.merge(op_ex, left_on=["code", "ex_d"], right_on=["code", "date_x"], how="left")
    m = m.drop(columns=["date_x"])
    # t+H close（c2c 终点）
    cl_t = cl_long.rename(columns={"date": "date_x", "close": "close_t"})
    m = m.merge(cl_t, left_on=["code", "tH_d"], right_on=["code", "date_x"], how="left")
    m = m.drop(columns=["date_x"])
    m["fwd_o2o"] = m["open_ex"] / m["open_en"] - 1.0
    m["fwd_c2c"] = m["close_t"] / m["close_sig"] - 1.0
    m = m.drop(columns=["en_d", "ex_d", "tH_d", "open_en", "open_ex", "close_sig", "close_t"])
    m = m.rename(columns={"sig_date": "date"})
    return m


def daily_benchmark(ml, kl, cal):
    """全池等权日度收益（close-to-close，ml_pred 覆盖票均值）。"""
    mpcodes = set(ml["code"].unique())
    k = kl[kl["code"].isin(mpcodes)].copy()
    k = k[k["date"].isin(cal)].sort_values(["code", "date"])
    k["prev_close"] = k.groupby("code")["close"].shift(1)
    k["r"] = k["close"] / k["prev_close"] - 1.0
    daily = k.groupby("date")["r"].mean().reindex(cal)
    return daily.fillna(0.0)


def ic_series(m, cal, H):
    """每日横截面 IC（score vs 未来H日收益 c2c，Spearman）。"""
    g = m.dropna(subset=["fwd_c2c"]).groupby("date")
    rows = [(d, grp["score"].corr(grp["fwd_c2c"], method="spearman"))
            for d, grp in g if len(grp) >= 15]
    return pd.Series(dict(rows), dtype=float)


def strategy_periods(by_date, cal, cali, op, cl, N, H):
    """非重叠 H 日持有期：返回 periods = (entry_i, exit_i, basket, sig_date)。"""
    L = len(cal)
    periods = []
    anomaly = 0
    for i in range(0, L - H - 1, H):
        sig_date = cal[i]
        g = by_date[sig_date]
        g = g.sort_values("score", ascending=False).dropna(subset=["fwd_o2o"])
        if g.empty:
            continue
        basket = g["code"].head(N).tolist()
        en_i = i + 1
        ex_i = i + H + 1
        en_d = cal[en_i]
        ex_d = cal[ex_i]
        okb = []
        for c in basket:
            if c in op.columns and c in cl.columns and en_d in op.index and ex_d in op.index:
                eo = op.at[en_d, c]
                xo = op.at[ex_d, c]
                if eo == eo and xo == xo:
                    okb.append(c)
                else:
                    anomaly += 1
            else:
                anomaly += 1
        if okb:
            periods.append((en_i, ex_i, okb, sig_date))
    return periods, anomaly


def daily_curve(cal, op, cl, periods):
    """日度净值收益序列（close 基准近似，供 MDD）：entry 日 close/open，其余 close/close。"""
    r_series = pd.Series(0.0, index=cal)
    for en_i, ex_i, basket, _sig in periods:
        d0 = cal[en_i]
        vals = [cl.at[d0, c] / op.at[d0, c] - 1.0 for c in basket
                if d0 in cl.index and d0 in op.index
                and cl.at[d0, c] == cl.at[d0, c] and op.at[d0, c] == op.at[d0, c]]
        r_series[d0] = np.mean(vals) if vals else 0.0
        prev = {c: cl.at[d0, c] for c in basket
                if d0 in cl.index and cl.at[d0, c] == cl.at[d0, c]}
        for j in range(en_i + 1, ex_i):
            d = cal[j]
            vals = [cl.at[d, c] / prev[c] - 1.0 for c in basket
                    if c in prev and d in cl.index and prev[c] == prev[c]
                    and cl.at[d, c] == cl.at[d, c]]
            r_series[d] = np.mean(vals) if vals else 0.0
            prev = {c: cl.at[d, c] for c in basket
                    if d in cl.index and cl.at[d, c] == cl.at[d, c]}
    return r_series


def overnight_intraday_decomp(cal, op, cl, periods, by_date, H):
    """每期组合级隔夜/日内分解（o2o 窗口 [en, ex)），log 归因。
    ov_c = Π(open[d+1]/close[d]), id_c = Π(close[d]/open[d])，等权平均后跨期复利，
    overnight_share = log(ov_total)/(log(ov_total)+log(id_total))。"""
    logs = {"full": {"ov": 0.0, "id": 0.0, "n": 0}, "w2": {"ov": 0.0, "id": 0.0, "n": 0}}
    for en_i, ex_i, basket, sig_date in periods:
        bucket = "w2" if sig_date >= WINDOW2 else "full"
        ov_list = []
        id_list = []
        for c in basket:
            ov_c = 1.0
            id_c = 1.0
            for j in range(en_i, ex_i):
                d = cal[j]
                dn = cal[j + 1]
                if c not in op.columns or c not in cl.columns:
                    continue
                if d in cl.index and dn in op.index:
                    o = op.at[dn, c]
                    pc = cl.at[d, c]
                    if o == o and pc == pc and pc != 0:
                        ov_c *= o / pc
                if d in op.index and d in cl.index:
                    o2 = op.at[d, c]
                    cl2 = cl.at[d, c]
                    if o2 == o2 and cl2 == cl2 and o2 != 0:
                        id_c *= cl2 / o2
            ov_list.append(ov_c)
            id_list.append(id_c)
        if ov_list:
            logs[bucket]["ov"] += np.log(np.mean(ov_list))
            logs[bucket]["id"] += np.log(np.mean(id_list))
            logs[bucket]["n"] += 1
    out = {}
    for k, v in logs.items():
        if v["n"] == 0:
            out[k] = {"overnight_cum": 0.0, "intraday_cum": 0.0, "n": 0, "overnight_share": None}
            continue
        ov = v["ov"]
        idc = v["id"]
        share = ov / (ov + idc) if (ov + idc) != 0 else None
        out[k] = {"overnight_cum": round(np.expm1(ov), 5), "intraday_cum": round(np.expm1(idc), 5),
                  "n": v["n"], "overnight_share": round(share, 4) if share is not None else None}
    return out


def c2c_period_returns(periods, by_date, N):
    """c2c 对照：每期 close[t] 进、close[t+H] 出（含换手成本），期净收益列表。"""
    rets = []
    prev_basket = None
    for _en_i, _ex_i, basket, sig_date in periods:
        g = by_date[sig_date].set_index("code")["fwd_c2c"].dropna()
        sub = [g[c] for c in basket if c in g.index]
        gross = float(np.mean(sub)) if sub else 0.0
        if prev_basket is not None:
            exited = [c for c in prev_basket if c not in set(basket)]
            entered = [c for c in basket if c not in set(prev_basket)]
        else:
            exited = []
            entered = basket
        cost = sum((1.0 / N) * sell_cost(c) for c in exited) + \
               sum((1.0 / N) * buy_cost(c) for c in entered)
        rets.append({"sig": sig_date, "gross": round(gross, 6), "cost": round(cost, 6)})
        prev_basket = basket
    return rets


def annualized(series):
    """由日度净值收益序列算年化与 MDD（252 交易日）。"""
    if series.empty or series.abs().sum() == 0:
        return 0.0, 0.0
    v = (1.0 + series).cumprod()
    n = len(series)
    yrs = n / 252.0
    if yrs <= 0 or v.iloc[-1] <= 0:
        return 0.0, 0.0
    ann = v.iloc[-1] ** (1.0 / yrs) - 1.0
    peak = v.cummax()
    mdd = float(((v - peak) / peak).min())
    return ann, mdd


def main():
    t0 = time.time()
    print("载入快照...", flush=True)
    ml, kl = load()
    print("  ml_pred rows=%d  kl rows=%d (%.0fs)" % (len(ml), len(kl), time.time() - t0), flush=True)
    cal, op_long, cl_long, op, cl = build_calendar_and_prices(ml, kl)
    cali = {d: i for i, d in enumerate(cal)}
    print("  交易日=%d  ml票=%d" % (len(cal), ml["code"].nunique()), flush=True)

    bench = daily_benchmark(ml, kl, cal)
    b_full = annualized(bench)
    b_w2 = annualized(bench[bench.index >= WINDOW2])
    print("基准: 全史 ann=%.2f%% mdd=%.2f%% | 25.06+ ann=%.2f%% mdd=%.2f%%" % (
        b_full[0] * 100, b_full[1] * 100, b_w2[0] * 100, b_w2[1] * 100), flush=True)

    fwd = {}
    for H in H_LIST:
        fwd[H] = forward_returns(ml, cal, op_long, cl_long, H)
        print("  fwd H=%d done (%d rows) (%.0fs)" % (H, len(fwd[H]), time.time() - t0), flush=True)

    results = {}
    for H in H_LIST:
        m = fwd[H]
        ics = ic_series(m, cal, H)
        by_date = {d: g for d, g in m.groupby("date")}
        for N in N_LIST:
            key = "N%d_H%d" % (N, H)
            print("组合 %s ..." % key, flush=True)
            periods, anomaly = strategy_periods(by_date, cal, cali, op, cl, N, H)
            r_daily = daily_curve(cal, op, cl, periods)
            decomp = overnight_intraday_decomp(cal, op, cl, periods, by_date, H)
            c2c_rets = c2c_period_returns(periods, by_date, N)

            # 期级 o2o 精确收益（成本后）
            cost_rows = []
            prev_basket = None
            for en_i, ex_i, basket, sig_date in periods:
                g = by_date[sig_date].set_index("code")["fwd_o2o"].dropna()
                sub = [g[c] for c in basket if c in g.index]
                gross = float(np.mean(sub)) if sub else 0.0
                if prev_basket is not None:
                    pb = set(prev_basket)
                    exited = [c for c in prev_basket if c not in set(basket)]
                    entered = [c for c in basket if c not in pb]
                else:
                    exited = []
                    entered = basket
                cost = sum((1.0 / N) * sell_cost(c) for c in exited) + \
                       sum((1.0 / N) * buy_cost(c) for c in entered)
                cost_rows.append({"sig": sig_date, "gross": round(gross, 6),
                                  "cost": round(cost, 6), "n": len(basket)})
                prev_basket = basket

            def ann_period(rets):
                if not rets:
                    return 0.0
                v = 1.0
                for r in rets:
                    v *= (1.0 + r)
                yrs = len(rets) * H / 252.0
                return v ** (1.0 / yrs) - 1.0 if yrs > 0 else 0.0

            def window_metrics(lo, hi):
                sub = r_daily[(r_daily.index >= lo) & (r_daily.index <= hi)]
                ann, mdd = annualized(sub)
                bs = bench[(bench.index >= lo) & (bench.index <= hi)]
                bann, bmdd = annualized(bs)
                prets = [x for x in cost_rows if lo <= x["sig"] <= hi]
                pr_ann = ann_period([x["gross"] - x["cost"] for x in prets])
                ic_sub = ics[(ics.index >= lo) & (ics.index <= hi)].dropna()
                ic_mean = float(ic_sub.mean()) if len(ic_sub) else 0.0
                ic_t = float(ic_sub.mean() / (ic_sub.std() / np.sqrt(len(ic_sub)))) \
                    if len(ic_sub) > 1 and ic_sub.std() > 0 else 0.0
                return {
                    "days": int(len(sub)), "ann": round(ann, 5), "mdd": round(mdd, 5),
                    "bench_ann": round(bann, 5), "bench_mdd": round(bmdd, 5),
                    "excess_ann": round(ann - bann, 5),
                    "period_o2o_ann": round(pr_ann, 5),
                    "period_excess_ann": round(pr_ann - bann, 5),
                    "ic_mean": round(ic_mean, 5), "ic_t": round(ic_t, 3),
                    "n_periods": len(prets), "anomaly": anomaly,
                }

            full = window_metrics(cal[0], cal[-1])
            w2 = window_metrics(WINDOW2, cal[-1])
            # c2c 对照年化（双窗口）
            def c2c_ann(lo, hi):
                prets = [x for x in c2c_rets if lo <= x["sig"] <= hi]
                if not prets:
                    return 0.0
                v = 1.0
                for x in prets:
                    v *= (1.0 + x["gross"] - x["cost"])
                yrs = len(prets) * H / 252.0
                return v ** (1.0 / yrs) - 1.0 if yrs > 0 else 0.0

            c2c_full_ann = c2c_ann(cal[0], cal[-1])
            c2c_w2_ann = c2c_ann(WINDOW2, cal[-1])
            monthly = {}
            for x in cost_rows:
                ym = x["sig"][:7]
                monthly.setdefault(ym, {"gross": 0.0, "cost": 0.0, "n": 0})
                monthly[ym]["gross"] += x["gross"]
                monthly[ym]["cost"] += x["cost"]
                monthly[ym]["n"] += 1
            monthly_out = {k: {"sum_gross": round(v["gross"], 4),
                               "sum_cost": round(v["cost"], 4), "n": v["n"]}
                           for k, v in monthly.items()}
            results[key] = {
                "N": N, "H": H,
                "full": full, "since_2025_06": w2,
                "c2c_full_ann": round(c2c_full_ann, 5), "c2c_since_2025_06_ann": round(c2c_w2_ann, 5),
                "overnight_decomp": decomp,
                "monthly": monthly_out,
                "n_periods_total": len(periods),
            }
            print("  %s full: o2o_ann=%.2f%% excess=%.2fpp mdd=%.2f%% | 25.06+: o2o_ann=%.2f%% "
                  "excess=%.2fpp ic=%.3f(t=%.1f)" % (
                    key, full["period_o2o_ann"] * 100, full["period_excess_ann"] * 100,
                    full["mdd"] * 100, w2["period_o2o_ann"] * 100,
                    w2["period_excess_ann"] * 100, w2["ic_mean"], w2["ic_t"]), flush=True)

    out = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "snapshot": SNAP,
        "criteria": CRITERIA,
        "cost_model": {"slippage": SLIPPAGE, "commission": COMMISSION,
                       "stamp_tax_sell": STAMP_TAX, "transfer_sh": TRANSFER},
        "benchmark": {"desc": "ml_pred 全池等权日度(close-to-close), 无A股指数可用",
                      "full_ann": round(b_full[0], 5), "full_mdd": round(b_full[1], 5),
                      "since_2025_06_ann": round(b_w2[0], 5), "since_2025_06_mdd": round(b_w2[1], 5)},
        "windows": {"full": [cal[0], cal[-1]], "since_2025_06": [WINDOW2, cal[-1]]},
        "ml_pred": {"rows": int(len(ml)), "days": len(cal),
                    "min_codes": int(ml.groupby("date")["code"].count().min()),
                    "max_codes": int(ml.groupby("date")["code"].count().max()),
                    "avg_codes": round(float(ml.groupby("date")["code"].count().mean()), 1)},
        "groups": results,
    }
    with open(OUT, "w", encoding="utf-8", newline="\n") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("\n输出:", OUT, "(%.0fs)" % (time.time() - t0))


if __name__ == "__main__":
    main()
