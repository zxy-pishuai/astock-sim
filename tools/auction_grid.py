# -*- coding: utf-8 -*-
"""★ Phase40：auction_intraday 参数网格（现成策略从未调参）

策略：app/engine_minute.MinuteBoardBacktest 的 auction_intraday
（首根 9:35 bar 开盘高开 [auction_min_pct, auction_max_pct) 且 竞价量比
(首根量/昨日全天量) ≥ auction_vr_min → 开盘买入）。

网格：auction_min_pct ∈ {2.0, 3.0, 4.0} × auction_vr_min ∈ {0.010, 0.015, 0.030}
     （auction_max_pct 固定 9.8），共 9 变体；
对照组：board_intraday 默认参数同期同池。
窗口：2025-08-18 ~ 2026-08-21（min5.db 全量覆盖期，牛市段）。
池：data/bt_pool.json 前 200（2026-08-21 成交额降序，排除 DATA_EXCLUDE_CODES）。

判定规则（事先写死）：最优变体须【收益与 Calmar 同时】优于 board_intraday 对照，
且交易笔数 ≥100 → 推荐该参数组合。★单一牛市窗口样本（246 个交易日、无熊市段）
的局限在报告显式声明：结论只对该市况有效；推荐参数只写报告，不改 config。

用法: python tools/auction_grid.py [--workers 3]
输出: data/bt_auction_grid.json
"""
import argparse
import json
import os
import sys
import time
from multiprocessing import Pool

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from app import config as C

W_START, W_END = "2025-08-18", "2026-08-21"
POOL_N = 200
GRID_MIN_PCT = [2.0, 3.0, 4.0]
GRID_VR_MIN = [0.010, 0.015, 0.030]
AUCTION_MAX = 9.8
OUT_JSON = os.path.join(BASE, "data", "bt_auction_grid.json")


def build_pool():
    pool = json.load(open(os.path.join(BASE, "data", "bt_pool.json"),
                          encoding="utf-8"))
    ex = set(getattr(C, "DATA_EXCLUDE_CODES", None) or [])
    codes = [c for c in pool["codes"] if c not in ex][:POOL_N]
    names = {c: c for c in codes}
    return codes, names


def _worker(spec):
    kind, params, tag, codes, names = spec
    t0 = time.time()
    from app.engine_minute import MinuteBoardBacktest as M
    bt = M(codes, names, W_START, W_END, 100000.0,
           strategy=kind, params=dict(params))
    r = bt.run()
    if "error" in r:
        return {"tag": tag, "error": r["error"]}
    mdd = r.get("max_drawdown") or 0.0
    ann = r.get("annual_return") or 0.0
    prem = r.get("premium") or {}
    row = {
        "tag": tag, "strategy": kind, "params": {k: v for k, v in params.items()
                                                 if k.startswith("auction")},
        "window": [W_START, W_END], "pool_n": len(codes),
        "total_return": r.get("total_return"),
        "annual_return": r.get("annual_return"),
        "max_drawdown": mdd,
        "calmar": round(ann / abs(mdd), 3) if mdd < 0 else None,
        "sharpe": r.get("sharpe"),
        "win_rate": r.get("win_rate"),
        "trade_count": r.get("trade_count"),
        "buy_count": r.get("buy_count"),
        "premium": {"total": prem.get("total"), "win_rate": prem.get("win_rate"),
                    "avg_open": prem.get("avg_open"), "avg_close": prem.get("avg_close"),
                    "avg_high": prem.get("avg_high")},
        "elapsed_s": round(time.time() - t0),
    }
    print("  [%s] ret=%+.4f calmar=%s trades=%d win=%.1f%% 次日溢价均收=%s (%ds)" % (
        tag, row["total_return"] or 0, row["calmar"], row["trade_count"],
        (row["win_rate"] or 0) * 100, prem.get("avg_close"), row["elapsed_s"]),
        flush=True)
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=3,
                    help="每 worker 需载入 200 股一年 5m 数据（内存大），默认 3")
    a = ap.parse_args()
    t0 = time.time()
    global codes, names
    codes, names = build_pool()
    print("池: %d 只 | 窗口 %s ~ %s" % (len(codes), W_START, W_END), flush=True)

    specs = [("board_intraday", {}, "board_default", codes, names)]
    for mn in GRID_MIN_PCT:
        for vr in GRID_VR_MIN:
            specs.append(("auction_intraday",
                          {"auction_min_pct": mn, "auction_max_pct": AUCTION_MAX,
                           "auction_vr_min": vr},
                          "auc_min%.1f_vr%.3f" % (mn, vr), codes, names))

    results = []
    with Pool(processes=a.workers) as pool:
        for row in pool.imap_unordered(_worker, specs):
            results.append(row)

    by_tag = {r["tag"]: r for r in results if "error" not in r}
    base = by_tag.get("board_default")
    variants = []
    for mn in GRID_MIN_PCT:
        for vr in GRID_VR_MIN:
            tag = "auc_min%.1f_vr%.3f" % (mn, vr)
            r = by_tag.get(tag)
            if not r:
                continue
            beats_ret = base and r["total_return"] > base["total_return"]
            beats_cal = base and (r["calmar"] or -99) > (base["calmar"] or -99)
            enough = r["trade_count"] >= 100
            variants.append({**r,
                             "beats_return": beats_ret, "beats_calmar": beats_cal,
                             "trades_ge_100": enough})
    ok = [v for v in variants if v["beats_return"] and v["beats_calmar"]
          and v["trades_ge_100"]]
    best = max(ok, key=lambda v: v["total_return"]) if ok else None
    verdict = {
        "rule": "最优变体收益与 Calmar 同时优于 board_intraday 对照且交易数≥100；"
                "推荐只写报告不改 config",
        "baseline_tag": "board_default",
        "recommended_params": best["params"] if best else None,
        "enabled": bool(best),
        "reason": ("推荐 %s（ret=%+.4f, calmar=%s, trades=%d）" % (
            best["tag"], best["total_return"], best["calmar"], best["trade_count"])
            if best else "无变体同时满足收益+Calmar 优于对照且样本≥100 → 维持现状"),
        "sample_limitation": "★单一牛市窗口（2025-08~2026-08，246 交易日、无熊市段）："
                             "结论只对该市况有效，不能外推为全天候参数。",
    }
    payload = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "phase": "40",
        "window": [W_START, W_END], "pool_n": POOL_N,
        "grid": {"auction_min_pct": GRID_MIN_PCT, "auction_vr_min": GRID_VR_MIN,
                 "auction_max_pct": AUCTION_MAX},
        "board_baseline": base,
        "variants": variants,
        "verdict": verdict,
    }
    with open(OUT_JSON, "w", encoding="utf-8", newline="\n") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    print("\n判定:", verdict["reason"], flush=True)
    print(verdict["sample_limitation"], flush=True)
    print("已写出 %s | 总耗时 %.0fs" % (OUT_JSON, time.time() - t0), flush=True)


if __name__ == "__main__":
    main()
