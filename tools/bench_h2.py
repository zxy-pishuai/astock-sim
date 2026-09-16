# -*- coding: utf-8 -*-
"""H2 改前/改后对照基准：三策略 × 两窗 + score 500 池性能窗。
用法:
  python tools/bench_h2.py --stage before --pool 200 --out tmp/h2/before_200.json
  python tools/bench_h2.py --stage after  --pool 200 --out tmp/h2/after_200.json
  python tools/bench_h2.py --stage before --pool 500 --perf --out tmp/h2/perf_before.json
"""
import argparse
import json
import os
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# 显式钉快照（2026-09-11，最新冻结库）
SNAP_DB = os.path.join(BASE, "data", "snapshots", "2026-09-11", "market.db")

WINDOWS = [
    ("2025-08-18", "2026-08-18", "近1年"),
    ("2021-01-01", "2025-12-31", "5年"),
]
STRATEGY_PARAMS = {
    "score": {"buy_threshold": 25, "max_positions": 3, "position_pct": 0.30},
    "board": {"buy_threshold": 40, "max_positions": 2, "position_pct": 0.25},
    "twothirty": {"max_positions": 3, "position_pct": 0.30},
}
BASE_PARAMS = {"zt_eco_gate": False, "dd_gate": False}
SLIPPAGE = 0.001


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", required=True)
    ap.add_argument("--pool", type=int, default=200)
    ap.add_argument("--perf", action="store_true", help="只跑 score 单窗性能窗（--win 指定）")
    ap.add_argument("--win", type=int, default=0, help="性能窗 widx：0=近1年 1=5年")
    ap.add_argument("--serial", action="store_true", help="H3：强制 _signals_on 串行（BT_PARALLEL_ENABLED=False）")
    ap.add_argument("--no-cache", action="store_true", help="H3：旁路 bt_cache（BT_NO_CACHE=1）")
    ap.add_argument("--cache-2nd", action="store_true", help="H3：打印缓存命中统计（供二次运行验证）")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    from app import config as C
    from app import engine as eng

    if a.serial:
        C.BT_PARALLEL_ENABLED = False
    if a.no_cache:
        os.environ["BT_NO_CACHE"] = "1"

    if not os.path.exists(SNAP_DB):
        print("[H2] 快照缺失: %s" % SNAP_DB, flush=True)
        sys.exit(2)
    C.DB_FILE = SNAP_DB
    with open(os.path.join(C.DATA_DIR, "bt_pool.json"), encoding="utf-8") as f:
        pool = json.load(f)
    codes = list(pool["codes"])[: a.pool]
    names = {c: c for c in codes}

    rows = []
    tasks = []
    _cache_dir = os.path.join(C.DATA_DIR, "bt_cache")
    _cache_before = set(os.listdir(_cache_dir)) if os.path.isdir(_cache_dir) else set()
    if a.perf:
        tasks = [("score", a.win)]
    else:
        tasks = [(s, w) for s in ("score", "board", "twothirty") for w in range(len(WINDOWS))]

    for strategy, widx in tasks:
        w0, w1, wtag = WINDOWS[widx]
        t0 = time.time()
        orig_quotes = eng.df.fetch_quotes
        orig_mom = getattr(C, "BOARD_MOMENTUM_MIN", None)
        eng.df.fetch_quotes = lambda cs: {}
        C.BOARD_MOMENTUM_MIN = 7.0
        try:
            params = dict(STRATEGY_PARAMS[strategy])
            params.update(BASE_PARAMS)
            params["slippage"] = SLIPPAGE
            bt = eng.Backtest(codes, names, w0, w1, 100000.0, strategy, params)
            r = bt.run()
        finally:
            eng.df.fetch_quotes = orig_quotes
            if orig_mom is not None:
                C.BOARD_MOMENTUM_MIN = orig_mom
        row = {"stage": a.stage, "strategy": strategy, "widx": widx,
               "window": [w0, w1], "window_tag": wtag, "pool": a.pool,
               "total_return": r.get("total_return"),
               "annual_return": r.get("annual_return"),
               "max_drawdown": r.get("max_drawdown"),
               "win_rate": r.get("win_rate"),
               "sharpe": r.get("sharpe"),
               "trade_count": len(bt.trades),
               "elapsed": round(time.time() - t0, 1)}
        rows.append(row)
        print("[%s %s %s pool=%d] ret=%s dd=%s win=%s trades=%d (%.0fs)" % (
            a.stage, strategy, wtag, a.pool, row["total_return"],
            row["max_drawdown"], row["win_rate"], row["trade_count"],
            row["elapsed"]), flush=True)

    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1)
    if a.cache_2nd:
        _cache_after = set(os.listdir(_cache_dir)) if os.path.isdir(_cache_dir) else set()
        _new = _cache_after - _cache_before
        print("[H3] cache-dir 前后文件数: %d -> %d（本跑新增 %d：%s）" % (
            len(_cache_before), len(_cache_after), len(_new), ",".join(sorted(_new)[:3])),
            flush=True)
    print("[H2] saved %s" % a.out, flush=True)


if __name__ == "__main__":
    main()
