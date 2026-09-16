# -*- coding: utf-8 -*-
"""★ Phase48+49 回归运行器：engine 收敛修复的前后对照基线

配方（dd-off 内部自洽口径，不拿 bt_clean_results 当逐位目标）：
  - 池 = bt_pool.json 前 500，names=代码兜底，fetch_quotes 置空
  - score: buy_threshold=25 / max_positions=3 / position_pct=0.30 / slippage=0.001
  - board: buy_threshold=40 / max_positions=2 / position_pct=0.25 / slippage=0.001
  - ★ BOARD_MOMENTUM_MIN 冻结 7.0（config 当前 8.0 是 Phase33 判定值；
    worker 内运行期改属性冻结，不改 config 文件）
  - zt_eco_gate / dd_gate 显式关闭；seed=42

用法:
  python tools/bt_convergence_runner.py --stage before --out data/bt_conv_before.json
  python tools/bt_convergence_runner.py --stage after  --out data/bt_conv_after.json
"""
import argparse
import json
import os
import sys
import time

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
WINDOW_TAGS = ["2019-20", "2021-22", "2023-24", "近1年"]
POOL_SIZE = 500
STRATEGY_PARAMS = {
    "score": {"buy_threshold": 25, "max_positions": 3, "position_pct": 0.30},
    "board": {"buy_threshold": 40, "max_positions": 2, "position_pct": 0.25},
}
SLIPPAGE = 0.001
BASE_PARAMS = {"zt_eco_gate": False, "dd_gate": False}
TASKS = [(s, w) for s in ("score", "board") for w in range(len(WINDOWS))]

codes = names = None
SNAP_DB = None   # ★ Phase46：冻结快照路径（worker 内设置 C.DB_FILE 指向它）
SNAP_INFO = None  # ★ B2：快照明细 {tag, age_days, coverage, ...}（结果 JSON 追溯）


def _resolve_db():
    """★ 冻结快照优先：updater 每晚/盘中会重写前复权历史，活库连"同日"都不稳定；
    快照口径才可复现。★ B2（2026-09-13）：新鲜度断言——无快照或最新快照超期
    （默认 >3 天）直接拒绝运行（exit 2），杜绝"过期快照静默回测"。
    主进程在 main() 开头先行检查；worker initializer 复用本函数（已通过）。"""
    global SNAP_DB, SNAP_INFO
    from app import config as C
    from app import data_snapshot as ds
    snap = ds.latest_snapshot_path(max_age_days=3)
    if not snap:
        print("[B2] 无可用快照或最新快照超期（>3 天，含无快照），拒绝运行——"
              "请先补快照（python tools/data_snapshot.py --force 或等服务收盘自动生成）",
              flush=True)
        sys.exit(2)
    SNAP_DB = snap
    SNAP_INFO = ds.snapshot_info(snap)
    return SNAP_DB


def _init_worker():
    global codes, names
    from app import config as C
    _resolve_db()
    C.DB_FILE = SNAP_DB                    # ★ 冻结快照口径
    with open(os.path.join(C.DATA_DIR, "bt_pool.json"), encoding="utf-8") as f:
        pool = json.load(f)
    codes = list(pool["codes"])[:POOL_SIZE]
    names = {c: c for c in codes}


def run_seed(cfg, strategy, seed, widx):
    """★ Phase67：当前落地配置 × 指定 seed 的单窗回测（模块级，可 pickle）。"""
    from app import config as C
    from app import engine as eng
    C.DB_FILE = SNAP_DB                    # ★ 冻结快照口径
    w0, w1 = WINDOWS[widx]
    t0 = time.time()
    orig_quotes = eng.df.fetch_quotes
    eng.df.fetch_quotes = lambda cs: {}     # 当前落地配置原样使用：不冻结动量下限
    try:
        params = dict(STRATEGY_PARAMS[strategy])
        params.update(BASE_PARAMS)
        params["slippage"] = SLIPPAGE
        params["seed"] = seed               # ★ 唯一变量
        bt = eng.Backtest(codes, names, w0, w1, 100000.0, strategy, params)
        r = bt.run()
    finally:
        eng.df.fetch_quotes = orig_quotes
    row = {"config": cfg, "strategy": strategy, "seed": seed, "widx": widx,
           "window": [w0, w1],
           "total_return": r.get("total_return"),
           "annual_return": r.get("annual_return"),
           "max_drawdown": r.get("max_drawdown"),
           "win_rate": r.get("win_rate"),
           "trade_count": len(bt.trades),
           "elapsed": round(time.time() - t0, 1)}
    print("  [%s %s seed=%d %s] ret=%+.4f dd=%.4f trades=%d (%.0fs)" % (
        cfg, strategy, seed, WINDOW_TAGS[widx], row["total_return"] or 0,
        row["max_drawdown"] or 0, row["trade_count"], row["elapsed"]), flush=True)
    return row


def run_one(task, stage):
    strategy, widx = task
    from app import config as C
    from app import engine as eng
    w0, w1 = WINDOWS[widx]
    t0 = time.time()
    orig_quotes = eng.df.fetch_quotes
    orig_mom = getattr(C, "BOARD_MOMENTUM_MIN", None)
    eng.df.fetch_quotes = lambda cs: {}
    C.BOARD_MOMENTUM_MIN = 7.0            # ★ 冻结动量档位下限（仅本进程）
    try:
        params = dict(STRATEGY_PARAMS[strategy])
        params.update(BASE_PARAMS)
        params["slippage"] = SLIPPAGE
        bt = eng.Backtest(codes, names, w0, w1, 100000.0, strategy, params)
        r = bt.run()
    finally:
        eng.df.fetch_quotes = orig_quotes
        if orig_mom is not None:
            C.BOARD_MOMENTUM_MIN = orig_mom
    row = {"stage": stage, "strategy": strategy, "widx": widx,
           "window": [w0, w1],
           "total_return": r.get("total_return"),
           "annual_return": r.get("annual_return"),
           "max_drawdown": r.get("max_drawdown"),
           "win_rate": r.get("win_rate"),
           "sharpe": r.get("sharpe"),
           "trade_count": len(bt.trades),
           "elapsed": round(time.time() - t0, 1)}
    print("  [%s %s %s] ret=%.4f dd=%.4f trades=%d (%.0fs)" % (
        stage, strategy, WINDOW_TAGS[widx], row["total_return"] or 0,
        row["max_drawdown"] or 0, row["trade_count"], row["elapsed"]), flush=True)
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", required=False, choices=["before", "after"])
    ap.add_argument("--out", required=True)
    # ★ Phase67：多种子稳健性模式——已落地配置 × seeds × 四窗口，验证方向不随种子翻转。
    #   与 --stage 互斥；此模式下【不冻结】BOARD_MOMENTUM_MIN、不改任何参数，
    #   全部使用当前 config 落地默认值（止损 -0.07 / 动量 8.0 / risk_parity），
    #   仅 params.seed 在 {7,42,123} 间切换（影响 exec_prob 排队成交抽签）。
    ap.add_argument("--seeds", default=None, help="逗号分隔，如 7,42,123")
    ap.add_argument("--configs", default="stop07,mom8,rp_score,rp_board",
                    help="stop07=score | mom8=board | rp_score | rp_board")
    a = ap.parse_args()

    t0 = time.time()
    # ★ B2：主进程先行快照新鲜度检查（超期/无快照 → _resolve_db 内 exit 2 拒绝）
    _resolve_db()
    print("[B2] snapshot_tag=%s age=%sd coverage=%s" % (
        SNAP_INFO["tag"], SNAP_INFO["age_days"], SNAP_INFO["coverage"] or {}),
        flush=True)
    runs = []
    if os.path.exists(a.out):
        with open(a.out, encoding="utf-8") as f:
            runs = json.load(f).get("runs", [])
        print("断点续跑：已有 %d 组" % len(runs), flush=True)

    CFG_STRATEGY = {"stop07": "score", "mom8": "board",
                    "rp_score": "score", "rp_board": "board"}
    seeds = [int(x) for x in a.seeds.split(",")] if a.seeds else None

    if seeds:
        # ---------- Phase67 多种子模式 ----------
        cfgs = [c.strip() for c in a.configs.split(",") if c.strip()]
        tasks = [(cf, CFG_STRATEGY[cf], s, w)
                 for cf in cfgs for s in seeds for w in range(len(WINDOWS))]
        done = {(r.get("config"), r.get("seed"), r["strategy"], r["widx"]) for r in runs}
        todo = [t for t in tasks if (t[0], t[1], t[2], t[3]) not in done]
        print("Phase67 模式：configs=%s seeds=%s 待跑 %d/%d" % (
            cfgs, seeds, len(todo), len(tasks)), flush=True)

        # run_seed 使用模块级定义（Windows spawn 需可 pickle）
        def dump():
            with open(a.out, "w", encoding="utf-8") as f:
                json.dump({"mode": "phase67_seeds", "configs": cfgs, "seeds": seeds,
                           "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                           # ★ B2：快照新鲜度追溯
                           "snapshot_tag": SNAP_INFO["tag"],
                           "snapshot_age_days": SNAP_INFO["age_days"],
                           "snapshot_coverage": SNAP_INFO["coverage"] or {},
                           "runs": runs}, f, ensure_ascii=False, indent=1)

        from concurrent.futures import ProcessPoolExecutor, as_completed
        if todo:
            ex = ProcessPoolExecutor(max_workers=min(8, len(todo)),
                                     initializer=_init_worker)
            try:
                for fut in as_completed({ex.submit(run_seed, *t): t for t in todo}):
                    try:
                        runs.append(fut.result())
                    except Exception as e:
                        print("任务异常: %s" % e, flush=True)
                    dump()
                    print("进度 %d/%d，累计 %.0fs" % (
                        len(runs), len(tasks), time.time() - t0), flush=True)
            finally:
                try:
                    ex.shutdown(wait=False, cancel_futures=True)
                except Exception:
                    pass
        done = {(r.get("config"), r.get("seed"), r["strategy"], r["widx"]) for r in runs}
        missing = [t for t in tasks if (t[0], t[1], t[2], t[3]) not in done]
        for t in missing:
            _init_worker()
            runs.append(run_seed(*t))
            dump()
        dump()
        # ---- 方向稳健性判定（★ Phase70：judge_kit 统一库）----
        _tools_dir = os.path.dirname(os.path.abspath(__file__))
        if _tools_dir not in sys.path:
            sys.path.insert(0, _tools_dir)
        import judge_kit as jk
        agg = {}
        for r in runs:
            agg.setdefault((r["config"], r["strategy"], r["widx"]), {})[r["seed"]] = \
                r["total_return"] or 0.0
        flips = 0
        print("\n=== 种子方向稳健性 ===")
        for (cfg, strat, wi), by_seed in sorted(agg.items()):
            st = jk.seed_stability(by_seed)
            if st["direction_flip"]:
                flips += 1
            print("%-9s %-6s %-9s seeds收益=%s 方向%s 极差=%.1fpp" % (
                cfg, strat, WINDOW_TAGS[wi],
                "/".join("%+.1f" % (v * 100) for v in by_seed.values()),
                ("翻转❌" if st["direction_flip"] else "一致✅"), st["spread_pp"]))
        print("\n种子方向翻转：%d/%d 组合" % (flips, len(agg)))
        print("完成：%s" % a.out, flush=True)
        return

    # ---------- 原 before/after 回归模式 ----------
    if not a.stage:
        print("需要 --stage 或 --seeds")
        return
    done = {(r["strategy"], r["widx"]) for r in runs}

    from concurrent.futures import ProcessPoolExecutor, as_completed
    todo = [t for t in TASKS if t not in done]
    print("stage=%s 待跑 %d/%d" % (a.stage, len(todo), len(TASKS)), flush=True)

    def dump():
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump({"stage": a.stage,
                       "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                       # ★ B2：快照新鲜度追溯
                       "snapshot_tag": SNAP_INFO["tag"],
                       "snapshot_age_days": SNAP_INFO["age_days"],
                       "snapshot_coverage": SNAP_INFO["coverage"] or {},
                       "runs": sorted(runs, key=lambda r: (
                           TASKS.index((r["strategy"], r["widx"]))
                           if (r["strategy"], r["widx"]) in TASKS else 99))},
                      f, ensure_ascii=False, indent=1)

    if todo:
        ex = ProcessPoolExecutor(max_workers=min(8, len(todo)),
                                 initializer=_init_worker)
        futs = {ex.submit(run_one, t, a.stage): t for t in todo}
        try:
            for fut in as_completed(futs):
                t = futs[fut]
                try:
                    runs.append(fut.result())
                except Exception as e:
                    print("任务异常 %s: %s" % (t, e), flush=True)
                dump()
                print("进度 %d/%d，累计 %.0fs" % (
                    len(runs), len(TASKS), time.time() - t0), flush=True)
        finally:
            try:
                ex.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass
        done = {(r["strategy"], r["widx"]) for r in runs}
        for t in TASKS:
            if t not in done:
                print("补跑 %s" % (t,), flush=True)
                _init_worker()
                runs.append(run_one(t, a.stage))
                dump()
        order = {tt: i for i, tt in enumerate(TASKS)}
        runs.sort(key=lambda r: order.get((r["strategy"], r["widx"]), 99))
        dump()
    print("完成：%s（%d 组）" % (a.out, len(runs)), flush=True)


if __name__ == "__main__":
    main()
