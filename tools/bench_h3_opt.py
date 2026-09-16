# -*- coding: utf-8 -*-
"""H3 optimize_params 对比：串行 vs 并行（BT_PARALLEL_ENABLED 开关）。
用法: python tools/bench_h3_opt.py [--pool N] [--serial] [--no-cache]
输出: best/前3 rows + 总耗时。
"""
import argparse
import json
import os
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

SNAP_DB = os.path.join(BASE, "data", "snapshots", "2026-09-11", "market.db")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", type=int, default=100)
    ap.add_argument("--serial", action="store_true", help="BT_PARALLEL_ENABLED=False")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--tag", default="opt")
    a = ap.parse_args()

    from app import config as C
    from app import engine as eng
    C.DB_FILE = SNAP_DB
    if a.serial:
        C.BT_PARALLEL_ENABLED = False
    if a.no_cache:
        os.environ["BT_NO_CACHE"] = "1"
    with open(os.path.join(C.DATA_DIR, "bt_pool.json"), encoding="utf-8") as f:
        pool = json.load(f)
    codes = list(pool["codes"])[: a.pool]
    names = {c: c for c in codes}
    t0 = time.time()
    r = eng.optimize_params(codes, names, "2021-01-01", "2025-12-31",
                            strategy="score",
                            grid={"buy_threshold": [20, 25, 30],
                                  "max_positions": [2, 3, 4]},
                            metric="total_return")
    dt = time.time() - t0
    print("[%s] pool=%d serial=%s searched=%d 总耗时 %.1fs" % (
        a.tag, a.pool, a.serial, r["searched"], dt), flush=True)
    print("[%s] best=%s" % (a.tag, r["best"]), flush=True)
    for row in r["results"][:5]:
        print("   %s tr=%.4f dd=%.4f win=%.4f trades=%d" % (
            row["params"], row.get("total_return") or 0,
            row.get("max_drawdown") or 0, row.get("win_rate") or 0,
            row.get("trade_count") or 0), flush=True)
    print("[%s] DONE %.1fs" % (a.tag, dt), flush=True)


if __name__ == "__main__":
    main()
