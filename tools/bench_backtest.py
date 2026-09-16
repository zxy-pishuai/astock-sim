# -*- coding: utf-8 -*-
"""H4 回测性能基准与回归护栏（离线）。
- 数据：钉快照 data/snapshots/2026-09-11/market.db（只读 URI），BT_NO_CACHE=1 强制直读
      （离线确定性：不依赖缓存命中状态、不碰网络/行情源）。
- codes：与 fixtures 同 3 票（600000/002001/300750）；窗口 2022-01-01~2026-08-18；
      score 策略固定参数（bt25/mpos3/ppct0.30/slippage0.001/zt/dd gate False）。
- 指标：total_elapsed_s（run 总耗时）、load_elapsed_s（_load_data 耗时）、
      signals_on_avg_ms（窗内 5 个等间隔交易日的 _signals_on 平均耗时）、
      peak_memory_mb（tracemalloc 单独跑近1年窗的峰值）。
- 比对：data/bench_baseline.json 的 backtest 段；退化 >20% → 非 0 退出并打印退化项。
用法: python tools\bench_backtest.py [--no-fail]
"""
import argparse
import json
import os
import statistics
import sys
import time
import tracemalloc

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

# 降噪：numpy/BLAS 固定单线程
for _v in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS",
           "VECLIB_MAXIMUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ.setdefault(_v, "1")

SNAP_DB = os.path.join(BASE, "data", "snapshots", "2026-09-11", "market.db")
BASELINE = os.path.join(BASE, "data", "bench_baseline.json")
DEGRADE = 0.20
CODES = ["600000", "002001", "300750"]
PARAMS = {"buy_threshold": 25, "max_positions": 3, "position_pct": 0.3,
          "slippage": 0.001, "zt_eco_gate": False, "dd_gate": False}
START, END = "2022-01-01", "2026-08-18"
MEM_START, MEM_END = "2025-08-18", "2026-08-18"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-fail", action="store_true")
    ap.add_argument("--update-baseline", action="store_true",
                    help="显式刷新基线到当前值（性能改造完成后人工确认用）")
    a = ap.parse_args()

    os.environ["BT_NO_CACHE"] = "1"  # 离线确定性：强制 SQLite 直读
    import gc
    gc.disable()  # 计时段关 GC
    from app import config as C
    from app import engine as eng
    C.DB_FILE = SNAP_DB
    names = {c: c for c in CODES}

    # ---- 1) load_elapsed：构造 → 手动 _load_data 计时 ----
    bt0 = eng.Backtest(CODES, names, START, END, 100000.0, "score", dict(PARAMS))
    t0 = time.perf_counter()
    bt0._load_data()
    load_s = time.perf_counter() - t0
    td = list(bt0.trading_days)
    mid_dates = [td[int(len(td) * f)] for f in (0.25, 0.375, 0.5, 0.625, 0.75)]

    # ---- 2) signals_on 单日均耗时（复用已加载实例）----
    sigs = []
    for d in mid_dates:
        t1 = time.perf_counter()
        bt0._signals_on(d)
        sigs.append((time.perf_counter() - t1) * 1000)
    sig_ms = statistics.median(sigs)

    # ---- 3) 总耗时（新实例 run() 3 次取中位数，含加载；BT_NO_CACHE=1 直读）----
    totals = []
    _rets = []
    for _ in range(3):
        bt1 = eng.Backtest(CODES, names, START, END, 100000.0, "score", dict(PARAMS))
        t2 = time.perf_counter()
        r = bt1.run()
        totals.append(time.perf_counter() - t2)
        _rets.append((r.get("total_return"), r.get("trade_count")))
    total_s = statistics.median(totals)
    _tr, _trades = _rets[2]

    # ---- 4) 峰值内存：tracemalloc 单独跑近1年窗 ----
    tracemalloc.start()
    try:
        bt2 = eng.Backtest(CODES, names, MEM_START, MEM_END, 100000.0, "score", dict(PARAMS))
        bt2.run()
        _cur, _peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    peak_mb = round(_peak / 1024 / 1024, 1)

    result = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "codes": CODES, "window": "%s~%s" % (START, END), "strategy": "score",
        "total_elapsed_s": round(total_s, 3),
        "load_elapsed_s": round(load_s, 3),
        "signals_on_avg_ms": round(sig_ms, 4),
        "peak_memory_mb": peak_mb,
        "note": "快照库 data/snapshots/2026-09-11/market.db（只读）; BT_NO_CACHE=1 直读; "
                "signals_on 为窗内 5 交易日均值; 内存为近1年窗 tracemalloc 峰值",
    }
    print("[H4-bt] total=%.3fs(3x中位) load=%.3fs signals_on=%.4fms peak_mem=%.1fMB  tr=%.4f trades=%d" % (
        total_s, load_s, sig_ms, peak_mb, _tr, _trades))

    base = {}
    if os.path.exists(BASELINE):
        try:
            with open(BASELINE, encoding="utf-8") as f:
                base = json.load(f)
        except Exception:
            base = {}
    cur = {"total_elapsed_s": total_s, "load_elapsed_s": load_s,
           "signals_on_avg_ms": sig_ms, "peak_memory_mb": peak_mb}
    failed = []
    if base.get("backtest"):
        b = base["backtest"]
        for key in cur:
            if key in b and cur[key] > b[key] * (1 + DEGRADE):
                failed.append("%s: 基线 %.4f → 当前 %.4f（+%.1f%%）" % (
                    key, b[key], cur[key], (cur[key] / b[key] - 1) * 100))
    if failed:
        print("[H4-bt] ❌ 退化项 %d:" % len(failed))
        for f in failed:
            print("   ", f)
    else:
        print("[H4-bt] ✅ 无退化（阈值 +%.0f%%）" % (DEGRADE * 100))

    # ---- 写/刷新基线（仅首次或显式 --update-baseline；平时只比对不写）----
    if not os.path.exists(BASELINE):
        with open(BASELINE, "w", encoding="utf-8") as f:
            json.dump({"schema": "v1",
                       "comment": "H4 性能基线：score 段由 bench_score.py 维护，backtest 段由 "
                                  "bench_backtest.py 维护；退化阈值 +20%；离线快照直读基准；"
                                  "降噪：GC 关闭 + BLAS 单线程 + total 3 次中位数",
                       "generated_at": result["generated_at"], "backtest": cur}, f,
                      ensure_ascii=False, indent=1)
        print("[H4-bt] 首次运行：已写入基线 %s" % BASELINE)
    elif base.get("backtest") and a.update_baseline:
        with open(BASELINE, "w", encoding="utf-8") as f:
            json.dump({"schema": "v1", "comment": base.get("comment", ""),
                       "generated_at": result["generated_at"],
                       "score": base.get("score"), "backtest": cur}, f,
                      ensure_ascii=False, indent=1)
        print("[H4-bt] 基线已显式刷新（--update-baseline）")
    elif not base.get("backtest"):
        with open(BASELINE, "w", encoding="utf-8") as f:
            json.dump({"schema": "v1", "comment": base.get("comment", ""),
                       "generated_at": result["generated_at"],
                       "score": base.get("score"), "backtest": cur}, f,
                      ensure_ascii=False, indent=1)
        print("[H4-bt] 基线缺少 backtest 段：已写入")

    if failed and not a.no_fail:
        print("[H4-bt] EXIT=1（性能退化）")
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
