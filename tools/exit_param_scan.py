# -*- coding: utf-8 -*-
"""★ Phase36：退出参数一维扫描 —— 处理最大亏损桶（"止损"，720 笔净亏 -107 万）

证据：Phase30 归因 by_exit.json——止损后 5 日均值 +0.27%、中位 -0.60%（轻微卖飞）；
STOP_LOSS_PCT=-0.05 / TIME_STOP_DAYS=3 从未做过敏感性验证。

方法：
- 引擎在调用点直接读 C.STOP_LOSS_PCT / C.TIME_STOP_DAYS → worker 进程内
  monkeypatch app.config 属性（Phase25 fetch_quotes 置空的既定先例），不改任何源文件。
- 一维扫描（其余参数冻结基线）：
  A 组 STOP_LOSS_PCT ∈ {-0.04, -0.06, -0.07}，TIME=3
  B 组 TIME_STOP_DAYS ∈ {2, 4}，STOP=-0.05
  基线档（-0.05/3）也实跑一遍：提供触发计数基准，并二次验证与
  data/bt_dd_gate.json 已对齐基线一致（收益/笔数必须逐位相同）。
- 配方与 Phase25/27 一致：bt_pool top500、fetch_quotes 置空、score 25/3/0.30/0.001。
- 记录：收益、最大回撤、止损触发次数、时间止损触发次数、
  ★止损后 5 日走势（该股收盘相对止损成交价；复用归因口径的卖飞成本）。

判定规则（事先写死）：推荐某参数须同时满足——
  ≥3/4 窗口收益改善或持平（Δ≥-1pp）；牛市窗口恶化 ≤2pp；
  触发次数方向自洽（相对基线：收益改善时止损总触发不得增加 >10%）。
★本阶段不改 config：推荐值只写报告，由验收方统一落配置。无达标 → 如实写"现行参数维持"。

用法: python tools/exit_param_scan.py [--workers 10]
输出: data/bt_exit_scan.json
"""
import argparse
import json
import os
import sqlite3
import statistics
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

WINDOWS = [
    ["2019-01-01", "2020-12-31"],
    ["2021-01-01", "2022-12-31"],
    ["2023-01-01", "2024-12-31"],
    ["2025-08-18", "2026-08-18"],
]
SCORE_PARAMS = {"buy_threshold": 25, "max_positions": 3,
                "position_pct": 0.30, "slippage": 0.001}
VARIANTS = [  # (stop, time_stop, group)
    (-0.04, 3, "A"), (-0.06, 3, "A"), (-0.07, 3, "A"),
    (-0.05, 2, "B"), (-0.05, 4, "B"),
    (-0.05, 3, "baseline"),
]
OUT_JSON = os.path.join(BASE, "data", "bt_exit_scan.json")

_CLOSE_CACHE = {}


def daily_closes(code):
    """code -> {date: close}（2019 起全量，供止损后 5 日走势）"""
    if code not in _CLOSE_CACHE:
        conn = sqlite3.connect("file:%s?mode=ro" % os.path.join(
            BASE, "data", "market.db"), uri=True, timeout=15)
        m = {d: cl for d, cl in conn.execute(
            "SELECT date, close FROM kline WHERE period='day' AND code=? "
            "AND close>0 AND date>='2019-01-01'", (code,))}
        conn.close()
        _CLOSE_CACHE[code] = m
    return _CLOSE_CACHE[code]


def _worker(spec):
    stop, tstop, group, w0, w1, codes, names = spec
    t0 = time.time()
    from app import config as C
    from app import engine as eng
    orig_q = eng.df.fetch_quotes
    orig_s = getattr(C, "STOP_LOSS_PCT")
    orig_t = getattr(C, "TIME_STOP_DAYS")
    eng.df.fetch_quotes = lambda cs: {}
    C.STOP_LOSS_PCT = stop
    C.TIME_STOP_DAYS = tstop
    try:
        bt = eng.Backtest(codes, names, w0, w1, 100000.0, "score",
                          dict(SCORE_PARAMS))
        r = bt.run()
        trades = list(bt.trades)
        days = bt.trading_days
        days_idx = {d: i for i, d in enumerate(days)}
    finally:
        C.STOP_LOSS_PCT = orig_s
        C.TIME_STOP_DAYS = orig_t
        eng.df.fetch_quotes = orig_q
    if "error" in r:
        return {"error": r["error"], "stop": stop, "time_stop": tstop,
                "group": group, "window": [w0, w1]}
    n_stop = n_time = 0
    fwd5 = []
    for t in trades:
        if t["side"] != "sell" or not t.get("reason"):
            continue
        reason = t["reason"]
        is_sl = reason.startswith("止损")
        is_ts = reason.startswith("时间止损")
        n_stop += is_sl
        n_time += is_ts
        if is_sl:
            cl = daily_closes(t["code"])
            if t["date"] in days_idx:
                j = days_idx[t["date"]] + 5
                if j < len(days):
                    c5 = cl.get(days[j])
                    if c5 and t["price"] > 0:
                        fwd5.append(c5 / t["price"] - 1.0)
    row = {
        "stop": stop, "time_stop": tstop, "group": group, "window": [w0, w1],
        "total_return": r.get("total_return"),
        "max_drawdown": r.get("max_drawdown"),
        "sharpe": r.get("sharpe"),
        "trade_count": r.get("trade_count"),
        "stop_loss_count": n_stop,
        "time_stop_count": n_time,
        "sl_fwd5_mean_pct": round(statistics.mean(fwd5) * 100, 3) if fwd5 else None,
        "sl_fwd5_med_pct": round(statistics.median(fwd5) * 100, 3) if fwd5 else None,
        "sl_fwd5_n": len(fwd5),
        "elapsed_s": round(time.time() - t0),
    }
    print("  [%s S=%.2f/T=%d] %s~%s ret=%+.4f trades=%d 止损=%d 时停=%d 止损后5日=%s%% (%ds)" % (
        group, stop, tstop, w0[:7], w1[:7], row["total_return"] or 0,
        row["trade_count"], n_stop, n_time, row["sl_fwd5_mean_pct"],
        row["elapsed_s"]), flush=True)
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=10)
    a = ap.parse_args()
    t0 = time.time()
    from tools.backtest_zt_eco import build_pool
    codes, names = build_pool()
    print("池: %d 只" % len(codes), flush=True)

    # dd_gate 基线（收益/笔数对齐参照）
    dd = json.load(open(os.path.join(BASE, "data", "bt_dd_gate.json"),
                        encoding="utf-8"))
    assert dd["baseline_aligned"], "dd_gate 基线未对齐"
    ref = {tuple(r["window"]): r for r in dd["baseline"]["score"]}

    specs = [(s, t, g, w0, w1, codes, names)
             for (s, t, g) in VARIANTS for (w0, w1) in WINDOWS]
    print("运行组数: %d（含基线档实跑）" % len(specs), flush=True)
    results = []
    with Pool(processes=a.workers) as pool:
        for row in pool.imap_unordered(_worker, specs):
            results.append(row)

    def four(stop, tstop):
        rs = sorted([r for r in results if "error" not in r
                     and r["stop"] == stop and r["time_stop"] == tstop],
                    key=lambda x: x["window"][0])
        assert len(rs) == 4, (stop, tstop, len(rs))
        return rs

    # 基线档：与 dd_gate 参照二次对齐校验 + 触发计数基准
    base_rows = four(-0.05, 3)
    align_ok = all(
        abs(base_rows[i]["total_return"]
            - ref[tuple(WINDOWS[i])]["total_return"]) <= 5e-5
        and base_rows[i]["trade_count"] == ref[tuple(WINDOWS[i])]["trade_count"]
        for i in range(4))
    print("基线二次对齐(实测 vs dd_gate):", align_ok, flush=True)
    base_sl_total = sum(r["stop_loss_count"] for r in base_rows)
    base_ts_total = sum(r["time_stop_count"] for r in base_rows)

    variants = []
    for stop, tstop, group in VARIANTS:
        if group == "baseline":
            continue
        rs = four(stop, tstop)
        deltas = [rs[i]["total_return"] - base_rows[i]["total_return"]
                  for i in range(4)]
        # ★ Phase70：判定委托 judge_kit（deltas 为小数，库内统一换算 pp）
        _tdir = os.path.dirname(os.path.abspath(__file__))
        if _tdir not in sys.path:
            sys.path.insert(0, _tdir)
        import judge_kit as jk
        n_ok = jk.window_pass([d * 100 for d in deltas],
                              tol_loss_pp=1.0, min_windows=3)[0]
        bull_d = deltas[3]
        sl_total = sum(r["stop_loss_count"] for r in rs)
        ts_total = sum(r["time_stop_count"] for r in rs)
        sl_ratio = sl_total / base_sl_total if base_sl_total else None
        # 自洽：收益改善（总Δ>0）时，止损触发不得比基线增加 >10%
        consistent = True
        if sum(deltas) > 0 and sl_ratio is not None:
            consistent = sl_ratio <= 1.10
        # ★ bull_ok 的入参是"损失"(正=更差)；bull_d 是 Δ 收益(正=更好)，取负传入
        passed = n_ok >= 3 and jk.bull_ok(-bull_d * 100, max_loss_pp=2.0) and consistent
        variants.append({
            "group": group, "stop": stop, "time_stop": tstop,
            "rows": [{k: v for k, v in r.items() if k != "group"} for r in rs],
            "deltas_pp": [round(d * 100, 2) for d in deltas],
            "improved_or_flat_windows": n_ok,
            "bull_delta_pp": round(bull_d * 100, 2),
            "avg_delta_pp": round(sum(deltas) / 4 * 100, 2),
            "stop_loss_total": sl_total,
            "time_stop_total": ts_total,
            "stop_ratio_vs_baseline": round(sl_ratio, 3) if sl_ratio is not None else None,
            "consistent_triggers": consistent,
            "pass": passed,
        })
        print("变体 S=%.2f/T=%d: Δ=%s 达标窗=%d 牛市Δ=%.2fpp 止损=%d(基线%d) pass=%s" % (
            stop, tstop, variants[-1]["deltas_pp"], n_ok,
            bull_d * 100, sl_total, base_sl_total, passed), flush=True)

    winners = [v for v in variants if v["pass"]]
    best = max(winners, key=lambda v: sum(v["deltas_pp"])) if winners else None
    verdict = {
        "rule": ">=3/4 窗口 Δ>=-1pp 且 牛市窗口 Δ>=-2pp 且 触发方向自洽；"
                "达标者中总Δ最高为推荐值。★只推荐不改配置",
        "recommended": ({"STOP_LOSS_PCT": best["stop"],
                         "TIME_STOP_DAYS": best["time_stop"]} if best else None),
        "enabled": bool(best),
        "reason": ("推荐 STOP_LOSS_PCT=%g / TIME_STOP_DAYS=%d（由验收方统一落配置）" % (
            best["stop"], best["time_stop"]) if best else
            "无变体达标 → 现行参数维持（STOP_LOSS_PCT=-0.05 / TIME_STOP_DAYS=3）"),
        "baseline_recheck_aligned": align_ok,
    }
    payload = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "phase": "36",
        "windows": WINDOWS,
        "grid": {"A_stop": [-0.04, -0.06, -0.07], "B_time": [2, 4],
                 "baseline": {"stop": -0.05, "time_stop": 3}},
        "params": SCORE_PARAMS,
        "baseline_rows": base_rows,
        "dd_gate_reference_aligned": align_ok,
        "variants": variants,
        "verdict": verdict,
        "note_config": "本阶段不改 config.py；推荐值仅供验收方统一落配置",
    }
    with open(OUT_JSON, "w", encoding="utf-8", newline="\n") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    print("\n判定:", verdict["reason"], flush=True)
    print("已写出 %s | 总耗时 %.0fs" % (OUT_JSON, time.time() - t0), flush=True)


if __name__ == "__main__":
    main()
