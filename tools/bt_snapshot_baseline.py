# -*- coding: utf-8 -*-
"""★ Phase46: 快照口径 vs 活库口径 基线对照（score/board 四窗口，同日双跑）。

用法：
  python tools/bt_snapshot_baseline.py --db <snapshot.market.db> --strategy score
输出落 data/snapshot_baseline_parts/<tag>_<strategy>.json，由主脚本汇总。
"""
import sys
import os
import json
import time
import argparse

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

WINDOWS = [["2019-01-01", "2020-12-31"], ["2021-01-01", "2022-12-31"],
           ["2023-01-01", "2024-12-31"], ["2025-08-18", "2026-08-21"]]
PARAMS = {
    "score": {"buy_threshold": 25, "max_positions": 3, "position_pct": 0.30, "slippage": 0.001},
    "board": {"buy_threshold": 40, "max_positions": 2, "position_pct": 0.25, "slippage": 0.001},
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--tag", required=True, help="口径标签（snapshot / live）")
    ap.add_argument("--strategy", required=True, choices=["score", "board"])
    a = ap.parse_args()

    from app import config as C
    from app import engine as eng
    eng.df.fetch_quotes = lambda cs: {}
    C.DB_FILE = a.db                       # ★ 口径切换：指向冻结快照或活库
    C.BOARD_MOMENTUM_MIN = 7.0             # 冻结到发布基线口径（Phase33 已改默认）

    pool = json.load(open(os.path.join(C.DATA_DIR, "bt_pool.json"), encoding="utf-8"))
    codes = pool["codes"][:500]
    names = {c: c for c in codes}

    out_path = os.path.join(C.DATA_DIR, "snapshot_baseline_parts",
                            "%s_%s.json" % (a.tag, a.strategy))
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    done = {}
    if os.path.exists(out_path):
        try:
            done = json.load(open(out_path, encoding="utf-8")).get("windows", {})
        except Exception:
            done = {}
    for wi, (w0, w1) in enumerate(WINDOWS):
        key = str(wi)
        if key in done and "error" not in done[key]:
            continue
        t0 = time.time()
        bt = eng.Backtest(codes, names, w0, w1, 100000.0, a.strategy,
                          dict(PARAMS[a.strategy]))
        r = bt.run()
        r["elapsed"] = round(time.time() - t0, 1)
        done[key] = {"window": [w0, w1], **r}
        tmp = out_path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump({"tag": a.tag, "strategy": a.strategy, "windows": done},
                      f, ensure_ascii=False, indent=1)
        os.replace(tmp, out_path)
        print("[done] %s %s win%d ret=%.4f trades=%d (%.0fs)" % (
            a.tag, a.strategy, wi, r.get("total_return") or 0,
            r.get("trade_count"), r.get("elapsed", 0)), flush=True)
    print("[complete]", out_path)


if __name__ == "__main__":
    main()
