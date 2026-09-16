# -*- coding: utf-8 -*-
"""H3 walk_forward 验证：数据共享（_wf_grid_select 一次加载）正确性与性能。
用法: python tools/bench_h3_wf.py [--pool N] [--folds N] [--no-share] [--no-cache]
输出: summary 关键字段 + 总耗时；--no-share 与默认对比得改善倍数。
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
    ap.add_argument("--folds", type=int, default=3)
    ap.add_argument("--no-share", action="store_true", help="BT_NO_SHARE=1（对照）")
    ap.add_argument("--no-cache", action="store_true", help="BT_NO_CACHE=1（对照）")
    ap.add_argument("--tag", default="wf")
    a = ap.parse_args()

    from app import config as C
    from app import engine as eng
    C.DB_FILE = SNAP_DB
    if a.no_share:
        os.environ["BT_NO_SHARE"] = "1"
    if a.no_cache:
        os.environ["BT_NO_CACHE"] = "1"
    with open(os.path.join(C.DATA_DIR, "bt_pool.json"), encoding="utf-8") as f:
        pool = json.load(f)
    codes = list(pool["codes"])[: a.pool]
    names = {c: c for c in codes}
    t0 = time.time()
    r = eng.walk_forward(codes, names, "2021-01-01", "2025-12-31",
                         strategy="score", folds=a.folds)
    dt = time.time() - t0
    if "error" in r:
        print("[%s] ERROR: %s" % (a.tag, r["error"]), flush=True)
        sys.exit(1)
    s = r["summary"]
    print("[%s] pool=%d folds=%d  avg_oos=%.4f avg_deg=%s pos=%d/%d stable=%s 总耗时 %.1fs" % (
        a.tag, a.pool, s["folds"], s.get("avg_oos_return"), s.get("avg_degradation"),
        s["positive_folds"], s["folds"], s["stable"], dt), flush=True)
    for f in r["folds"]:
        print("  fold%d train=%s test=%s is=%.4f oos=%.4f best=%s trades=%s" % (
            f["fold"], f["train"], f["test"], f.get("is_return"),
            f.get("oos_return"), (f.get("best_params") or {}).get("buy_threshold"),
            f.get("oos_trades")), flush=True)
    print("[%s] DONE %.1fs" % (a.tag, dt), flush=True)


if __name__ == "__main__":
    main()
