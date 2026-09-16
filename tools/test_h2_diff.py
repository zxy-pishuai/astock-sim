# -*- coding: utf-8 -*-
"""H2 差异定位：50 池 × score 5年窗，逐日对比 sc.score_final(ctx=None) 与
bt._score_stock_at，输出第一个不一致的 (code, date) 与分数/信号明细。"""
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

SNAP_DB = os.path.join(BASE, "data", "snapshots", "2026-09-11", "market.db")
W0, W1 = "2021-01-01", "2025-12-31"


def main():
    from app import config as C
    from app import engine as eng
    from app import scoring as sc
    C.DB_FILE = SNAP_DB
    with open(os.path.join(C.DATA_DIR, "bt_pool.json"), encoding="utf-8") as f:
        pool = json.load(f)
    codes = list(pool["codes"])[:50]
    names = {c: c for c in codes}
    orig = eng.df.fetch_quotes
    eng.df.fetch_quotes = lambda cs: {}
    try:
        bt = eng.Backtest(codes, names, W0, W1, 100000.0, "score",
                          {"buy_threshold": 25, "max_positions": 3,
                           "position_pct": 0.30, "slippage": 0.001,
                           "zt_eco_gate": False, "dd_gate": False})
        bt._load_data()
    finally:
        eng.df.fetch_quotes = orig

    n_checked = 0
    for code in codes:
        kl = bt.klines.get(code)
        di = bt.date_index.get(code)
        if not kl or not di:
            continue
        for d, i in sorted(di.items(), key=lambda x: x[1]):
            if i < 249:
                continue
            if not (W0 <= d <= W1):
                continue
            n_checked += 1
            exp_s, exp_sig, _bd = sc.score_final(kl[:i + 1], code=code, as_of=d, ctx=None)
            got_s, got_sig = bt._score_stock_at(code, i, d)
            if exp_s != got_s or list(exp_sig) != list(got_sig):
                print("首个不一致: %s %s i=%d" % (code, d, i))
                print("  exp score=%s signals=%s" % (exp_s, exp_sig))
                print("  got score=%s signals=%s" % (got_s, got_sig))
                b = kl[i]
                print("  bar:", {k: b[k] for k in ("date", "open", "high", "low", "close", "volume")})
                print("  检查 %d 个 (code,date) 后命中" % n_checked)
                sys.exit(1)
        print("  %s 全一致 (%d 日)" % (code, len([1 for d, i in di.items() if i >= 249 and W0 <= d <= W1])))
    print("50 池全一致，共检查 %d 个评分点" % n_checked)


if __name__ == "__main__":
    main()
