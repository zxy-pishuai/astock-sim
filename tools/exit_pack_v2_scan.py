# -*- coding: utf-8 -*-
"""★ Phase68: 卖点策略包 v2 参数研究（monkeypatch config，零 app 修改）。

候选三项（P45 审计 + P36 已扫项的快照口径复验）：
  A 移动止损参数化：TRAILING_ACTIVATE_PCT/TRAILING_STOP_PCT 变体
  B 冲高回落止盈参数化：INTRADAY_HIGH_TRIGGER/INTRADAY_PULLBACK 变体
  C TIME_STOP_DAYS ∈ {2,4}（P36 扫过 {2,3,4}，本轮回照冻结快照口径复核）
基线臂 = 当前落地默认（含 STOP_LOSS_PCT=-0.07）。

判定（事先写死）：某变体 ≥3/4 窗口收益改善或持平(Δ≥-1pp) 且牛市恶化 ≤2pp
→ 建议采纳（只记录建议，不改 config，交验收方）。

数据口径：★ 冻结快照 data/snapshots/2026-08-26/market.db（可复现）。
"""
import sys
import os
import json
import time
from concurrent.futures import ProcessPoolExecutor, as_completed

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

WINDOWS = [["2019-01-01", "2020-12-31"], ["2021-01-01", "2022-12-31"],
           ["2023-01-01", "2024-12-31"], ["2025-08-18", "2026-08-21"]]
WIN_NAMES = ["2019-20", "2021-22", "2023-24", "牛市"]
STRATEGY = "score"
POOL_N = 500
PARAMS = {"buy_threshold": 25, "max_positions": 3, "position_pct": 0.30,
          "slippage": 0.001, "zt_eco_gate": False, "dd_gate": False}

# 变体网格（patch: {config属性: 值}）
VARIANTS = [
    ("baseline", {}),
    ("A1_trail_act8_stop4", {"TRAILING_ACTIVATE_PCT": 0.08, "TRAILING_STOP_PCT": -0.04}),
    ("A2_trail_act12_stop5", {"TRAILING_ACTIVATE_PCT": 0.12, "TRAILING_STOP_PCT": -0.05}),
    ("B1_pullback_trig5_back25", {"INTRADAY_HIGH_TRIGGER": 0.05, "INTRADAY_PULLBACK": 0.025}),
    ("B2_pullback_trig8_back40", {"INTRADAY_HIGH_TRIGGER": 0.08, "INTRADAY_PULLBACK": 0.04}),
    ("C1_time2", {"TIME_STOP_DAYS": 2}),
    ("C2_time4", {"TIME_STOP_DAYS": 4}),
]
J_MIN_WIN, J_MAX_LOSS_PP, J_BULL_MAX_LOSS_PP = 3, -1.0, -2.0

_G = {}


def _init_worker(snapshot):
    from app import config as C
    _G["snap"] = snapshot
    C.DB_FILE = snapshot                    # ★ 冻结快照口径
    with open(os.path.join(C.DATA_DIR, "bt_pool.json"), encoding="utf-8") as f:
        codes = json.load(f)["codes"][:POOL_N]
    _G["codes"] = codes
    _G["names"] = {c: c for c in codes}


def _run_one(task):
    label, patches, widx = task
    from app import config as C
    from app import engine as eng
    w0, w1 = WINDOWS[widx]
    orig = {k: getattr(C, k) for k in patches}
    orig_q = eng.df.fetch_quotes
    eng.df.fetch_quotes = lambda cs: {}
    try:
        for k, v in patches.items():
            setattr(C, k, v)
        bt = eng.Backtest(_G["codes"], _G["names"], w0, w1, 100000.0,
                          STRATEGY, dict(PARAMS))
        r = bt.run()
    finally:
        eng.df.fetch_quotes = orig_q
        for k, v in orig.items():
            setattr(C, k, v)
    return {"variant": label, "widx": widx, "window": WIN_NAMES[widx],
            "total_return": r.get("total_return"),
            "max_drawdown": r.get("max_drawdown"),
            "trade_count": r.get("trade_count"), "win_rate": r.get("win_rate")}


def main():
    t0 = time.time()
    from app import config as C
    from app import data_snapshot as ds
    snap = ds.latest_snapshot_path()
    if not snap:
        print("无冻结快照，中止"); return
    print("[db] 冻结快照:", snap, flush=True)

    tasks = [(lab, p, wi) for lab, p in VARIANTS for wi in range(4)]
    results = {}
    with ProcessPoolExecutor(max_workers=7, initializer=_init_worker,
                             initargs=(snap,)) as ex:
        futs = {ex.submit(_run_one, t): t for t in tasks}
        done_n = 0
        for fut in as_completed(futs):
            try:
                r = fut.result()
                results.setdefault(r["variant"], {})[r["widx"]] = r
            except Exception as e:
                print("任务异常:", e, flush=True)
            done_n += 1
            if done_n % 7 == 0:
                print("进度 %d/%d 累计 %.0fs" % (done_n, len(tasks), time.time() - t0),
                      flush=True)

    # ---- 判定（★ Phase70：统一判定库 judge_kit，符号/单位约定见其 docstring）----
    _tools_dir = os.path.dirname(os.path.abspath(__file__))
    if _tools_dir not in sys.path:
        sys.path.insert(0, _tools_dir)
    import judge_kit as jk
    base = results.get("baseline", {})
    judged = []
    for lab, _p in VARIANTS[1:]:
        wins_ok, losses = 0, []
        detail = []
        for wi in range(4):
            b = base.get(wi)
            v = results.get(lab, {}).get(wi)
            if not b or not v:
                continue
            loss_pp = ((b["total_return"] or 0) - (v["total_return"] or 0)) * 100
            losses.append(loss_pp)
            ok_w = abs(loss_pp) <= abs(J_MAX_LOSS_PP) or loss_pp < 0   # 恶化≤1pp 或改善
            wins_ok += 1 if ok_w else 0
            detail.append({"win": WIN_NAMES[wi], "ret": v["total_return"],
                           "base_ret": b["total_return"], "loss_pp": round(loss_pp, 2),
                           "ok": ok_w})
        bull_loss = losses[3] if len(losses) > 3 else None
        passed = jk.window_pass(losses, tol_loss_pp=abs(J_MAX_LOSS_PP),
                                min_windows=J_MIN_WIN)[1] \
            and jk.bull_ok(bull_loss, max_loss_pp=abs(J_BULL_MAX_LOSS_PP))
        judged.append({"variant": lab, "detail": detail,
                       "windows_ok": wins_ok,
                       "bull_loss_pp": round(bull_loss, 2) if bull_loss is not None else None,
                       "pass": passed})
    out = {
        "meta": {"snapshot": snap, "strategy": STRATEGY, "pool_n": POOL_N,
                 "params": PARAMS,
                 "judge": "≥3/4 窗口 Δ≥-1pp 且牛市损失 ≤2pp",
                 "variants": {lab: p for lab, p in VARIANTS}},
        "results": results, "judgement": judged,
        "elapsed_s": round(time.time() - t0, 1),
    }
    path = os.path.join(C.DATA_DIR, "bt_exit_pack_v2_p68.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    print("\n=== 判定 ===")
    for j in judged:
        print("%-24s 达标窗=%d 牛市损=%s pass=%s" % (
            j["variant"], j["windows_ok"], j["bull_loss_pp"], j["pass"]))
    print("saved", path)


if __name__ == "__main__":
    main()
