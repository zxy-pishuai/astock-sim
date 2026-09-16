# -*- coding: utf-8 -*-
"""★ Phase26：涨停生态温度计闸门回测调度（board 策略，独立进程，不走服务 HTTP）

口径对齐 Phase21/22/25（与 bt_clean_results.json 基线精确复现的配方一致）：
  - 池 = data/bt_pool.json 前 500 只（2026-08-21 成交额降序；DATA_EXCLUDE_CODES 由
    engine 内部过滤），names 用代码兜底
  - board 参数镜像实盘打板配置：buy_threshold=40(BOARD_SCORE_THRESHOLD)、
    max_positions=2(BOARD_MAX_POSITIONS)、position_pct=0.25(BOARD_POSITION_RATIO)、
    slippage=0.001；capital=100000
  - fetch_quotes 置空：实时流通市值快照不可复现，置空走"成交额×20 近似"，与基线口径一致

流程：
  1) 复现基线（zt_eco_gate=False）：目标 -11.77 / -6.65 / -1.66 / +27.30%（bt_clean_results.json），
     不一致则报错退出（先修对齐再继续）
  2) 阈值网格：历史温度 20%/30%/40%/50% 分位 × 4 窗口 = 16 组（stop 模式）
  3) 按【事先写死】的判定规则输出 verdict（诚实执行，含负结果）

判定规则（本脚本运行前已确定，禁止事后修改）：
  C1: 某阈值 T 使 ≥3/4 窗口 Δ(相对基线) ≥ -1pp（改善或持平）
  C2: 该 T 下近一年牛市窗口 Δ ≥ -2pp
  C3: 敏感性形状——4 档阈值的"4 窗口平均 Δ"按温度升序为 S，候选档 i 须
      非孤立尖峰：S[i] ≥ min(邻档) - 0.5pp 且 S[i] ≤ max(邻档) + 1.5pp（边界查单侧）
  候选选取：先按满足 C1 的阈值里取平均 Δ 最高者，再验 C2/C3。
  全部满足 → 建议启用（config 默认阈值）；任一不满足 → 维持禁用并如实记录。

用法: python tools/backtest_zt_eco.py [--skip-baseline-check]
输出: data/bt_zt_eco.json
"""
import argparse
import io
import json
import os
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)
sys.stdout = sys.stdout if hasattr(sys.stdout, "buffer") else sys.stdout
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from app import config as C
from app import engine as eng
from app import zt_ecosystem as ze

WINDOWS = [
    ["2019-01-01", "2020-12-31"],
    ["2021-01-01", "2022-12-31"],
    ["2023-01-01", "2024-12-31"],
    ["2025-08-18", "2026-08-18"],   # 近一年（牛市窗口，C2 用第 4 窗）
]
BASELINE_TARGET = [-0.1177, -0.0665, -0.0166, 0.2730]   # bt_clean_results.json board
POOL_DATE = "2026-08-21"
POOL_SIZE = 500
# 基线精确复现配方（Phase25 diag_align 验证）：board 参数镜像实盘打板配置
BOARD_PARAMS = {"buy_threshold": 40, "max_positions": 2,
                "position_pct": 0.25, "slippage": 0.001}
OUT_JSON = os.path.join(BASE, "data", "bt_zt_eco.json")


def build_pool():
    """基线口径：bt_pool.json 前 500（engine 内部滤 DATA_EXCLUDE_CODES），names 代码兜底"""
    with open(os.path.join(C.DATA_DIR, "bt_pool.json"), encoding="utf-8") as f:
        pool = json.load(f)
    codes = list(pool["codes"])[:POOL_SIZE]
    return codes, {c: c for c in codes}


def run_one(codes, names, w0, w1, params, tag):
    t0 = time.time()
    # 基线对齐：fetch_quotes 置空（实时市值快照不可复现 → 成交额×20 近似，确定性）
    orig_quotes = eng.df.fetch_quotes
    eng.df.fetch_quotes = lambda cs: {}
    try:
        bt = eng.Backtest(codes, names, w0, w1, 100000.0, "board",
                          dict(BOARD_PARAMS) | dict(params or {}))
        r = bt.run()
    finally:
        eng.df.fetch_quotes = orig_quotes
    if "error" in r:
        return {"tag": tag, "error": r["error"]}
    row = {
        "tag": tag,
        "window": [w0, w1],
        "total_return": r.get("total_return"),
        "annual_return": r.get("annual_return"),
        "max_drawdown": r.get("max_drawdown"),
        "sharpe": r.get("sharpe"),
        "win_rate": r.get("win_rate"),
        "trade_count": r.get("trade_count"),
        "buy_count": r.get("buy_count"),
        "flat_day_ratio": r.get("flat_day_ratio"),
        "zt_gate_blocked_days": r.get("zt_gate_blocked_days"),
        "elapsed": round(time.time() - t0, 1),
    }
    print("  [%s] %s~%s ret=%.4f dd=%.4f trades=%s blocked=%s (%.0fs)" % (
        tag, w0, w1, row["total_return"] or 0, row["max_drawdown"] or 0,
        row["trade_count"], row["zt_gate_blocked_days"], row["elapsed"]), flush=True)
    return row


# ---------- 判定规则（事先写死） ----------
FLAT_TOL = -0.01          # C1: Δ ≥ -1pp 记"改善或持平"
BULL_TOL = -0.02          # C2: 牛市窗口 Δ ≥ -2pp
SPIKE_FLOOR = -0.005      # C3: 平均Δ不得低于邻档最小值 0.5pp 以上
SPIKE_CAP = 0.015         # C3: 高出邻档最大值不得超过 1.5pp


def judge(baseline_rets, grid):
    """grid: [{threshold, rets:[4]} ...]（rets 与 WINDOWS 对齐）。返回 verdict dict"""
    deltas = []
    for g in grid:
        d = [(g["rets"][i] - baseline_rets[i]) for i in range(4)]
        deltas.append({"threshold": g["threshold"], "deltas": d,
                       "avg_delta": sum(d) / 4.0})
    # 候选 = 满足 C1 中平均 Δ 最高者
    c1_ok = [d for d in deltas
             if sum(1 for x in d["deltas"] if x >= FLAT_TOL) >= 3]
    verdict = {"rule": {"C1_min_improved": ">=3/4 且 Δ>=-1pp",
                        "C2_bull_delta": ">=-2pp",
                        "C3_shape": "非孤立尖峰(S[i]>=min(nb)-0.5pp 且 <=max(nb)+1.5pp)",
                        "candidate_pick": "C1 达标者中 avgΔ 最高"},
               "per_threshold": [{**d, "c1": sum(1 for x in d["deltas"] if x >= FLAT_TOL)}
                                 for d in deltas]}
    if not c1_ok:
        verdict.update({"enabled": False,
                        "reason": "无阈值满足 C1（≥3/4 窗口改善或持平）"})
        return verdict
    cand = max(c1_ok, key=lambda d: d["avg_delta"])
    idx = [d["threshold"] for d in deltas].index(cand["threshold"])
    bull_d = cand["deltas"][3]
    c2 = bull_d >= BULL_TOL
    # C3 形状检查
    s = [d["avg_delta"] for d in deltas]   # 已按阈值升序（分位升序=温度升序）
    nbs = [s[j] for j in (idx - 1, idx + 1) if 0 <= j < len(s)]
    c3 = bool(nbs) and (s[idx] >= min(nbs) - SPIKE_FLOOR) and (s[idx] <= max(nbs) + SPIKE_CAP)
    enabled = c2 and c3
    reasons = []
    reasons.append("C1 ✅（%d/4 窗口改善或持平）" % sum(1 for x in cand["deltas"] if x >= FLAT_TOL))
    reasons.append("C2 %s（牛市窗口 Δ=%+.2fpp）" % ("✅" if c2 else "❌", bull_d * 100))
    reasons.append("C3 %s（avgΔ 序列=%s，候选=%.2f%%，邻档=%s）" % (
        "✅" if c3 else "❌", ["%.2f" % (x * 100) for x in s],
        s[idx] * 100, ["%.2f" % (x * 100) for x in nbs]))
    verdict.update({"candidate_threshold": cand["threshold"],
                    "candidate_deltas": cand["deltas"],
                    "C2_pass": c2, "C3_pass": c3,
                    "enabled": enabled, "reason": "；".join(reasons)})
    return verdict


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-baseline-check", action="store_true")
    a = ap.parse_args()
    t0 = time.time()
    eco = ze.build()   # 缓存命中则秒回
    thresholds = {k: v for k, v in eco["thresholds"].items()}
    qs_sorted = sorted(thresholds.keys(), key=float)
    thr_values = [thresholds[k] for k in qs_sorted]
    print("温度分位阈值:", thresholds, flush=True)

    codes, names = build_pool()
    print("池: %d 只（%s 成交额 top%d）" % (len(codes), POOL_DATE, POOL_SIZE), flush=True)

    # ---- 1. 基线复现 ----
    print("== 基线复现（zt_eco_gate 关闭）==", flush=True)
    base_rows = []
    for w0, w1 in WINDOWS:
        base_rows.append(run_one(codes, names, w0, w1,
                                 {"zt_eco_gate": False}, "baseline"))
    base_rets = [r.get("total_return") for r in base_rows]
    mismatch = [(WINDOWS[i], base_rets[i], BASELINE_TARGET[i])
                for i in range(4)
                if base_rets[i] is None or abs(base_rets[i] - BASELINE_TARGET[i]) > 0.0005]
    baseline_ok = not mismatch
    if not baseline_ok and not a.skip_baseline_check:
        print("❌ 基线不一致，先修对齐再继续:", mismatch, flush=True)
        json.dump({"baseline_ok": False, "mismatch": mismatch,
                   "baseline": base_rows},
                  open(OUT_JSON, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        return {"baseline_ok": False}

    # ---- 2. 阈值网格 4×4（stop 模式）----
    print("== 阈值网格（stop 模式）==", flush=True)
    grid = []
    for q, tv in zip(qs_sorted, thr_values):
        rows = []
        for w0, w1 in WINDOWS:
            rows.append(run_one(codes, names, w0, w1,
                                {"zt_eco_gate": {"threshold": tv, "mode": "stop"}},
                                "q%s_T%.1f" % (q, tv)))
        grid.append({"quantile": float(q), "threshold": tv, "rows": rows,
                     "rets": [r.get("total_return") for r in rows]})
        # 每完成一档落盘一次（防中断丢结果）
        _dump(base_rows, base_rets, baseline_ok, grid, eco, thresholds, None)
        print("  档 %s(T=%.1f) 完成，累计 %.0fs" % (q, tv, time.time() - t0), flush=True)

    # ---- 3. 判定 ----
    verdict = judge([-99 if v is None else v for v in base_rets], grid)
    print("判定:", verdict.get("enabled"), verdict.get("reason"), flush=True)

    out = _dump(base_rows, base_rets, baseline_ok, grid, eco, thresholds, verdict)
    print("已写出 %s（总耗时 %.0fs）" % (OUT_JSON, time.time() - t0), flush=True)
    return out


def _dump(base_rows, base_rets, baseline_ok, grid, eco, thresholds, verdict):
    """结果落盘（含温度序列快照：月末采样 + 低温度日统计 + 阶段对照统计）"""
    series = eco["series"]
    # 月末快照（每月最后一个交易日）
    month_end = {}
    for s in series:
        month_end[s["date"][:7]] = s
    snapshot = [month_end[k] for k in sorted(month_end.keys())]
    # 低温度日（< 最低档阈值）按年分布
    lo_thr = min(thresholds.values())
    low_by_year = {}
    for s in series:
        t = s.get("temperature")
        if t is not None and t < lo_thr:
            low_by_year[s["date"][:4]] = low_by_year.get(s["date"][:4], 0) + 1
    # 与旧阶段表对照（qg_zt_full）
    cross = _cross_with_phases(series)
    payload = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "phase": "26",
        "strategy": "board",
        "pool": {"source": "data/bt_pool.json[:500]", "date": POOL_DATE,
                 "size": POOL_SIZE,
                 "rule": "成交额top500(bt_pool.json), engine内滤DATA_EXCLUDE_CODES, names=code"},
        "board_params": BOARD_PARAMS,
        "quotes_patch": "fetch_quotes置空(市值用成交额×20近似, 与基线口径一致)",
        "windows": WINDOWS,
        "eco_meta": {"weights": eco["meta"]["weights"],
                     "factor_ic": eco["meta"]["factor_ic"],
                     "ic_target": eco["meta"]["ic_target"],
                     "weight_rule": eco["meta"]["weight_rule"],
                     "thresholds": thresholds},
        "baseline_ok": baseline_ok,
        "baseline": base_rows,
        "baseline_target": BASELINE_TARGET,
        "grid": grid,
        "verdict": verdict,
        "temperature_snapshot_monthly": snapshot,
        "low_temp_days_by_year_below_min_threshold": {
            "below": lo_thr, "by_year": low_by_year},
        "cross_check_with_qg_zt_full": cross,
    }
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    return payload


def _cross_with_phases(series):
    """新温度 vs qg_zt_full 阶段表：退潮识别天数对比 + 相位叠加统计"""
    import sqlite3
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % C.DB_FILE, uri=True, timeout=10)
        ph = dict(conn.execute("SELECT date, phase FROM qg_zt_full").fetchall())
        conn.close()
    except Exception:
        return {"error": "qg_zt_full 不可读"}
    temps = {s["date"]: s.get("temperature") for s in series}
    common = [d for d in sorted(set(ph) & set(temps)) if temps[d] is not None]
    th = ze.quantile_thresholds_from(series)
    q20 = min(th.values())
    q50 = th[max(th, key=float)]
    from collections import Counter
    phase_cnt = Counter(ph[d] for d in common)
    low_in_phase = Counter(ph[d] for d in common if temps[d] < q20)
    mat = Counter((ph[d], "低" if temps[d] < q20 else "高") for d in common)
    return {
        "common_days": len(common),
        "old_phase_counts": dict(phase_cnt),
        "new_below_q20_days": int(sum(low_in_phase.values())),
        "new_below_q20_by_phase": dict(low_in_phase),
        "matrix_phase_x_temp": {"%s|%s" % k: v for k, v in sorted(mat.items())},
        "note": "旧表'退潮'全历史 %d 天；新温度计低于 P20 分位共 %d 天（分布见 by_year/matrix）"
                % (phase_cnt.get("退潮", 0), sum(low_in_phase.values())),
    }


if __name__ == "__main__":
    main()
