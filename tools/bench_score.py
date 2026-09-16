# -*- coding: utf-8 -*-
"""H4 score 性能基准与回归护栏（离线）。
- 输入：data/bench_fixtures.npz（3 票 × 1250 根日K，bench_fixtures_build.py 生成）
- 测：score_stock 单次耗时（热态 100 次中位数）、函数调用数（sys.setprofile 计数）、
      indicators 各函数单项耗时（50 次中位数）。
- 比对：data/bench_baseline.json 的 score 段；任一指标退化 >20% → 非 0 退出并打印退化项。
- 首次运行（无基线）自动写基线。
- 完全离线：不碰行情源/网络；score_stock 不传 code（跳过业绩/资金面查询维度，纯指标核心）。
用法: python tools\bench_score.py [--no-fail] [--runs 100] [--ind-runs 50]
"""
import argparse
import json
import os
import statistics
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

# 降噪：numpy/BLAS 固定单线程（基线自洽；避免线程池竞争噪声）
for _v in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS",
           "VECLIB_MAXIMUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ.setdefault(_v, "1")

FIXTURES = os.path.join(BASE, "data", "bench_fixtures.npz")
BASELINE = os.path.join(BASE, "data", "bench_baseline.json")
DEGRADE = 0.20  # 退化阈值：当前 > 基线 × 1.20


def load_fixtures():
    import numpy as np
    z = np.load(FIXTURES, allow_pickle=False)
    klines, meta = {}, {}
    for code in ("600000", "002001", "300750"):
        a = z[code]
        klines[code] = [{"date": str(r["date"]), "open": float(r["open"]),
                         "high": float(r["high"]), "low": float(r["low"]),
                         "close": float(r["close"]), "volume": float(r["volume"]),
                         "amount": float(r["amount"])} for r in a]
    for r in z["meta"]:
        meta[str(r["code"])] = {"label": str(r["label"]), "bars": int(r["bars"])}
    z.close()
    return klines, meta


def median_time(fn, runs):
    # 热态：先跑 10 次预热（CPU 频率/缓存稳定），再 runs 次计时取中位数
    for _ in range(10):
        fn()
    ts = []
    for _ in range(runs):
        t0 = time.perf_counter()
        fn()
        ts.append((time.perf_counter() - t0) * 1000)
    return statistics.median(ts)


def count_calls(fn):
    n = [0]

    def _prof(frame, event, arg):
        if event in ("call", "return", "c_call", "c_return", "c_exception"):
            n[0] += 1
    old = sys.getprofile()
    sys.setprofile(_prof)
    try:
        fn()
    finally:
        sys.setprofile(old)
    return n[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-fail", action="store_true", help="只报告不比退退出码（建基线用）")
    ap.add_argument("--update-baseline", action="store_true",
                    help="显式刷新基线到当前值（性能改造完成后人工确认用）")
    ap.add_argument("--runs", type=int, default=300)
    ap.add_argument("--ind-runs", type=int, default=50)
    a = ap.parse_args()

    from app import indicators as ind
    from app import scoring as sc

    import gc
    gc.disable()  # 计时段关 GC（H2 已知 GC 风暴是主要噪声源）

    klines, meta = load_fixtures()
    kl = klines["600000"]
    closes = [k["close"] for k in kl]
    volumes = [k["volume"] for k in kl]

    # ---- score_stock 核心（不传 code：跳过业绩/资金面维度，纯指标）----
    def _score():
        sc.score_stock(kl)
    ss_ms = median_time(_score, a.runs)
    ss_calls = count_calls(_score)

    # ---- indicators 单项 ----
    ind_fns = {
        "sma": lambda: ind.sma(closes, 20),
        "ema": lambda: ind.ema(closes, 20),
        "rsi": lambda: ind.rsi(closes),
        "macd": lambda: ind.macd(closes),
        "boll": lambda: ind.boll(closes),
        "atr": lambda: ind.atr(kl),
        "obv": lambda: ind.obv(kl),
        "rolling_max": lambda: ind.rolling_max(closes),
        "rolling_min": lambda: ind.rolling_min(closes),
        "volume_ma": lambda: ind.volume_ma(volumes),
        "vwap": lambda: ind.vwap(kl),
        "price_volume_corr": lambda: ind.price_volume_corr(closes, volumes),
        "rsrs": lambda: ind.rsrs(kl),
        "volume_slope": lambda: ind.volume_slope(volumes),
        "price_vol_divergence": lambda: ind.price_vol_divergence(closes, volumes),
        "obv_slope": lambda: ind.obv_slope(kl),
        "chip_profile": lambda: ind.chip_profile(kl),
    }
    ind_ms = {name: median_time(fn, a.ind_runs) for name, fn in ind_fns.items()}

    result = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "fixture": {"codes": list(meta.keys()), "bars": meta["600000"]["bars"],
                    "source": "data/bench_fixtures.npz"},
        "score_stock_ms": round(ss_ms, 4),
        "score_stock_calls": ss_calls,
        "indicators_ms": {k: round(v, 4) for k, v in sorted(ind_ms.items())},
    }
    print("[H4-score] score_stock %.4f ms | calls %d | 中位数(100x)" % (ss_ms, ss_calls))
    for k, v in sorted(ind_ms.items()):
        print("   %-24s %.4f ms" % (k, v))

    # ---- 比对基线 ----
    base = {}
    if os.path.exists(BASELINE):
        try:
            with open(BASELINE, encoding="utf-8") as f:
                base = json.load(f)
        except Exception:
            base = {}
    cur = {"score_stock_ms": ss_ms, "score_stock_calls": ss_calls,
           "indicators_ms": ind_ms}
    failed = []
    if base.get("score"):
        b = base["score"]
        for key in ("score_stock_ms", "score_stock_calls"):
            if key in b and key in cur:
                if cur[key] > b[key] * (1 + DEGRADE):
                    failed.append("%s: 基线 %.4f → 当前 %.4f（+%.1f%%）" % (
                        key, b[key], cur[key], (cur[key] / b[key] - 1) * 100))
        bi = b.get("indicators_ms") or {}
        for k, v in cur["indicators_ms"].items():
            if k in bi and v > bi[k] * (1 + DEGRADE):
                failed.append("indicator[%s]: 基线 %.4f → 当前 %.4f（+%.1f%%）" % (
                    k, bi[k], v, (v / bi[k] - 1) * 100))
    if failed:
        print("[H4-score] ❌ 退化项 %d:" % len(failed))
        for f in failed:
            print("   ", f)
    else:
        print("[H4-score] ✅ 无退化（阈值 +%.0f%%）" % (DEGRADE * 100))

    # ---- 写基线（首次 or 刷新）----
    if not os.path.exists(BASELINE):
        with open(BASELINE, "w", encoding="utf-8") as f:
            json.dump({"schema": "v1",
                       "comment": "H4 性能基线：score 段由 bench_score.py 维护，backtest 段由 "
                                  "bench_backtest.py 维护；退化阈值 +20%；离线 fixtures 基准；"
                                  "降噪：GC 关闭 + BLAS 单线程 + 热态多次中位数",
                       "generated_at": result["generated_at"], "score": cur}, f,
                      ensure_ascii=False, indent=1)
        print("[H4-score] 首次运行：已写入基线 %s" % BASELINE)
    elif not base.get("score"):
        with open(BASELINE, "w", encoding="utf-8") as f:
            json.dump({"schema": "v1", "comment": base.get("comment", ""),
                       "generated_at": result["generated_at"],
                       "score": cur, "backtest": base.get("backtest")}, f,
                      ensure_ascii=False, indent=1)
        print("[H4-score] 基线缺少 score 段：已写入")
    elif base.get("score") and a.update_baseline:
        with open(BASELINE, "w", encoding="utf-8") as f:
            json.dump({"schema": "v1", "comment": base.get("comment", ""),
                       "generated_at": result["generated_at"],
                       "score": cur, "backtest": base.get("backtest")}, f,
                      ensure_ascii=False, indent=1)
        print("[H4-score] 基线已显式刷新（--update-baseline）")

    if failed and not a.no_fail:
        print("[H4-score] EXIT=1（性能退化）")
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
