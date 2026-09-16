# -*- coding: utf-8 -*-
"""★ Phase77: 时间止损 T=2 复验 —— P68 最接近达标项的预注册二次验证。

预注册内容（跑前写死，判定只用本文件常量与 judge_kit）：
  新增臂：
    T2        : TIME_STOP_DAYS=2（其余默认，含 STOP=-0.07 落地值）
    T2_A1     : T2 + 移动止损 act8%/stop-4%
    T2_A2     : T2 + 移动止损 act12%/stop-5%
  复用臂（同快照已实测，直接引用 data/bt_exit_pack_v2.json）：
    baseline  : T=3 默认退出
    T3_A1 / T3_A2 : A1/A2 移动止损变体（T=3）
  PIT 对照臂（牛市窗，pit_pools["近1年"] 成分池）：
    baseline_pit / 各新增变体_pit —— 全部候选配置都跑，不挑 favourable

判定（事先写死）：
  推荐落地 = 变体满足 judge_kit.landing_rule
  （≥3/4 窗口损失≥-1pp 且 牛市损失≤2pp，均相对同快照 baseline 臂；
    PIT 对照臂仅作佐证呈现，不改变静态判定）。
  多个变体同时通过 → 按"四窗损失之和最负（总改善最大）"排序取首。

数据口径：冻结快照 data/snapshots/2026-08-26/market.db（Phase46）；score 策略；top500。
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

from app import config as C          # noqa: E402
from app import data_snapshot as ds  # noqa: E402

WINDOWS = [["2019-01-01", "2020-12-31"], ["2021-01-01", "2022-12-31"],
           ["2023-01-01", "2024-12-31"], ["2025-08-18", "2026-08-21"]]
WIN_NAMES = ["2019-20", "2021-22", "2023-24", "牛市"]
STRATEGY = "score"
POOL_N = 500
PARAMS = {"buy_threshold": 25, "max_positions": 3, "position_pct": 0.30,
          "slippage": 0.001, "zt_eco_gate": False, "dd_gate": False}
BULL_WIDX = 3

A1 = {"TRAILING_ACTIVATE_PCT": 0.08, "TRAILING_STOP_PCT": -0.04}
A2 = {"TRAILING_ACTIVATE_PCT": 0.12, "TRAILING_STOP_PCT": -0.05}

NEW_ARMS = [
    ("T2", {"TIME_STOP_DAYS": 2}),
    ("T2_A1", dict(A1, TIME_STOP_DAYS=2)),
    ("T2_A2", dict(A2, TIME_STOP_DAYS=2)),
]
REUSE_LABELS = ["baseline", "A1_trail_act8_stop4", "A2_trail_act12_stop5"]

P68_FILE = os.path.join(C.DATA_DIR, "bt_exit_pack_v2.json")
OUT_FILE = os.path.join(C.DATA_DIR, "bt_time_stop_reverify.json")

_G = {"snap": None}


def _init_worker(snapshot):
    import io
    from app import config as C
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  errors="replace") if hasattr(sys.stdout, "buffer") else sys.stdout
    C.DB_FILE = snapshot
    _G["snap"] = snapshot
    with open(os.path.join(C.DATA_DIR, "bt_pool.json"), encoding="utf-8") as f:
        codes = json.load(f)["codes"][:500]
    _G["codes"] = [c for c in codes
                   if c not in set(getattr(C, "DATA_EXCLUDE_CODES", None) or [])]
    _G["names"] = {c: c for c in _G["codes"]}


def _run_one(label, patches, widx, codes=None):
    """codes=None 用主池；传列表则用覆盖池（PIT 臂）。"""
    from app import config as C
    from app import engine as eng
    w0, w1 = WINDOWS[widx]
    use_codes = codes if codes else _G["codes"]
    orig = {k: getattr(C, k) for k in patches}
    orig_q = eng.df.fetch_quotes
    eng.df.fetch_quotes = lambda cs: {}
    try:
        for k, v in patches.items():
            setattr(C, k, v)
        bt = eng.Backtest(use_codes, ({c: c for c in use_codes}), w0, w1,
                          100000.0, STRATEGY, dict(PARAMS))
        r = bt.run()
    finally:
        eng.df.fetch_quotes = orig_q
        for k, v in orig.items():
            setattr(C, k, v)
    return {"arm": label, "widx": widx, "pool": "pit" if codes else "top500",
            "total_return": r.get("total_return"),
            "max_drawdown": r.get("max_drawdown"),
            "trade_count": r.get("trade_count"), "win_rate": r.get("win_rate")}


def main():
    t0 = time.time()
    snap = ds.latest_snapshot_path()
    if not snap:
        print("无冻结快照，中止"); return
    print("[db] 冻结快照:", snap, flush=True)

    tasks = []
    for lab, patches in NEW_ARMS:
        for wi in range(4):
            tasks.append((lab, patches, wi, None))
    # PIT 对照臂：全部新增变体 + 基线语义（patches={} 在顶500上已有基线，PIT 需单跑）
    tasks.append(("baseline_pit", {}, BULL_WIDX, "__PIT__"))

    with open(os.path.join(C.DATA_DIR, "pit_pools.json"), encoding="utf-8") as f:
        pit_codes = json.load(f)["pools"]["近1年"]["codes"]
    print("[pit] 近1年 PIT 池 %d 只" % len(pit_codes), flush=True)

    results = {}
    with ProcessPoolExecutor(max_workers=7, initializer=_init_worker,
                             initargs=(snap,)) as ex:
        futs = {}
        for t in tasks:
            lab, patches, wi, pooltag = t
            if pooltag == "__PIT__":
                # PIT 臂：worker 内无法直接传大列表（pickle 开销可接受），显式构造
                futs[ex.submit(_run_one_pit, lab, patches, wi, pit_codes)] = (lab, wi)
            else:
                futs[ex.submit(_run_one, lab, patches, wi, None)] = t
        done_n = 0
        for fut in as_completed(futs):
            try:
                r = fut.result()
                key = "%s|%s|pit" % (r["arm"], r["pool"]) if r["pool"] == "pit" \
                    else None
                if key:
                    results.setdefault(key, {})[r["widx"]] = r
                else:
                    results.setdefault(r["arm"], {})[r["widx"]] = r
            except Exception as e:
                print("任务异常:", e, flush=True)
            done_n += 1
            if done_n % 6 == 0:
                print("进度 %d/%d 累计 %.0fs" % (done_n, len(tasks) + 6,
                                                time.time() - t0), flush=True)

    # ---- 合并复用臂 ----
    reused = {}
    try:
        p68 = json.load(open(P68_FILE, encoding="utf-8"))
        for lab in REUSE_LABELS:
            for wi_str, rec in p68["results"].get(lab, {}).items():
                reused.setdefault(lab, {})[int(wi_str)] = {
                    "total_return": rec.get("total_return"),
                    "max_drawdown": rec.get("max_drawdown"),
                    "trade_count": rec.get("trade_count")}
    except Exception as e:
        print("复用 P68 臂失败:", e, flush=True)

    out = {"meta": {
        "snapshot": snap, "strategy": STRATEGY, "pool_n": POOL_N, "params": PARAMS,
        "arms_new": {lab: p for lab, p in NEW_ARMS},
        "reuse": REUSE_LABELS,
        "prereg_judge": "landing_rule: >=3/4窗损失>=-1pp 且 牛市损失<=2pp"
                        "（相对同快照 baseline 臂）",
        "pit_note": "PIT 对照= pit_pools['近1年'] 成分池 × 牛市窗（现引擎现参数重跑），"
                    "用于失真校准而非静态判定",
    }, "new_results": results, "reused": reused}

    # ---- 判定 ----
    verdicts = []
    merged = {}
    merged["baseline"] = reused.get("baseline")
    for lab, _p in NEW_ARMS:
        merged[lab] = results.get(lab, {})
    for rlab in ("A1_trail_act8_stop4", "A2_trail_act12_stop5"):
        merged[rlab] = reused.get(rlab)

    candidates = []
    for lab in ["T2", "T2_A1", "T2_A2", "T3_A1_trail_act8_stop4",
                "T3_A2_trail_act12_stop5"]:
        src = ("A1_trail_act8_stop4" if lab == "T3_A1_trail_act8_stop4"
               else "A2_trail_act12_stop5" if lab == "T3_A2_trail_act12_stop5"
               else lab)
        arm = reused.get(src) if lab.startswith("T3_") else results.get(lab)
        if not arm or any(wi not in arm for wi in range(4)):
            continue
        losses = [(merged["baseline"][wi]["total_return"] or 0)
                  - (arm[wi]["total_return"] or 0) for wi in range(4)]
        bull_loss = losses[3] * 100
        sys_path = os.path.dirname(os.path.abspath(__file__))
        if sys_path not in sys.path:
            sys.path.insert(0, sys_path)
        import judge_kit as jk
        ok, detail = jk.landing_rule([x * 100 for x in losses], bull_loss)
        cand = {"variant": lab,
                "returns": [round(arm[wi]["total_return"] or 0, 4) for wi in range(4)],
                "loss_pp": [round(x * 100, 2) for x in losses],
                "sum_improve_pp": round(-sum(losses) * 100, 2),
                "pass": ok, "detail": detail}
        candidates.append(cand)
        verdicts.append(cand)
    passed = sorted([c for c in verdicts if c["pass"]],
                    key=lambda c: -c["sum_improve_pp"])
    out["verdicts"] = verdicts
    out["recommendation"] = (
        {"variant": passed[0]["variant"],
         "sum_improve_pp": passed[0]["sum_improve_pp"],
         "action": "建议落地交验收方（≤2pp 牛市门槛内全窗不恶化）"}
        if passed else
        {"variant": None, "action": "无变体通过预注册规则 → 时间止损维持 T=3"})
    tmp = OUT_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    os.replace(tmp, OUT_FILE)
    print("\n=== 复验判定 ===")
    for c in verdicts:
        print(c["variant"], "loss_pp=%s pass=%s" % (c["loss_pp"], c["pass"]))
    print("recommendation:", json.dumps(out["recommendation"], ensure_ascii=False))
    print("saved", OUT_FILE)


# PIT 臂包装：覆盖股票池
def _run_one_pit(label, patches, widx, pit_codes):
    global _G
    return _run_one(label + "|pit", patches, widx, pit_codes)


if __name__ == "__main__":
    main()
