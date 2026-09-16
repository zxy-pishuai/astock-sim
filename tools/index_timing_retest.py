# -*- coding: utf-8 -*-
"""★ Phase43：指数择时复验 —— 旧结论（phase2_index_timing.md）产生于 P20/P21 数据
修复之前，对现行基线无效；情绪闸门已在 P22 按干净数据复验（判无效），本阶段补指数择时。

网格：
  Part1  index_timing ∈ {off, on} × {score, board} × 4 窗口
         —— off 臂复用 data/bt_dd_gate.json 已两次验证的基线（不重跑）
  Part2  空头乘数 INDEX_TIMING_MULT_DOWN ∈ {0.6, 0.7}（对照现行 0.8 即 Part1 on 臂），
         仅 score × 4 窗口；worker 内 monkeypatch config 常量（Phase25 先例）
配方与 Phase25/27 一致：bt_pool top500、fetch_quotes 置空、score 25/3/0.30/0.001、
board 40/2/0.25/0.001。

记录：收益、最大回撤、Calmar、乘数生效天数占比（独立逐日调用
index_timing.timing_multiplier(as_of=prev_date)，统计 mult<1 的交易日占比，
与运行参数一致）、分窗口明细。

判定规则（事先写死）：某配置 ≥3/4 窗口收益改善或持平（Δ≥-1pp），且牛市窗口
恶化 ≤2pp，且非牛窗口至少 1 个改善 ≥3pp → 建议开启并写明参数。
★不改 config（写权限归并行任务/验收方）；不满足 → 维持关闭，与 P22 结论并列归档。

用法: python tools/index_timing_retest.py [--workers 10]
输出: data/bt_index_timing.json
"""
import argparse
import json
import os
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
STRAT_PARAMS = {
    "score": {"buy_threshold": 25, "max_positions": 3, "position_pct": 0.30,
              "slippage": 0.001},
    "board": {"buy_threshold": 40, "max_positions": 2, "position_pct": 0.25,
              "slippage": 0.001},
}
MULT_DOWN_GRID = [0.6, 0.7]
OUT_JSON = os.path.join(BASE, "data", "bt_index_timing.json")


def _worker(spec):
    strat, mult_down, w0, w1, codes, names = spec
    t0 = time.time()
    from app import config as C
    from app import engine as eng
    from app import index_timing as it
    orig_q = eng.df.fetch_quotes
    orig_md = getattr(C, "INDEX_TIMING_MULT_DOWN")
    eng.df.fetch_quotes = lambda cs: {}
    if mult_down is not None:
        C.INDEX_TIMING_MULT_DOWN = float(mult_down)
    try:
        p = dict(STRAT_PARAMS[strat])
        p["index_timing"] = True
        bt = eng.Backtest(codes, names, w0, w1, 100000.0, strat, p)
        r = bt.run()
        # 独立统计乘数生效天数占比（与运行参数一致；prev_date 口径同引擎）
        active_days = 0
        checked = 0
        for di in range(1, len(bt.trading_days)):
            m, _r = it.timing_multiplier(as_of=bt.trading_days[di - 1])
            checked += 1
            if m < 1.0:
                active_days += 1
    finally:
        C.INDEX_TIMING_MULT_DOWN = orig_md
        eng.df.fetch_quotes = orig_q
    if "error" in r:
        return {"error": r["error"], "strategy": strat, "mult_down": mult_down,
                "window": [w0, w1]}
    mdd = r.get("max_drawdown") or 0.0
    ann = r.get("annual_return") or 0.0
    row = {
        "strategy": strat, "mult_down": mult_down, "window": [w0, w1],
        "total_return": r.get("total_return"),
        "max_drawdown": mdd,
        "calmar": round(ann / abs(mdd), 3) if mdd < 0 else None,
        "sharpe": r.get("sharpe"),
        "trade_count": r.get("trade_count"),
        "mult_active_day_ratio": round(active_days / max(1, checked), 4),
        "elapsed_s": round(time.time() - t0),
    }
    print("  [%s MD=%s] %s~%s ret=%+.4f calmar=%s trades=%d 生效占比=%.1f%% (%ds)" % (
        strat, mult_down or "-", w0[:7], w1[:7], row["total_return"] or 0,
        row["calmar"], row["trade_count"], row["mult_active_day_ratio"] * 100,
        row["elapsed_s"]), flush=True)
    return row


def judge(base_rows, rows, label):
    """判定规则（事先写死）。返回 dict"""
    deltas = [rows[i]["total_return"] - base_rows[i]["total_return"]
              for i in range(4)]
    n_ok = sum(1 for d in deltas if d >= -0.01)
    bull_d = deltas[3]
    nonbull_best = max(deltas[0], deltas[1], deltas[2])
    passed = n_ok >= 3 and bull_d <= 0.02 and nonbull_best >= 0.03
    return {
        "label": label,
        "deltas_pp": [round(d * 100, 2) for d in deltas],
        "improved_or_flat_windows": n_ok,
        "bull_delta_pp": round(bull_d * 100, 2),
        "nonbull_best_improve_pp": round(nonbull_best * 100, 2),
        "pass": passed,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=10)
    a = ap.parse_args()
    t0 = time.time()
    from tools.backtest_zt_eco import build_pool
    codes, names = build_pool()
    print("池: %d 只" % len(codes), flush=True)

    # off 臂复用 dd_gate 基线（已两次对齐验证）
    dd = json.load(open(os.path.join(BASE, "data", "bt_dd_gate.json"),
                        encoding="utf-8"))
    assert dd["baseline_aligned"], "dd_gate 基线未对齐，禁止复用"
    base_rows = {s: sorted(dd["baseline"][s], key=lambda x: x["window"][0])
                 for s in ("score", "board")}
    print("off 基线(复用): score %s / board %s" % (
        [r["total_return"] for r in base_rows["score"]],
        [r["total_return"] for r in base_rows["board"]]), flush=True)

    specs = []
    for strat in ("score", "board"):
        for w0, w1 in WINDOWS:
            specs.append((strat, None, w0, w1, codes, names))       # on，MD=0.8 现行
    for md in MULT_DOWN_GRID:
        for w0, w1 in WINDOWS:
            specs.append(("score", md, w0, w1, codes, names))
    results = []
    with Pool(processes=a.workers) as pool:
        for row in pool.imap_unordered(_worker, specs):
            results.append(row)

    def four(strat, md):
        rs = sorted([r for r in results if "error" not in r
                     and r["strategy"] == strat and r["mult_down"] == md],
                    key=lambda x: x["window"][0])
        assert len(rs) == 4, (strat, md, len(rs))
        return rs

    arms = []
    # Part1 on 臂
    for strat in ("score", "board"):
        rows = four(strat, None)
        j = judge([dict(total_return=r["total_return"]) for r in base_rows[strat]],
                  rows, "%s index_timing on(MD=0.8)" % strat)
        arms.append({"rows": rows, **j})
    # Part2 空头乘数敏感性（score）
    for md in MULT_DOWN_GRID:
        rows = four("score", md)
        j = judge([dict(total_return=r["total_return"]) for r in base_rows["score"]],
                  rows, "score index_timing on(MD=%.1f)" % md)
        arms.append({"rows": rows, **j})

    winners = [x for x in arms if x["pass"]]
    best = max(winners, key=lambda x: sum(x["deltas_pp"])) if winners else None
    verdict = {
        "rule": ">=3/4 窗口 Δ>=-1pp 且 牛市窗口恶化<=2pp 且 非牛至少 1 窗改善>=3pp；"
                "★只建议不改 config",
        "recommended": ({"strategy": best["label"], "params": {
            "INDEX_TIMING_ENABLED": True}} if best else None),
        "enabled": bool(best),
        "reason": (best["label"] + " 达标 → 建议开启（参数详见 JSON/报告）" if best
                   else "无配置达标 → 指数择时维持关闭（与 P22 情绪闸门结论并列归档）"),
        "arms_summary": [{k: v for k, v in x.items() if k != "rows"} for x in arms],
    }
    payload = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "phase": "43",
        "windows": WINDOWS,
        "params": STRAT_PARAMS,
        "mult_down_grid": MULT_DOWN_GRID,
        "baseline_source": "data/bt_dd_gate.json（已两次对齐验证）",
        "baseline": {s: [{k: r[k] for k in ("window", "total_return", "trade_count")}
                         for r in base_rows[s]] for s in ("score", "board")},
        "arms": arms,
        "verdict": verdict,
        "note_config": "本阶段零代码修改；建议参数仅供验收方统一落配置",
    }
    with open(OUT_JSON, "w", encoding="utf-8", newline="\n") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    print("\n各臂汇总:", flush=True)
    for s in verdict["arms_summary"]:
        print(" ", json.dumps(s, ensure_ascii=False), flush=True)
    print("\n判定:", verdict["reason"], flush=True)
    print("已写出 %s | 总耗时 %.0fs" % (OUT_JSON, time.time() - t0), flush=True)


if __name__ == "__main__":
    main()
