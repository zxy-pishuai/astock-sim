# -*- coding: utf-8 -*-
"""★ Phase25 高速回测 worker：(strategy, weight, window) 单窗口一进程。

对齐说明（关键）：
- 复现 Phase21 基线的精确条件（tmp/diag_align2.py 实证）：
  ① 池 = bt_pool.json 前 500（engine 内滤 DATA_EXCLUDE_CODES）
  ② params：score={bt25,pos3,pct0.30}，board={bt40,pos2,pct0.25}，slippage=0.001，capital=10万
  ③ fetch_quotes 置空（基线运行时无实时行情，mcap 走成交额×20 近似）→ 本 worker 进程内
     monkeypatch，不改任何项目源码
- 结果落盘 data/bt_ml_weight_parts/{key}.json，由 merge 脚本汇总。
"""
import sys
import os
import json
import time
import argparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import config as C          # noqa: E402
from app import engine as eng        # noqa: E402

# ★ 对齐条件③：进程内禁用实时行情（mcap 用成交额近似，消除网络不确定性）
eng.df.fetch_quotes = lambda codes: {}

PARTS_DIR = os.path.join(C.DATA_DIR, "bt_ml_weight_parts")
WINDOWS = [
    ["2019-01-01", "2020-12-31"],
    ["2021-01-01", "2022-12-31"],
    ["2023-01-01", "2024-12-31"],
    ["2025-08-18", "2026-08-18"],
]
PARAMS = {
    "score": {"buy_threshold": 25, "max_positions": 3, "position_pct": 0.3, "slippage": 0.001},
    "board": {"buy_threshold": 40, "max_positions": 2, "position_pct": 0.25, "slippage": 0.001},
}


def load_pool(top_n=500):
    with open(os.path.join(C.DATA_DIR, "bt_pool.json"), encoding="utf-8") as f:
        pool = json.load(f)
    codes = [c for c in pool["codes"][:top_n]]
    return codes, {c: c for c in codes}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", required=True, choices=["score", "board"])
    ap.add_argument("--weight", required=True, type=float)
    args = ap.parse_args()
    os.makedirs(PARTS_DIR, exist_ok=True)
    key = "%s_w%.1f" % (args.strategy, args.weight)
    out_path = os.path.join(PARTS_DIR, key + ".json")

    done = {}
    if os.path.exists(out_path):
        try:
            done = json.load(open(out_path, encoding="utf-8")).get("windows", {})
        except Exception:
            done = {}

    codes, names = load_pool()
    C.ML_RANK_WEIGHT = args.weight   # 运行时覆盖（仅本进程）

    for i, (w0, w1) in enumerate(WINDOWS):
        wk = "%d" % i
        if wk in done and "error" not in done[wk]:
            continue
        t0 = time.time()
        bt = eng.Backtest(codes, names, w0, w1, 100000.0, args.strategy, dict(PARAMS[args.strategy]))
        try:
            r = bt.run()
            r["elapsed"] = round(time.time() - t0, 1)
        except Exception:
            r = {"error": str(sys.exc_info()[1])}
        done[wk] = {"window": [w0, w1], "start": w0, "end": w1, **r}
        tmp = out_path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump({"strategy": args.strategy, "weight": args.weight, "windows": done},
                      f, ensure_ascii=False, indent=1)
        os.replace(tmp, out_path)
        print("[done] %s win%s %.0fs ret=%.4f trades=%s" % (
            key, wk, r.get("elapsed", -1), r.get("total_return", 0) if "error" not in r else float('nan'),
            r.get("trade_count")), flush=True)


if __name__ == "__main__":
    main()
