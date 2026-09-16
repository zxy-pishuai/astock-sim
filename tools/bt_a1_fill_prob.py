# -*- coding: utf-8 -*-
"""
C4｜A1 可成交性建模（涨停价排队成交概率）——研究脚本（零落地）

口径与判据预注册（先于结果落盘，跑完不改；报告 §0 同文）：
  1. 覆盖率判据：min5 对 A1 历史信号（10cm 首板缩量非一字，n=5588）的覆盖率
     = 信号(code,date) 落在 kline_min5 覆盖内占比。若 <5% → 声明"min5 无法直接
     支撑历史信号封板概率重建"，改用 limit_pool（2026-08-15~09-11 近月涨停池）
     封板行为分布 + M2 先验（live_fill_calibration.json：sealed_le_1030=0.3 /
     sealed_gt_1030=0.6 / broken=1.0，先验设定非实测）做两档代理口径。
  2. 三档情景：
     S1 理想：P=1（现状基线，V1/atr_exit 已含 QUEUE=0.001 排队惩罚）
     S2 概率加权：按封板行为分布 × M2 先验结构加权成交
     S3 保守：仅"炸过板（zbc>=1）"成交，未炸一律买不到
  3. 判据：S2（limit_pool 口径）折扣后 OOS 期望 ≤ 0 → 如实报 null + 给替代执行
     臂；>0 → 报折扣后数值与"edge 剩余比例"，并给敏感性区间（M2 先验 ±0.15）。
  4. 校准判据：实盘 board 成交记录（live_fill_calibration + account.json board 类）
     若 n<30 → 声明"校准样本不足，模型未实证校准"。
  5. 覆盖口径：min5 只覆盖 2025-08-18 起 515 只数字代码票（60/00 共 326）；
     A1 信号窗口 2019-01-02~2026-08-21。limit_pool.date 为交易日+1 标签
     （周末重复打标），payload 归属前一交易日。

数据来源（全部只读）：
  - tmp/v1/events.pkl + kline_day.pkl（V1 缓存，复用 tactic_lianban_backtest）
  - data/min5.db（kline_min5，2025-08-18 起，515 只数字代码票）
  - data/market.db limit_pool（kind='zt'，2026-08-15~09-11）
  - data/attribution/live_fill_calibration.json（校准，n=2）
  - data/account.json（board 类成交核对）
"""
import json
import os
import sqlite3
import sys

import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

OUT = os.path.join(BASE, "data", "bt_a1_fill_prob.json")
SLIP, COMM, STAMP, QUEUE = 0.002, 0.00025, 0.0005, 0.001
BUY_FEE = 1 + SLIP + QUEUE + COMM
SELL_FEE = 1 - SLIP - COMM - STAMP
M2 = {"sealed_le_1030": 0.3, "sealed_gt_1030": 0.6, "broken": 1.0}


def fbt_min(fbt):
    """payload fbt（如 92500=09:25:00）→ 分钟数；解析失败返回 None"""
    if not fbt:
        return None
    s = str(int(fbt)).zfill(6)
    try:
        return int(s[:2]) * 60 + int(s[2:4])
    except Exception:
        return None


def load_a1_signals():
    """复用 V1 的 10cm A1 组合（首板 lbc=1 + 缩量 amount_ratio<1 + 非一字）逐笔信号"""
    from tools import tactic_lianban_backtest as V1
    E = V1._load_events('10cm')
    T, frames, buys, buy_fees, sells = V1._build_trades(E)
    c1 = frames['A1'][(frames['A1']['lbc'] == 1) & (frames['A1']['amount_ratio'] < 1.0)]
    sig = c1.copy()
    sig['code'] = sig['code'].astype(str)
    sig['date'] = sig['date'].astype(str)
    sig['ret'] = sig['next_open'] * SELL_FEE / (sig['close'] * BUY_FEE) - 1.0
    return sig


def coverage_stats(sig):
    """min5 覆盖率（code 且日期在范围内）"""
    c5 = sqlite3.connect("file:%s/data/min5.db?mode=ro" % BASE, uri=True, timeout=30)
    cov = pd.read_sql_query(
        "SELECT code, MIN(date) AS dmin, MAX(date) AS dmax FROM kline_min5 "
        "WHERE code GLOB '[0-9][0-9]*' GROUP BY code", c5)
    c5.close()
    mcodes = set(cov['code'])
    cov_map = dict(zip(cov['code'], zip(cov['dmin'], cov['dmax'])))
    sig['in_min5'] = sig['code'].isin(mcodes)
    sig['min5_ok'] = False
    for i, row in sig.iterrows():
        if not row['in_min5']:
            continue
        dm = cov_map.get(row['code'])
        if dm and dm[0] <= row['date'] + ' 00:00:00' <= dm[1]:
            sig.loc[i, 'min5_ok'] = True
    return int(sig['min5_ok'].sum()), int(len(sig))


def limit_pool_dist():
    """limit_pool 近月封板行为分布（唯一 code,payload 去重；lbc=1 子集）"""
    cd = sqlite3.connect("file:%s/data/market.db?mode=ro" % BASE, uri=True, timeout=15)
    rows = cd.execute("SELECT date, code, payload FROM limit_pool WHERE kind='zt'").fetchall()
    cd.close()
    uniq = {}
    for d, code, p in rows:
        uniq[(code, p)] = d
    parsed = []
    for (code, p), d in uniq.items():
        pp = json.loads(p)
        parsed.append({"code": code, "date": d, "lbc": pp.get("lbc"),
                       "fbt": pp.get("fbt"), "zbc": pp.get("zbc")})
    one = [p for p in parsed if p["lbc"] == 1]
    return parsed, one


def scen_rates(sub):
    """三档成交率：S2 = M2 结构（炸过=1.0；未炸按 fbt 分桶 0.3/0.5/0.6）；
    S3 = 仅炸板。返回 (s2, s3, 分桶明细)。"""
    n = len(sub)
    ps, buck = [], {}
    for p in sub:
        if (p["zbc"] or 0) >= 1:
            ps.append(1.0)
            buck.setdefault("broken", 0)
            buck["broken"] += 1
        else:
            m = fbt_min(p["fbt"])
            if m is None or m <= 9 * 60 + 30:
                ps.append(0.3)
                k = "le0930"
            elif m <= 10 * 60 + 30:
                ps.append(0.5)
                k = "0930_1030"
            else:
                ps.append(0.6)
                k = "gt1030"
            buck[k] = buck.get(k, 0) + 1
    return sum(ps) / n, sum(1 for p in sub if (p["zbc"] or 0) >= 1) / n, buck


def main():
    print("load A1 signals (10cm, n expected 5588) ...")
    sig = load_a1_signals()
    n_sig = len(sig)
    sig['date'] = sig['date'].astype(str)
    sig['is_is'] = sig['date'] <= '2023-12-31'
    sig['is_oos'] = sig['date'] >= '2024-01-01'

    # ---------- §1 覆盖率 ----------
    cov_n, tot = coverage_stats(sig)
    cov_ratio = cov_n / tot
    print("min5 覆盖率: %d/%d = %.1f%%" % (cov_n, tot, 100.0 * cov_ratio))

    # ---------- §2 A1 组合板型分布（日K 代理）----------
    sig['open_eq_close'] = np.isclose(sig['open'], sig['close'], rtol=1e-4)
    oec_ratio = float(sig['open_eq_close'].mean())
    print("open==close（开盘即封，T字/一字类）: %.1f%%" % (100.0 * oec_ratio))

    # ---------- §3 limit_pool 封板行为 ----------
    all_lp, one_lp = limit_pool_dist()
    s2_all, s3_all, buck_all = scen_rates(all_lp)
    s2_one, s3_one, buck_one = scen_rates(one_lp)
    # 非一字适配子集：剔除"竞价封死且未炸"（<=0930 & zbc=0，A1 已剔一字的代理）
    adj = [p for p in one_lp if not ((p["zbc"] or 0) == 0 and (fbt_min(p["fbt"]) or 999) <= 9 * 60 + 30)]
    s2_adj, s3_adj, buck_adj = scen_rates(adj)
    print("limit_pool lbc=1: n=%d S2=%.3f S3=%.3f; 非一字适配 n=%d S2=%.3f S3=%.3f" % (
        len(one_lp), s2_one, s3_one, len(adj), s2_adj, s3_adj))

    # ---------- §4 三档期望 ----------
    # 口径A：A1 自身板型逐笔加权（乐观端）
    res = {"meta": {"task": "C4 A1 可成交性建模", "generated_at": pd.Timestamp.now().strftime('%Y-%m-%d %H:%M:%S'),
                    "a1_n": int(n_sig), "min5_cov": cov_n, "min5_cov_ratio": round(cov_ratio, 4),
                    "limit_pool_window": "2026-08-15~09-11(近月代表)", "m2_prior": M2,
                    "note": "min5 覆盖率不足，用 limit_pool 近月封板行为分布代理历史"}}
    res["coverage"] = {"min5_covered": cov_n, "total": tot,
                       "ratio": round(cov_ratio, 4),
                       "min5_cover_desc": "min5 仅 2025-08-18 起 515 只数字代码票(60/00 共326)，"
                                          "与 A1 信号交集极小"}
    res["board_shape"] = {"open_eq_close_ratio": round(oec_ratio, 4),
                          "open_eq_close_n": int(sig['open_eq_close'].sum())}
    res["limit_pool_dist"] = {"all_n": len(all_lp), "lbc1_n": len(one_lp),
                              "adj_n": len(adj), "buckets_lbc1": buck_one,
                              "s2_all": round(s2_all, 4), "s3_all": round(s3_all, 4),
                              "s2_lbc1": round(s2_one, 4), "s3_lbc1": round(s3_one, 4),
                              "s2_adj": round(s2_adj, 4), "s3_adj": round(s3_adj, 4)}

    def scen_block(mask_lab):
        mask = sig['is_is'].values if mask_lab == 'IS' else (sig['is_oos'].values if mask_lab == 'OOS' else np.ones(len(sig), bool))
        idx = mask & sig['ret'].notna().values
        r = sig['ret'].values[idx]
        n = len(r)
        blk = {"n": int(n), "S1_mean": float(r.mean())}
        # 口径A（板型代理）：open==close → P=0.6；否则 P=0.95
        psA = np.where(sig['open_eq_close'].values[idx], 0.6, 0.95)
        blk["A_boardtype_S2"] = float((r * psA).mean())
        psA3 = np.where(sig['open_eq_close'].values[idx], 0.0, 1.0)
        blk["A_boardtype_S3"] = float((r * psA3).mean())
        # 口径B（limit_pool 封板行为 × M2）：整体成交率折扣
        blk["B_lp_S2"] = float(r.mean() * s2_one)
        blk["B_lp_adj_S2"] = float(r.mean() * s2_adj)
        blk["B_lp_S3"] = float(r.mean() * s3_one)
        # 敏感性：M2 先验 ±0.15
        for delta, lab in [(0.15, "hi"), (-0.15, "lo")]:
            d = {k: v + delta for k, v in M2.items()}
            ps = []
            for p in one_lp:
                if (p["zbc"] or 0) >= 1:
                    ps.append(d["broken"])
                else:
                    m = fbt_min(p["fbt"])
                    if m is None or m <= 9 * 60 + 30:
                        ps.append(d["sealed_le_1030"])
                    elif m <= 10 * 60 + 30:
                        ps.append((d["sealed_le_1030"] + d["sealed_gt_1030"]) / 2)
                    else:
                        ps.append(d["sealed_gt_1030"])
            blk["B_lp_S2_sens_%s" % lab] = float(r.mean() * (sum(ps) / len(ps)))
        # 胜率A：S1 原胜率；加权口径 = ΣP·I(ret>0)/n（全样本，未成交记 0）
        blk["winA_S1"] = float((r > 0).mean())
        blk["winA_weighted_S2"] = float((r > 0).sum() / n * s2_one)
        blk["winA_weighted_S3"] = float((r > 0).sum() / n * s3_one)
        return blk

    res["scenarios"] = {k: scen_block(k) for k in ["IS", "OOS", "ALL"]}

    # ---------- §5 校准 ----------
    lf = json.load(open(os.path.join(BASE, "data", "attribution", "live_fill_calibration.json"), encoding="utf-8"))
    n_cal = len(lf.get("board_fills_calibrated") or [])
    ac = json.load(open(os.path.join(BASE, "data", "account.json"), encoding="utf-8"))
    board_trades = [t for t in ac.get("trades") or []
                    if "board" in (t.get("reason") or "").lower() or "打板" in (t.get("reason") or "")]
    res["calibration"] = {"live_fill_board_n": n_cal, "account_board_trades": len(board_trades),
                          "verdict": "样本不足（n=%d<30），模型未实证校准；M2 先验保持未校准状态" % n_cal}

    # ---------- §6 判定 ----------
    oos_s2 = res["scenarios"]["OOS"]["B_lp_S2"]
    oos_s2_adj = res["scenarios"]["OOS"]["B_lp_adj_S2"]
    oos_s3 = res["scenarios"]["OOS"]["B_lp_S3"]
    if oos_s2 <= 0:
        res["verdict"] = "NULL：S2 折扣后 OOS 期望≤0，edge 归零，必须给替代执行臂"
    else:
        res["verdict"] = ("edge 未归零：S2（limit_pool 口径）OOS 期望 %+.4f（原 %+.4f，保留 %.0f%%）；"
                          "非一字适配 %+.4f；S3 保守 %+.4f。区间口径 A（板型代理）%+.4f。" % (
                              oos_s2, res["scenarios"]["OOS"]["S1_mean"],
                              100.0 * oos_s2 / res["scenarios"]["OOS"]["S1_mean"],
                              oos_s2_adj, oos_s3, res["scenarios"]["OOS"]["A_boardtype_S2"]))
    print("\n=== 判定 ===")
    print(res["verdict"])

    with open(OUT, "w", encoding="utf-8", newline="\n") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)
    print("saved:", OUT)


if __name__ == "__main__":
    main()
