# -*- coding: utf-8 -*-
"""P71-A1 干净隔离臂：无排除(DATA_EXCLUDE_CODES=[])+momentum 8.0 同快照对照

目的：把"P54冻结MOM7.0→现默认8.0"的动量口径差异从排除扩容效应中剥离。
三臂分解：
  BASE_P53   = P53 冻结基线（excludes@当时 + MOM7.0 冻结）      ← data/snapshot_baseline.json
  ARM_NOEXCL = 本工具（excludes=[]    + MOM8.0，同快照 2026-08-26）
  ARM_AFTER  = 已有回归（excludes=197 + MOM8.0，同快照）        ← data/bt_exclude_landing_regression_parts/
则：动量效应 = ARM_NOEXCL − BASE_P53；干净排除效应 = ARM_AFTER − ARM_NOEXCL。

配方与既有回归完全一致：bt_pool top500 / fetch_quotes置空 / score25,3,0.30,0.001 /
board40,2,0.25,0.001 / C.DB_FILE=快照2026-08-26。
分窗落盘 data/bt_noexcl_baseline_parts/{strategy}_{widx}.json（幂等续跑，只新增数据不动旧档）。
"""
import json, os, sys, time, pathlib

BASE = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

SNAP = os.path.join(BASE, "data", "snapshots", "2026-08-26", "market.db")
PARTS = BASE / "data" / "bt_noexcl_baseline_parts"
WINDOWS = [["2019-01-01", "2020-12-31"], ["2021-01-01", "2022-12-31"],
           ["2023-01-01", "2024-12-31"], ["2025-08-18", "2026-08-21"]]
TAGS = ["2019-20", "2021-22", "2023-24", "近1年"]

from app import config as C          # noqa: E402
if not os.path.isfile(SNAP):
    print("[noexcl] 快照不存在:", SNAP, flush=True)
    sys.exit(1)
C.DB_FILE = SNAP
C.DATA_EXCLUDE_CODES = []            # ★ 运行时置空（engine/board_true/zt_eco 均为 getattr(C,...) 运行时读取）
assert len(C.DATA_EXCLUDE_CODES) == 0
print(f"[noexcl] snapshot={SNAP} excludes={len(C.DATA_EXCLUDE_CODES)} "
      f"MOM={C.BOARD_MOMENTUM_MIN}", flush=True)

from app import engine as eng        # noqa: E402


def run_window(wi, strategy):
    orig_q = eng.df.fetch_quotes
    eng.df.fetch_quotes = lambda cs: {}
    try:
        pool = json.loads((BASE / "data" / "bt_pool.json").read_text(encoding="utf-8"))
        codes = pool["codes"][:500]
        names = {c: c for c in codes}
        params = {"buy_threshold": 25 if strategy == "score" else 40,
                  "max_positions": 3 if strategy == "score" else 2,
                  "position_pct": 0.30 if strategy == "score" else 0.25,
                  "slippage": 0.001}
        bt = eng.Backtest(codes, names, WINDOWS[wi][0], WINDOWS[wi][1],
                          100000.0, strategy, params)
        r = bt.run()
        return {"total_return": r.get("total_return"),
                "max_drawdown": r.get("max_drawdown"),
                "sharpe": r.get("sharpe"),
                "trade_count": r.get("trade_count"),
                "win_rate": r.get("win_rate"),
                "annual_return": r.get("annual_return")}
    finally:
        eng.df.fetch_quotes = orig_q


def main():
    PARTS.mkdir(parents=True, exist_ok=True)
    for strategy in ["board", "score"]:
        for wi in range(4):
            part = PARTS / f"{strategy}_{wi}.json"
            if part.exists():
                print(f"[noexcl] {strategy} {TAGS[wi]} 已有, 跳过", flush=True)
                continue
            t0 = time.time()
            cur = run_window(wi, strategy)
            print(f"[noexcl] {strategy} {TAGS[wi]} ret={cur['total_return']:+.4f} "
                  f"dd={cur['max_drawdown']:+.4f} {time.time()-t0:.1f}s", flush=True)
            part.write_text(json.dumps(cur, ensure_ascii=False, indent=2),
                            encoding="utf-8", newline="\n")
    print("[noexcl] 全部完成 8 窗", flush=True)


if __name__ == "__main__":
    main()
