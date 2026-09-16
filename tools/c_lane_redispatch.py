# -*- coding: utf-8 -*-
"""★ 任务3 C 通道重派（回测型）：C2 二维交互网格 / C3 高档冲高回落复验 /
C4 moneyflow 卖出信号（数据门控）/ C5 T2_A1 组合跨策略稳健性。

Phase68 方法论硬约束：
  - 数据口径：冻结快照 data/snapshots/2026-08-26/market.db（本脚本固定，不用 latest）
  - 判定（事先写死，不得事后修改）：judge_kit——≥3/4 窗口 Δ≥−1pp 且牛市恶化 ≤2pp
  - monkeypatch config 属性（worker 内改、结束恢复）；零 app/ 修改；不改 config；
    不落地任何参数（只产建议）

变体网格：
  C2 移动止损×时间止损二维交互：act{8%,12%}×stop{-4%,-5%}×T{2,3,4} 中按任务书取
     act8%/stop-4% 与 act12%/stop-5% 两列 × T{2,3,4} 共 6 格
  C3 冲高回落高档：{trig10%,back5%} / {trig12%,back6%}（B1/B2 牛市洗强票的对症加档）
  C5 T2_A1 组合：{T2 + trail8%/-4%} 在 board 策略复验（跨策略稳健性；单快照局限入报告）

C4 数据门控（探测先行）：退出触发所需「逐票主力净流向日频序列」在快照中是否存在。
  实测快照 moneyflow 仅含 kind=north（单日截面×299 票）与 rzrq（两融余额，日均覆盖
  约 98/500 且收缩）→ 不满足则如实记录 blocked 与量化缺口，不伪造实验。

用法: python tools/c_lane_redispatch.py [--workers 6]
输出: data/bt_c_lane_redispatch.json
"""
import argparse
import json
import os
import sqlite3
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SNAPSHOT = os.path.join(BASE, "data", "snapshots", "2026-08-26", "market.db")
OUT_JSON = os.path.join(BASE, "data", "bt_c_lane_redispatch.json")

WINDOWS = [["2019-01-01", "2020-12-31"], ["2021-01-01", "2022-12-31"],
           ["2023-01-01", "2024-12-31"], ["2025-08-18", "2026-08-21"]]
WIN_NAMES = ["2019-20", "2021-22", "2023-24", "牛市"]
POOL_N = 500
# 两策略各自的 Phase43/68 口径参数
PARAMS = {
    "score": {"buy_threshold": 25, "max_positions": 3, "position_pct": 0.30,
              "slippage": 0.001, "zt_eco_gate": False, "dd_gate": False},
    "board": {"buy_threshold": 40, "max_positions": 2, "position_pct": 0.25,
              "slippage": 0.001, "zt_eco_gate": False, "dd_gate": False},
}

A1 = {"TRAILING_ACTIVATE_PCT": 0.08, "TRAILING_STOP_PCT": -0.04}
A2 = {"TRAILING_ACTIVATE_PCT": 0.12, "TRAILING_STOP_PCT": -0.05}
T2_A1 = dict(A1, TIME_STOP_DAYS=2)

VARIANTS = [
    # (label, strategy, patches)
    ("base_score", "score", {}),
    ("C2_act8_T2", "score", dict(A1, TIME_STOP_DAYS=2)),
    ("C2_act8_T3", "score", dict(A1, TIME_STOP_DAYS=3)),
    ("C2_act8_T4", "score", dict(A1, TIME_STOP_DAYS=4)),
    ("C2_act12_T2", "score", dict(A2, TIME_STOP_DAYS=2)),
    ("C2_act12_T3", "score", dict(A2, TIME_STOP_DAYS=3)),
    ("C2_act12_T4", "score", dict(A2, TIME_STOP_DAYS=4)),
    ("C3_trig10_back5", "score", {"INTRADAY_HIGH_TRIGGER": 0.10, "INTRADAY_PULLBACK": 0.05}),
    ("C3_trig12_back6", "score", {"INTRADAY_HIGH_TRIGGER": 0.12, "INTRADAY_PULLBACK": 0.06}),
    ("base_board", "board", {}),
    ("C5_board_T2_A1", "board", T2_A1),
]

_G = {}


def _init_worker(snapshot):
    """worker 初始化：DB 指向冻结快照 + 池/names 载入"""
    from app import config as C
    _G["snap"] = snapshot
    C.DB_FILE = snapshot
    with open(os.path.join(C.DATA_DIR, "bt_pool.json"), encoding="utf-8") as f:
        codes = json.load(f)["codes"][:POOL_N]
    _G["codes"] = codes
    _G["names"] = {c: c for c in codes}


def _run_one(task):
    label, strategy, patches, widx = task
    from app import config as C
    from app import engine as eng
    w0, w1 = WINDOWS[widx]
    t0 = time.time()
    orig = {k: getattr(C, k) for k in patches}
    orig_q = eng.df.fetch_quotes
    eng.df.fetch_quotes = lambda cs: {}
    try:
        for k, v in patches.items():
            setattr(C, k, v)
        bt = eng.Backtest(_G["codes"], _G["names"], w0, w1, 100000.0,
                          strategy, dict(PARAMS[strategy]))
        r = bt.run()
    finally:
        eng.df.fetch_quotes = orig_q
        for k, v in orig.items():
            setattr(C, k, v)
    return {"variant": label, "strategy": strategy, "widx": widx,
            "window": WIN_NAMES[widx],
            "total_return": r.get("total_return"),
            "max_drawdown": r.get("max_drawdown"),
            "sharpe": r.get("sharpe"),
            "trade_count": r.get("trade_count"),
            "win_rate": r.get("win_rate"),
            "elapsed": round(time.time() - t0, 1)}


def c4_probe():
    """moneyflow 数据门控探测：C4 需要「逐票主力净流向日频序列」。
    返回 (available, facts)。判据（事先写死）：存在可解析为逐票净流向的 kind，
    且 ∩pool500 的日均覆盖率 ≥50%、时间跨度 ≥2 年。"""
    uri = "file:" + SNAPSHOT.replace("\\", "/") + "?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    try:
        kinds = conn.execute(
            "SELECT kind, COUNT(*), COUNT(DISTINCT code), MIN(date), MAX(date) "
            "FROM moneyflow GROUP BY kind").fetchall()
        pools = json.load(open(os.path.join(BASE, "data", "bt_pool.json"),
                               encoding="utf-8"))["codes"][:POOL_N]
        ph = ",".join("?" * len(pools))
        fact = {"requirement": "逐票主力净流向日频序列：∩pool500 日均覆盖≥50% 且跨度≥2年",
                "kinds": []}
        for kind, n, ncodes, d0, d1 in kinds:
            cov = conn.execute(
                "SELECT AVG(c) FROM (SELECT date, COUNT(DISTINCT code) c FROM moneyflow "
                "WHERE kind=? AND code IN (%s) GROUP BY date)" % ph,
                tuple([kind] + pools)).fetchone()[0]
            fact["kinds"].append({"kind": kind, "rows": n, "distinct_codes": ncodes,
                                  "date_min": d0, "date_max": d1,
                                  "avg_pool_codes_per_day": round(cov or 0, 1)})
        # 判据评估：当前已知 kind 均不是逐票主力净流向序列
        ok_kinds = [k for k in fact["kinds"]
                    if k["kind"] in ("stock", "main", "flow", "individual")]
        span_ok = [k for k in ok_kinds
                   if k["avg_pool_codes_per_day"] >= POOL_N * 0.5
                   and str(k["date_min"]) <= "2024-08"]
        fact["verdict_kinds_present"] = bool(ok_kinds)
        return bool(span_ok), fact
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args()
    t0 = time.time()

    if not os.path.isfile(SNAPSHOT):
        print("冻结快照不存在:", SNAPSHOT)
        return 1
    print("[db] 固定冻结快照:", SNAPSHOT, flush=True)

    c4_ok, c4_facts = c4_probe()
    print("[C4 门控] available=%s | %s" % (c4_ok, json.dumps(c4_facts["kinds"],
                                                       ensure_ascii=False)), flush=True)
    if c4_ok:
        raise SystemExit("C4 数据门控通过但本版未实现 stock-flow 变体（须先扩展网格），"
                         "中止以免半套实验——请更新工具后重跑")

    import judge_kit as jk   # ★ 统一判定库（Phase70）

    tasks = [(lab, strat, p, wi) for lab, strat, p in VARIANTS
             for wi in range(len(WINDOWS))]
    results = {}
    from concurrent.futures import ProcessPoolExecutor, as_completed
    ex = ProcessPoolExecutor(max_workers=a.workers, initializer=_init_worker,
                             initargs=(SNAPSHOT,))
    futs = {ex.submit(_run_one, t): t for t in tasks}
    done_n = 0
    try:
        for fut in as_completed(futs):
            t = futs[fut]
            try:
                r = fut.result()
                results.setdefault(r["variant"], {})[r["widx"]] = r
            except Exception as e:
                lab, strat, _p, wi = t
                results.setdefault(lab, {})[wi] = {
                    "variant": lab, "strategy": strat, "widx": wi,
                    "window": WIN_NAMES[wi], "error": repr(e)}
                print("cell crash [%s/%s]: %r" % (lab, WIN_NAMES[wi], e), flush=True)
            done_n += 1
            if done_n % 11 == 0:
                print("进度 %d/%d 累计 %.0fs" % (done_n, len(tasks), time.time() - t0),
                      flush=True)
    finally:
        try:
            ex.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass

    # ---- 判定（judge_kit；损失 pp = 基线收益−变体收益，正=更差）----
    judgement = []
    for lab, strat, _p in VARIANTS:
        if lab.startswith("base_"):
            continue
        base_key = "base_%s" % strat
        losses, detail, wins_ok = [], [], 0
        for wi in range(len(WINDOWS)):
            b = results.get(base_key, {}).get(wi)
            v = results.get(lab, {}).get(wi)
            if not b or not v or b.get("total_return") is None \
                    or v.get("total_return") is None:
                detail.append({"win": WIN_NAMES[wi], "error": "missing-cell"})
                continue
            loss_pp = ((b["total_return"] or 0) - (v["total_return"] or 0)) * 100
            losses.append(loss_pp)
            wins_ok += 1 if jk.window_pass([loss_pp], tol_loss_pp=1.0,
                                           min_windows=1)[0] else 0
            detail.append({"win": WIN_NAMES[wi], "ret": v["total_return"],
                           "base_ret": b["total_return"],
                           "loss_pp": round(loss_pp, 2),
                           "trades": "%s→%s" % (b.get("trade_count"),
                                                v.get("trade_count"))})
        bull_loss = losses[3] if len(losses) == 4 else None
        passed = bool(losses) and len(losses) == 4 and jk.window_pass(
            losses, tol_loss_pp=1.0, min_windows=3)[1] \
            and jk.bull_ok(bull_loss, max_loss_pp=2.0)
        judgement.append({
            "variant": lab, "strategy": strat, "detail": detail,
            "windows_ok": wins_ok,
            "bull_loss_pp": round(bull_loss, 2) if bull_loss is not None else None,
            "pass": passed})

    out = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "phase": "C-redespatch",
        "meta": {"snapshot": SNAPSHOT, "pool_n": POOL_N, "params": PARAMS,
                 "method": "Phase68：冻结快照+monkeypatch config+judge_kit 判定"
                           "（≥3/4 窗 Δ≥−1pp 且牛市恶化≤2pp，事先写死）",
                 "variants": [{"label": l, "strategy": s, "patches": p}
                              for l, s, p in VARIANTS]},
        "c4_data_gate": {"available": c4_ok, "facts": c4_facts,
                         "conclusion": None if c4_ok else
                         "快照无逐票主力净流向日频序列（north=单日截面、rzrq=两融且覆盖"
                         "<20%池），C4 无法按任务书口径验证——记录等待原因，待 fflow "
                         "类日频历史积累后再启用"},
        "results": results,
        "judgement": judgement,
        "elapsed_s": round(time.time() - t0, 1),
    }
    tmp = OUT_JSON + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    os.replace(tmp, OUT_JSON)

    print("\n===== 判定表 =====", flush=True)
    for j in judgement:
        dd = "; ".join("%s ret=%s base=%s loss=%spp" % (
            x["win"], x.get("ret"), x.get("base_ret"), x.get("loss_pp"))
            for x in j["detail"])
        print("[%s/%s] 达标窗=%s 牛市损=%spp pass=%s\n    %s" % (
            j["strategy"], j["variant"], j["windows_ok"], j["bull_loss_pp"],
            j["pass"], dd), flush=True)
    print("saved", OUT_JSON, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
