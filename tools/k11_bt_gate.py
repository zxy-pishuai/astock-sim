# -*- coding: utf-8 -*-
"""★ K11 判据⑤：B4 高潮情绪闸门（SENTIMENT_POS_GATE）新口径下 IS/OOS 复测。

背景：qg_zt_full 已由 K11 统一为 eco 权威口径（limit_prices 精确板价 + ST/新股过滤）。
sentiment_gate.phase_mult(as_of) 读 qg_zt_full.phase → SENTIMENT_POS_MULT 乘数，
叠在 position_pct 的 min() 之后、指数择时之后（engine.py L1190-1201，只缩仓）。

口径（与 Phase21/22/26 基线配方一致）：
  池 = bt_pool.json top500，滤 DATA_EXCLUDE_CODES；fetch_quotes 置空；
  BOARD_MOMENTUM_MIN=7.0（引擎内运行时对齐，不改文件）；
  score 25/3/0.30/0.001；board 40/2/0.25。
窗口（K11 任务约定）：IS=2019-01-01..2023-12-31；OOS=2024-01-01..2026-08-31。
开关组：
  off   = SENTIMENT_POS_GATE=False（基线）
  consv = True + {冰点1.0, 发酵1.0, 高潮0.9, 退潮1.0}（Phase22 保守版）
  orig  = True + {冰点0.85, 发酵1.0, 高潮0.9, 退潮0.3}（Phase16 原版）
统计：total_return / max_drawdown / annual_return / trade_count / 闸门触发次数
（从 bt.log_lines 中 '情绪闸门[' 行数统计；触发次数仅 gate on 有意义）。

用法：python tools/k11_bt_gate.py [--workers 12]
输出：data/bt_k11_gate.json
"""
import json
import os
import sys
import time
import argparse
import multiprocessing as mp

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import config as C          # noqa: E402
from app import engine as eng        # noqa: E402

PARAMS_BY_STRATEGY = {
    "score": {"buy_threshold": 25, "max_positions": 3, "position_pct": 0.30, "slippage": 0.001},
    "board": {"buy_threshold": 40, "max_positions": 2, "position_pct": 0.25, "slippage": 0.001},
}
WINDOWS = {
    "IS": ["2019-01-01", "2023-12-31"],
    "OOS": ["2024-01-01", "2026-08-31"],
}
GATES = {
    "off": (False, {}),
    "consv": (True, {"冰点": 1.0, "发酵": 1.0, "高潮": 0.9, "退潮": 1.0}),
    "orig": (True, {"冰点": 0.85, "发酵": 1.0, "高潮": 0.9, "退潮": 0.3}),
}

_G = {}


def _work_one(task):
    strategy, win, gate = task
    if not _G:
        eng.df.fetch_quotes = lambda codes: {}          # 基线配方：置空实时行情
        C.BOARD_MOMENTUM_MIN = 7.0                      # 运行时对齐 Phase21 基线
        pool = json.load(open(os.path.join(C.DATA_DIR, "bt_pool.json"), encoding="utf-8"))
        codes = pool["codes"][:500]
        names = {}
        try:
            for row in json.load(open(os.path.join(C.DATA_DIR, "stock_list.json"),
                                      encoding="utf-8")):
                names[row[0]] = row[1]
        except Exception:
            pass
        _G["codes"] = [c for c in codes if c not in set(getattr(C, "DATA_EXCLUDE_CODES", None) or [])]
        _G["names"] = {c: names.get(c, c) for c in _G["codes"]}
    on, mult = GATES[gate]
    C.SENTIMENT_POS_GATE = on
    C.SENTIMENT_POS_MULT = dict(mult)
    params = dict(PARAMS_BY_STRATEGY[strategy])
    w0, w1 = WINDOWS[win]
    bt = eng.Backtest(_G["codes"], _G["names"], w0, w1, 100000.0, strategy, params)
    r = bt.run()
    n_gate = 0
    gate_detail = {}
    for ln in getattr(bt, "log_lines", []) or []:
        if "情绪闸门[" in ln:
            n_gate += 1
            # 形如 "2024-01-05 情绪闸门[高潮]×0.9(低置信)"
            try:
                seg = ln.split("情绪闸门[")[1].split("]")[0]
                gate_detail[seg] = gate_detail.get(seg, 0) + 1
            except Exception:
                pass
    return {"label": "%s|%s|%s" % (gate, strategy, win),
            "strategy": strategy, "win": win, "gate": gate,
            "total_return": r.get("total_return"),
            "max_drawdown": r.get("max_drawdown"),
            "annual_return": r.get("annual_return"),
            "trade_count": r.get("trade_count"),
            "n_days": len(getattr(bt, "trading_days", []) or []),
            "gate_triggers": n_gate,
            "gate_detail": gate_detail}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=12)
    a = ap.parse_args()
    t0 = time.time()
    tasks = [(s, w, g) for g in GATES for s in ("score", "board") for w in WINDOWS]
    print("任务 %d 跑（%d workers）..." % (len(tasks), a.workers), flush=True)
    with mp.Pool(a.workers) as pool:
        results = pool.map(_work_one, tasks)
    out = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
           "pool": "bt_pool.json top500（滤 DATA_EXCLUDE_CODES）",
           "windows": WINDOWS,
           "gates": {k: {"on": v[0], "mult": v[1]} for k, v in GATES.items()},
           "runs": {r["label"]: r for r in results},
           "meta": {"caliber": "qg_zt_full=eco 权威口径（K11 2026-09-16）",
                    "rule": "IS=2019-01-01..2023-12-31 / OOS=2024-01-01..2026-08-31"}}
    out_path = os.path.join(C.DATA_DIR, "bt_k11_gate.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("\n=== B4 闸门 IS/OOS 复测（新口径） ===")
    print("%-22s %-10s %-10s %-10s %-8s %-6s" % (
        "label", "total_ret", "mdd", "annual", "trades", "gate#"))
    for r in results:
        tr = r["total_return"]
        print("%-22s %-10s %-10s %-10s %-8s %-6s %s" % (
            r["label"],
            ("%+.2f%%" % (tr * 100)) if tr is not None else "?",
            ("%+.2f%%" % (r["max_drawdown"] * 100)) if r["max_drawdown"] is not None else "?",
            ("%+.2f%%" % (r["annual_return"] * 100)) if r["annual_return"] is not None else "?",
            r["trade_count"], r["gate_triggers"], r["gate_detail"]))
    print("\n结果 → %s" % out_path, flush=True)
    print("总耗时 %.1fs" % (time.time() - t0), flush=True)


if __name__ == "__main__":
    main()
