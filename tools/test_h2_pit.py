# -*- coding: utf-8 -*-
"""H2 PIT 测试 + 数值等价抽查：
1) _hist_klines 返回视图严格截止 i（含），篡改 i 之后数据不影响第 i 日信号/分数；
2) _score_stock_at 与回测现行评分入口（SCORING_UNIFIED=False → score_final(ctx=None)
   = score_stock）逐日逐条一致。
用法: python tools/test_h2_pit.py
"""
import os
import random
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

SNAP_DB = os.path.join(BASE, "data", "snapshots", "2026-09-11", "market.db")


def make_bt(codes, start="2021-01-01", end="2026-08-18"):
    from app import config as C
    from app import engine as eng
    C.DB_FILE = SNAP_DB
    names = {c: c for c in codes}
    orig = eng.df.fetch_quotes
    eng.df.fetch_quotes = lambda cs: {}
    try:
        bt = eng.Backtest(codes, names, start, end, 100000.0, "score",
                          {"buy_threshold": 25, "max_positions": 3,
                           "position_pct": 0.30, "slippage": 0.001,
                           "zt_eco_gate": False, "dd_gate": False})
        bt._load_data()
        return bt
    finally:
        eng.df.fetch_quotes = orig


def pick_dates(bt, code, n=5, seed=7):
    di = bt.date_index.get(code)
    if not di:
        return []
    items = sorted((d, i) for d, i in di.items() if i >= 249)
    if len(items) <= n:
        return items
    r = random.Random(seed)
    return r.sample(items, n)


def check_equivalence(bt, code, dates):
    from app import scoring as sc
    from app import config as C
    kl = bt.klines[code]
    bad = 0
    for d, i in dates:
        hist = kl[:i + 1]
        exp_s, exp_sig, _bd = sc.score_final(hist, code=code, as_of=d, ctx=None)
        got_s, got_sig = bt._score_stock_at(code, i, d)
        if exp_s != got_s or list(exp_sig) != list(got_sig):
            bad += 1
            print("  [MISMATCH] %s %s i=%d exp=(%s, %s) got=(%s, %s)" % (
                code, d, i, exp_s, exp_sig[:3], got_s, got_sig[:3]))
    return bad


def check_pit_view(bt, code, dates):
    """篡改 i 之后的全部 bar 为极端值，断言第 i 日评分不变 + 视图长度正确。"""
    import numpy as np
    kl = bt.klines[code]
    di = bt.date_index[code]
    bad = 0
    for d, i in dates:
        s0, sig0 = bt._score_stock_at(code, i, d)
        # 视图边界：len == i+1，访问 i+1 越界
        view = bt._hist_klines(code, d)
        if len(view) != i + 1:
            print("  [PIT-VIEW] %s %s len(view)=%d 期望 %d" % (code, d, len(view), i + 1))
            bad += 1
            continue
        # 篡改 klines 里 i 之后的所有 bar（原始对象结构不变，仅值变化）
        sav = []
        for k in kl[i + 1:]:
            sav.append((k["close"], k["high"], k["low"], k["open"], k["volume"]))
            k["close"] = 100000.0
            k["high"] = 100000.0
            k["low"] = 0.01
            k["open"] = 100000.0
            k["volume"] = 10**10
        s1, sig1 = bt._score_stock_at(code, i, d)
        if s0 != s1 or list(sig0) != list(sig1):
            print("  [PIT-FAIL] %s %s i=%d 篡改后评分变化 s0=%s s1=%s" % (code, d, i, s0, s1))
            bad += 1
        # 还原
        for k, sv in zip(kl[i + 1:], sav):
            k["close"], k["high"], k["low"], k["open"], k["volume"] = sv
    return bad


def main():
    if not os.path.exists(SNAP_DB):
        print("快照缺失", SNAP_DB)
        sys.exit(2)
    # 覆盖 60 开头（主板）/ 00（深主板）/ 30（创业板）/ 688（科创）
    codes = ["000002", "600000", "300308", "688777", "000001"]
    bt = make_bt(codes)
    total_bad = 0
    for code in codes:
        dates = pick_dates(bt, code, n=6)
        if not dates:
            print("  %s 无足够历史" % code)
            continue
        total_bad += check_equivalence(bt, code, dates)
        total_bad += check_pit_view(bt, code, dates)
    if total_bad == 0:
        print("H2 PIT/等价测试: 全部通过（5 只 × 6 日 × 2 项）")
    else:
        print("H2 PIT/等价测试: %d 项失败" % total_bad)
        sys.exit(1)


if __name__ == "__main__":
    main()
