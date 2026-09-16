# -*- coding: utf-8 -*-
"""★ Phase47+50：config 落地批处理回归对照（同日 A/B，规避前复权重锚定漂移）

落地项：
  a) STOP_LOSS_PCT -0.05 → -0.07（Phase36 判定）
  b) 指数择时 board 专属（Phase43 判定）：trader 打板路径接线；回测等价口径为
     params.index_timing=True（engine 侧本就支持；trader 接线不影响引擎路径，
     本回归用 params 开关复现落地后的 board 行为）

同日 A/B 口径（已知陷阱①：上游每晚前复权重写历史收盘，禁止拿
bt_clean_results.json 等旧文件当逐位目标）：
  score 臂：pre(STOP=-0.05) vs post(-0.07)，其余冻结 25/3/0.30/0.001
  board 臂：pre(无择时) vs post(index_timing=True)，其余冻结 40/2/0.25/0.001
配方：bt_pool top500、fetch_quotes 置空、四窗口。

用法: python tools/config_landing_regression.py [--workers 8]
输出: data/bt_config_landing_regression.json
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
SCORE_PARAMS = {"buy_threshold": 25, "max_positions": 3,
                "position_pct": 0.30, "slippage": 0.001}
BOARD_PARAMS = {"buy_threshold": 40, "max_positions": 2,
                "position_pct": 0.25, "slippage": 0.001}
OUT_JSON = os.path.join(BASE, "data", "bt_config_landing_regression.json")


def _worker(spec):
    strat, params, tag, codes, names = spec
    t0 = time.time()
    from app import config as C
    from app import engine as eng
    orig_q = eng.df.fetch_quotes
    # 引擎在调用点直读 C.STOP_LOSS_PCT（无同名 params 支持）→ 必须进程内
    # monkeypatch（Phase25/36 先例）；params 里仅作记录。
    orig_stop = getattr(C, "STOP_LOSS_PCT")
    C.STOP_LOSS_PCT = float(params.get("stop_loss_pct", orig_stop))
    eng.df.fetch_quotes = lambda cs: {}
    try:
        bt = eng.Backtest(codes, names, w0 := params["_w0"], w1 := params["_w1"],
                          100000.0, strat,
                          {k: v for k, v in params.items() if not k.startswith("_")})
        r = bt.run()
    finally:
        C.STOP_LOSS_PCT = orig_stop
        eng.df.fetch_quotes = orig_q
    if "error" in r:
        return {"tag": tag, "error": r["error"]}
    mdd = r.get("max_drawdown") or 0.0
    ann = r.get("annual_return") or 0.0
    row = {
        "tag": tag, "strategy": strat, "window": [params["_w0"], params["_w1"]],
        "total_return": r.get("total_return"),
        "max_drawdown": mdd,
        "calmar": round(ann / abs(mdd), 3) if mdd < 0 else None,
        "trade_count": r.get("trade_count"),
        "elapsed_s": round(time.time() - t0),
    }
    print("  [%s] %s~%s ret=%+.4f dd=%.4f trades=%d (%ds)" % (
        tag, row["window"][0][:7], row["window"][1][:7], row["total_return"] or 0,
        mdd, row["trade_count"], row["elapsed_s"]), flush=True)
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args()
    t0 = time.time()
    from tools.backtest_zt_eco import build_pool
    codes, names = build_pool()
    print("池: %d 只 | 同日 A/B 回归" % len(codes), flush=True)

    specs = []
    # score 臂：STOP_LOSS_PCT 落地前后
    for stop, tag in ((-0.05, "score_stop_pre"), (-0.07, "score_stop_post")):
        for w0, w1 in WINDOWS:
            p = dict(SCORE_PARAMS)
            p["stop_loss_pct"] = stop
            p["_w0"], p["_w1"] = w0, w1
            specs.append(("score", p, "%s|%s" % (tag, w0), codes, names))
    # board 臂：指数择时落地前后（engine 口径 = params.index_timing）
    for on, tag in ((False, "board_timing_pre"), (True, "board_timing_post")):
        for w0, w1 in WINDOWS:
            p = dict(BOARD_PARAMS)
            p["index_timing"] = on
            p["_w0"], p["_w1"] = w0, w1
            specs.append(("board", p, "%s|%s" % (tag, w0), codes, names))

    results = []
    err = False
    with Pool(processes=a.workers) as pool:
        for row in pool.imap_unordered(_worker, specs):
            if "error" in row:
                err = True
            results.append(row)
    if err:
        print("存在失败组，中止判定", flush=True)
        json.dump({"error": "runs failed", "rows": results},
                  open(OUT_JSON, "w", encoding="utf-8", newline="\n"),
                  ensure_ascii=False, indent=1)
        return

    def four(tag):
        rs = sorted([r for r in results if r["tag"].startswith(tag + "|")],
                    key=lambda x: x["window"][0])
        assert len(rs) == 4
        return rs

    pre_s, post_s = four("score_stop_pre"), four("score_stop_post")
    pre_b, post_b = four("board_timing_pre"), four("board_timing_post")

    def summarize(pre, post, label):
        ds = [post[i]["total_return"] - pre[i]["total_return"] for i in range(4)]
        return {"label": label,
                "pre_ret": [r["total_return"] for r in pre],
                "post_ret": [r["total_return"] for r in post],
                "pre_trades": [r["trade_count"] for r in pre],
                "post_trades": [r["trade_count"] for r in post],
                "deltas_pp": [round(d * 100, 2) for d in ds],
                "bull_delta_pp": round(ds[3] * 100, 2),
                "avg_delta_pp": round(sum(ds) / 4 * 100, 2),
                "matches_phase36_expect": label != "stop"}

    arms = [summarize(pre_s, post_s, "STOP_LOSS_PCT_-0.05to-0.07(score)"),
            summarize(pre_b, post_b, "index_timing_board(board)")]
    payload = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "phase": "47+50",
        "windows": WINDOWS,
        "same_day_ab": True,
        "note": "前复权每晚重锚定：与历史报告数值不可比，仅本 JSON 内 pre/post 同日可比",
        "landings": {
            "STOP_LOSS_PCT": "-0.05 -> -0.07（依据 docs/reports/exit_scan.md）",
            "INDEX_TIMING_BOARD_ONLY": "True（trader 打板路径接线；全局开关仍 False）",
        },
        "arms": arms,
    }
    with open(OUT_JSON, "w", encoding="utf-8", newline="\n") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    for x in arms:
        print("%s: Δ=%s 牛市=%.2fpp 平均=%.2fpp" % (
            x["label"], x["deltas_pp"], x["bull_delta_pp"], x["avg_delta_pp"]),
            flush=True)
    print("已写出 %s | 总耗时 %.0fs" % (OUT_JSON, time.time() - t0), flush=True)


if __name__ == "__main__":
    main()
