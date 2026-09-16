# -*- coding: utf-8 -*-
"""★ Phase38 保留意见处置：auction_intraday 抽样逐笔回对

背景：docs/reports/auction_grid.md §4 对 min4.0 变体（+389.7%）提出三项保留。
本工具重跑 min4.0 变体取得逐笔买入，再对当日 min5 分时做可成交性回对：
  C1 一字板拦截：首根 bar 开盘 ≥ 涨停价×0.999 → 引擎已拦（应=0 笔漏网）
  C2 无对手价：买入所属 bar 成交量 = 0 → 真实盘口无成交，不可能成交
  C3 价格越界：买入价(计滑点) 越出该 bar [low, high] 区间 → 回测价不可实现
  C4 秒封核查：买入后至收盘若长期封死涨停，开盘价仍可能成交（集合竞价），
     不计入违规——只统计"买入 bar 即封死"（bar 收盘==涨停且 open==close）
输出：抽样结论表 + 可成交占比（真实排队约束下的折扣估计）。

用法: python tools/auction_verify.py [--sample 40]
输出: data/attribution/auction_verify.json
"""
import argparse
import json
import os
import sqlite3
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from app import config as C
from app import engine as eng

W_START, W_END = "2025-08-18", "2026-08-21"
AUCTION_PARAMS = {"auction_min_pct": 4.0, "auction_max_pct": 9.8,
                  "auction_vr_min": 0.010}
OUT_JSON = os.path.join(BASE, "data", "attribution", "auction_verify.json")


def rerun_variant(codes, names):
    from app.engine_minute import MinuteBoardBacktest as M
    orig_q = None
    try:
        bt = M(codes, names, W_START, W_END, 100000.0,
               strategy="auction_intraday", params=dict(AUCTION_PARAMS))
        r = bt.run()
        return r, bt
    finally:
        pass


def load_min5_day(sym, day):
    conn = sqlite3.connect("file:%s?mode=ro" % os.path.join(
        BASE, "data", "min5.db"), uri=True, timeout=15)
    rows = conn.execute(
        "SELECT date, open, high, low, close, volume FROM kline_min5 "
        "WHERE code=? AND date LIKE ? ORDER BY date",
        (sym, day + "%")).fetchall()
    conn.close()
    return [(r[0][11:16], r[1], r[2], r[3], r[4], r[5] or 0) for r in rows]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=40)
    a = ap.parse_args()
    t0 = time.time()
    from tools.auction_grid import build_pool
    codes, names = build_pool()
    print("重跑 min4.0 变体取逐笔...", flush=True)
    _r, bt = rerun_variant(codes, names)
    buys = [t for t in bt.trades if t["side"] == "buy"]
    print("全部买入 %d 笔" % len(buys), flush=True)
    # 等距抽样铺满窗口
    step = max(1, len(buys) // a.sample)
    sample = buys[::step][:a.sample]

    def min5_code(code):
        return ("sh" if code.startswith("6") else "sz") + code

    rows = []
    counts = {"C1_yizi": 0, "C2_noliquidity": 0, "C3_out_of_range": 0,
              "C4_entry_bar_sealed": 0}
    for t in sample:
        code, day = t["code"], t["date"]
        bars = load_min5_day(min5_code(code), day)
        first = bars[0] if bars else None
        prev = bt._prev_close(code, day)
        lu, _ld = eng.limit_prices(code, prev) if prev else (None, None)
        entry_px = t["price"]          # 已含滑点
        checks = {}
        if first:
            tm, o, h, l, c, v = first
            checks["C1_yizi"] = bool(lu and o >= lu * 0.999)
            checks["C2_noliquidity"] = bool(v <= 0)
            band_lo, band_hi = l * (1 - 0.001), h * (1 + 0.001)
            checks["C3_out_of_range"] = not (band_lo <= entry_px <= band_hi)
            sealed_now = bool(lu and abs(o - lu) < 0.005 and abs(c - lu) < 0.005)
            checks["C4_entry_bar_sealed"] = sealed_now
        else:
            checks["no_bars"] = True
        for k in ("C1_yizi", "C2_noliquidity", "C3_out_of_range",
                  "C4_entry_bar_sealed"):
            if checks.get(k):
                counts[k] += 1
        fillable = not any(checks.get(k) for k in
                           ("C1_yizi", "C2_noliquidity", "C3_out_of_range"))
        rows.append({
            "date": day, "code": code, "name": names.get(code, code),
            "entry_px": entry_px, "first_bar": first,
            "limit_up": lu, "checks": checks, "fillable_under_real_queue": fillable,
        })
    n = len(rows)
    fillable_n = sum(1 for r in rows if r["fillable_under_real_queue"])
    summary = {
        "variant": "auc_min4.0_vr0.010",
        "all_buys": len(buys), "sampled": n,
        "violations": counts,
        "fillable": fillable_n,
        "fillable_ratio": round(fillable_n / max(1, n), 4),
        "conclusion": (
            "抽样 %d 笔中 %.0f%% 在真实排队约束下可成交；违规 %s" % (
                n, fillable_n / max(1, n) * 100,
                json.dumps(counts, ensure_ascii=False))
            if n else "无样本"),
    }
    payload = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "phase": "38复核",
        "window": [W_START, W_END],
        "params": AUCTION_PARAMS,
        "checks_legend": {
            "C1_yizi": "首根bar开盘≥涨停价×0.999（一字板，引擎应已拦）",
            "C2_noliquidity": "买入bar成交量=0（无对手价）",
            "C3_out_of_range": "买入价越出 bar [low,high]±0.1% 区间",
            "C4_entry_bar_sealed": "买入bar即封死（开盘=收盘=涨停；不计入不可成交，仅披露）",
        },
        "summary": summary,
        "rows": rows,
    }
    os.makedirs(os.path.dirname(OUT_JSON), exist_ok=True)
    with open(OUT_JSON, "w", encoding="utf-8", newline="\n") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    print("已写出 %s | 总耗时 %.0fs" % (OUT_JSON, time.time() - t0), flush=True)


if __name__ == "__main__":
    main()
