# -*- coding: utf-8 -*-
"""★ Phase73：index_timing 快照口径复验（准备阶段脚本）

背景：P43/P47 的指数择时 board 结论在不同数据锚点间翻转（+1.05pp ↔ -17.57pp），
Phase46 快照机制上线后按原范式在冻结口径复验。

执行条件（事先写死）：data/snapshots/ 下存在 **≥3 个不同日期**的快照目录
（每日由 updater 的 data_snapshot.auto_snapshot 自动生成，无需人工干预）。
条件未满足 → 打印等待原因并退出 0（可安全挂调度反复探测）。

条件满足时执行（配方照抄 Phase43，见 tools/index_timing_retest.py）：
  on 臂 ：params.index_timing=True，活库，× {score,board} × 4 窗口
  off 臂：最新冻结快照库（datafeed 动态读 C.DB_FILE），dd-off，同矩阵
  两臂同参数同池（bt_pool 前500、fetch_quotes 置空、slippage=0.001、
  zt_eco_gate/dd_gate 关），仅 index_timing 与数据锚点不同 → 对照表隔离
  "数据锚点"效应。输出 data/bt_it_snapshot_retest.json。

用法（系统 Python，项目根）:
  python tools/index_timing_snapshot_retest.py [--execute] [--workers 8]
不加 --execute 时只检查条件（干跑）。
"""
import argparse
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed

# 本脚本位于 tools/（非 tools/ml_sidecar/）：到项目根只需两层 dirname
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SNAP_DIR = os.path.join(BASE, "data", "snapshots")
OUT_JSON = os.path.join(BASE, "data", "bt_it_snapshot_retest.json")
NEED_SNAPSHOTS = 3
POOL_SIZE = 500
WINDOWS = [
    ["2019-01-01", "2020-12-31"],
    ["2021-01-01", "2022-12-31"],
    ["2023-01-01", "2024-12-31"],
    ["2025-08-18", "2026-08-18"],
]
WINDOW_TAGS = ["2019-20", "2021-22", "2023-24", "近1年"]
STRATEGY_PARAMS = {
    "score": {"buy_threshold": 25, "max_positions": 3, "position_pct": 0.30,
              "slippage": 0.001},
    "board": {"buy_threshold": 40, "max_positions": 2, "position_pct": 0.25,
              "slippage": 0.001},
}


def snapshot_dates():
    """已冻结快照的日期列表（含 market.db 的目录才算，按目录名升序）"""
    out = []
    if os.path.isdir(SNAP_DIR):
        for d in sorted(os.listdir(SNAP_DIR)):
            p = os.path.join(SNAP_DIR, d)
            if os.path.isfile(os.path.join(p, "market.db")):
                out.append(d)
    return out


def _run(task):
    """单格回测 worker（★ 模块级：Windows spawn 下嵌套函数无法 pickle）"""
    arm, strategy, widx, codes, snap_db = task
    w0, w1 = WINDOWS[widx]
    t0 = time.time()
    from app import config as C
    from app import engine as eng
    orig_quotes = eng.df.fetch_quotes
    orig_db = C.DB_FILE
    eng.df.fetch_quotes = lambda cs: {}      # 实时市值快照不可复现 → 置空，确定性口径
    if arm == "off":
        C.DB_FILE = snap_db                  # 冻结快照库（datafeed/engine 调用时动态读取）
    try:
        params = dict(STRATEGY_PARAMS[strategy])
        params.update({"zt_eco_gate": False, "dd_gate": False})
        if arm == "on":
            params["index_timing"] = True
        names = {c: c for c in codes}
        bt = eng.Backtest(codes, names, w0, w1, 100000.0, strategy, params)
        r = bt.run()
    finally:
        eng.df.fetch_quotes = orig_quotes
        C.DB_FILE = orig_db
    row = {"arm": arm, "strategy": strategy, "widx": widx, "window": [w0, w1],
           "snapshot": latest_snapshot() if arm == "off" else "live",
           "total_return": r.get("total_return"),
           "max_drawdown": r.get("max_drawdown"),
           "sharpe": r.get("sharpe"),
           "trade_count": r.get("trade_count", len(getattr(bt, "trades", []))),
           "elapsed": round(time.time() - t0, 1)}
    if "error" in r:
        row["error"] = str(r["error"])
        print("  [%s/%s %s] ERROR %s" % (arm, strategy, WINDOW_TAGS[widx],
                                         row["error"]), flush=True)
        return row
    print("  [%s/%s %s] ret=%.4f trades=%s (%.0fs)" % (
        arm, strategy, WINDOW_TAGS[widx], row["total_return"] or 0,
        row["trade_count"], row["elapsed"]), flush=True)
    return row


def latest_snapshot():
    ds = snapshot_dates()
    return os.path.join(SNAP_DIR, ds[-1], "market.db") if ds else ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args()
    dates = snapshot_dates()
    print("快照日期: %s（需 ≥%d 个不同日期）" % (dates or "无", NEED_SNAPSHOTS))
    if len(dates) < NEED_SNAPSHOTS:
        print("等待原因: 冻结快照仅 %d/%d 个不同日期——"
              "每日 updater 成功后自动生成一个；条件满足后重跑本脚本"
              "（加 --execute 全量执行）" % (len(dates), NEED_SNAPSHOTS))
        return 0
    if not a.execute:
        print("条件已满足。加 --execute 执行全量复验。")
        return 0

    # ---- 全量执行 ----
    latest = dates[-1]
    snap_db = os.path.join(SNAP_DIR, latest, "market.db")
    print("最新快照: %s" % snap_db, flush=True)

    from app import config as C   # 仅取 DATA_DIR 读池文件
    with open(os.path.join(C.DATA_DIR, "bt_pool.json"), encoding="utf-8") as f:
        codes = json.load(f)["codes"][:POOL_SIZE]
    print("池: bt_pool.json 前 %d 只" % len(codes), flush=True)

    tasks = [(arm, s, w, codes, snap_db) for arm in ("on", "off")
             for s in ("score", "board") for w in range(len(WINDOWS))]
    runs = []
    ex = ProcessPoolExecutor(max_workers=a.workers)
    futs = {ex.submit(_run, t): t for t in tasks}
    try:
        for fut in as_completed(futs):
            try:
                runs.append(fut.result())
            except Exception as e:                      # 单格失败不拖垮全表
                t = futs[fut]
                runs.append({"arm": t[0], "strategy": t[1], "widx": t[2],
                             "window": WINDOWS[t[2]], "error": repr(e)})
                print("  worker crash [%s/%s]: %r" % (t[0], t[1], e), flush=True)
    finally:
        try:
            ex.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass

    grid = {(r["arm"], r["strategy"], r["widx"]): r for r in runs}
    table = []
    for s in ("score", "board"):
        for wi, tag in enumerate(WINDOW_TAGS):
            on, off = grid.get(("on", s, wi), {}), grid.get(("off", s, wi), {})
            table.append({
                "strategy": s, "window": tag,
                "on_ret": on.get("total_return"), "off_ret": off.get("total_return"),
                "delta_pp": (round(((on.get("total_return") or 0)
                                    - (off.get("total_return") or 0)) * 100, 2)
                             if on.get("total_return") is not None
                             and off.get("total_return") is not None else None),
                "on_error": on.get("error"), "off_error": off.get("error"),
            })
    n_err = sum(1 for r in runs if r.get("error"))
    doc = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
           "phase": "73", "snapshot_used": latest,
           "condition": "data/snapshots ≥%d 个不同日期（实际 %d）" % (
               NEED_SNAPSHOTS, len(dates)),
           "errors": n_err, "table": table, "runs": runs}
    with open(OUT_JSON, "w", encoding="utf-8", newline="\n") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    print("已写出 %s（%d 格，错误 %d）" % (OUT_JSON, len(table), n_err), flush=True)
    for t in table:
        print("  %-5s %-8s on=%s off=%s Δ=%spp%s" % (
            t["strategy"], t["window"], t["on_ret"], t["off_ret"], t["delta_pp"],
            ("  [on:%s off:%s]" % (t["on_error"], t["off_error"]))
            if (t["on_error"] or t["off_error"]) else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
