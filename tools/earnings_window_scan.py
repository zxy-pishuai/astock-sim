# -*- coding: utf-8 -*-
"""★ P71：业绩披露窗口禁买研究（只读 + 新工具，同日 A/B）

假设：财报披露前后股价波动放大/信息风险高 → report_date 前 N 日对该标的
不开新仓可改善收益/回撤。N ∈ {0, 3, 5}（0=仅披露当日禁买）。

实现（零源码修改）：worker 内构造 Backtest 后替换实例方法 `_signals_on`——
先调原方法取得候选，再剔除"距最近 report_date ∈ [0, N] 天"的标的。
PIT 近似声明：report_date 视为交易所提前公开的预约披露时间（实际可得）；
earnings 表为业绩预告（233 条，覆盖稀疏），过滤触发率天然偏低，报告如实注明。

配方与 Phase25/27/36 一致：bt_pool top500、fetch_quotes 置空、
score 25/3/0.30/0.001、四窗口、同日 A/B（基线臂同日实跑）。

证据规则（事先写死）：仅当某 N 使 ≥3/4 窗口收益改善或持平（Δ≥-1pp）且牛市
窗口恶化 ≤2pp 时才"建议落地"；否则如实写"无证据，维持现状"。本阶段不改 config。

用法: python tools/earnings_window_scan.py [--workers 10]
输出: data/bt_earnings_window.json
"""
import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime
from multiprocessing import Pool

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

WINDOWS = [
    ["2019-01-01", "2020-12-31"],
    ["2021-01-01", "2022-12-31"],
    ["2023-01-01", "2024-12-31"],
    ["2025-08-18", "2026-08-18"],
]
SCORE_PARAMS = {"buy_threshold": 25, "max_positions": 3,
                "position_pct": 0.30, "slippage": 0.001}
NS = [0, 3, 5]
OUT_JSON = os.path.join(BASE, "data", "bt_earnings_window.json")


def load_calendar():
    conn = sqlite3.connect("file:%s?mode=ro" % os.path.join(
        BASE, "data", "market.db"), uri=True, timeout=15)
    cal = {}
    for code, rd in conn.execute("SELECT code, report_date FROM earnings "
                                 "WHERE report_date IS NOT NULL"):
        cal.setdefault(code, []).append(rd)
    conn.close()
    return cal


def _worker(spec):
    n_days, w0, w1, codes, names = spec
    t0 = time.time()
    from app import config as C
    from app import engine as eng
    orig_q = eng.df.fetch_quotes
    eng.df.fetch_quotes = lambda cs: {}
    try:
        bt = eng.Backtest(codes, names, w0, w1, 100000.0, "score",
                          dict(SCORE_PARAMS))
        if n_days is not None:
            cal = load_calendar()
            orig = bt._signals_on

            def filtered(date, _orig=orig, _cal=cal, _n=n_days):
                sigs = _orig(date)
                d0 = datetime.strptime(date, "%Y-%m-%d")
                out = []
                blocked = 0
                for s in sigs:
                    hit = False
                    for rd in _cal.get(s["code"], ()):
                        try:
                            gap = (datetime.strptime(rd, "%Y-%m-%d") - d0).days
                        except ValueError:
                            continue
                        if 0 <= gap <= _n:
                            hit = True
                            break
                    if hit:
                        blocked += 1
                        continue
                    out.append(s)
                filtered.blocked_total += blocked
                return out
            filtered.blocked_total = 0
            bt._signals_on = filtered
        r = bt.run()
        blocked_total = getattr(getattr(bt, "_signals_on", None),
                                "blocked_total", 0)
        trades = list(bt.trades)
    finally:
        eng.df.fetch_quotes = orig_q
    if "error" in r:
        return {"error": r["error"], "n": n_days, "window": [w0, w1]}
    row = {
        "n": n_days, "window": [w0, w1],
        "total_return": r.get("total_return"),
        "max_drawdown": r.get("max_drawdown"),
        "sharpe": r.get("sharpe"),
        "trade_count": r.get("trade_count"),
        "signals_blocked": blocked_total,
        "elapsed_s": round(time.time() - t0),
    }
    print("  [N=%s] %s~%s ret=%+.4f dd=%.4f trades=%d 拦截信号=%d (%ds)" % (
        n_days if n_days is not None else "-", w0[:7], w1[:7],
        row["total_return"] or 0, row["max_drawdown"] or 0,
        row["trade_count"], blocked_total, row["elapsed_s"]), flush=True)
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=10)
    a = ap.parse_args()
    t0 = time.time()
    from tools.backtest_zt_eco import build_pool
    codes, names = build_pool()
    cal = load_calendar()
    print("池: %d 只 | earnings 预告覆盖 %d 只（稀疏，触发率天然偏低）" % (
        len(codes), len(cal)), flush=True)

    specs = []
    for n in [None] + NS:                      # None=基线臂（同日实跑）
        for w0, w1 in WINDOWS:
            specs.append((n, w0, w1, codes, names))
    results = []
    with Pool(processes=a.workers) as pool:
        for row in pool.imap_unordered(_worker, specs):
            results.append(row)

    def four(n):
        rs = sorted([r for r in results if "error" not in r and r["n"] == n],
                    key=lambda x: x["window"][0])
        assert len(rs) == 4, (n, len(rs))
        return rs

    base_rows = four(None)
    variants = []
    for n in NS:
        rs = four(n)
        deltas = [rs[i]["total_return"] - base_rows[i]["total_return"]
                  for i in range(4)]
        n_ok = sum(1 for d in deltas if d >= -0.01)
        bull_d = deltas[3]
        passed = n_ok >= 3 and bull_d >= -0.02
        variants.append({
            "n": n, "rows": rs,
            "deltas_pp": [round(d * 100, 2) for d in deltas],
            "improved_or_flat_windows": n_ok,
            "bull_delta_pp": round(bull_d * 100, 2),
            "avg_delta_pp": round(sum(deltas) / 4 * 100, 2),
            "signals_blocked_total": sum(r["signals_blocked"] for r in rs),
            "pass": passed,
        })
        print("变体 N=%d: Δ=%s 达标窗=%d 牛市Δ=%.2fpp 拦截信号=%d pass=%s" % (
            n, variants[-1]["deltas_pp"], n_ok, bull_d * 100,
            variants[-1]["signals_blocked_total"], passed), flush=True)

    winners = [v for v in variants if v["pass"]]
    verdict = {
        "rule": ">=3/4 窗口 Δ>=-1pp 且 牛市恶化<=2pp 才建议落地；否则维持现状",
        "recommended_n": (max(winners, key=lambda v: v["avg_delta_pp"])["n"]
                          if winners else None),
        "enabled": bool(winners),
        "reason": ("N=%s 达标（有证据，建议落地由验收方决定）" %
                   ",".join(str(w["n"]) for w in winners)) if winners else
                  "无 N 达标 → 无证据，维持现状（不引入披露窗口禁买）",
        "caveats": ["earnings 表为业绩预告（233 条），覆盖稀疏，过滤触发率低；",
                    "report_date 按交易所预约披露口径视为提前可知（PIT 近似）；",
                    "同日 A/B 口径（前复权重锚定下跨天不可比）。"],
    }
    payload = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "phase": "71",
        "windows": WINDOWS,
        "ns": NS,
        "params": SCORE_PARAMS,
        "baseline_rows": base_rows,
        "variants": variants,
        "verdict": verdict,
        "note_config": "本阶段不改 config；有证据才建议，落地由验收方决定",
    }
    with open(OUT_JSON, "w", encoding="utf-8", newline="\n") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    print("\n判定:", verdict["reason"], flush=True)
    print("已写出 %s | 总耗时 %.0fs" % (OUT_JSON, time.time() - t0), flush=True)


if __name__ == "__main__":
    main()
