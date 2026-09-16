# -*- coding: utf-8 -*-
"""Y3 撮合现实性审计 —— 流动性参与率强制缩量重放（研究·零落地）

快照: data/snapshots/2026-08-27/market.db（只读，与 R3/W3 同快照同 196 口径）
策略: base_score（buy_threshold 25 / max_positions 3 / position_pct 0.30 / slippage 0.001 /
      zt_eco_gate False / dd_gate False / seed 42）—— 与 W3 sizing_lab S0 完全同参
窗口: 仅 2023-24（2023-01-01 ~ 2024-12-31）；池: PIT "2023-24"（pit_pools.json）过滤 196
变体（同一引擎只改 C.EXEC_PARTIAL_CAP，其余全同，seed 相同 → 差异只来自参与率上限）:
  base    = EXEC_PARTIAL_CAP=0.30（现役默认，config 原值）→ 校验应复现 W3 S0 2023-24: ret=-0.4829
  cap003  = EXEC_PARTIAL_CAP=0.03（参与率≤3% 强制缩量，买不满就少买不补单）
输出: data/bt_fill_reality.json（含两格指标 + base 的每笔参与率分布 + 判定增量）
纪律: OUT 路径断言、逐格增量落盘、幂等续跑、workers<=2、ProcessPool 断裂回退顺序、
      零生产改动（monkey-patch config.EXEC_PARTIAL_CAP，不改 app/*.py）、不出网
用法: python tools/fill_reality_lab.py [--workers 2] [--mode process|sequential]
"""
import argparse
import json
import math
import os
import sqlite3
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from app import config as C            # noqa: E402

SNAPSHOT = os.path.join(BASE, "data", "snapshots", "2026-08-27", "market.db")
OUT_JSON = os.path.join(BASE, "data", "bt_fill_reality.json")
W0, W1 = "2023-01-01", "2024-12-31"
PIT_KEY = "2023-24"
WARMUP = 260
PARAMS_SCORE = {"buy_threshold": 25, "max_positions": 3, "position_pct": 0.30,
                "slippage": 0.001, "zt_eco_gate": False, "dd_gate": False}
CAPS = {"base": 0.30, "cap003": 0.03}
_G = {}


def _filtered_pool(codes):
    excl = set(getattr(C, "DATA_EXCLUDE_CODES", []) or [])
    return [c for c in codes if c not in excl]


def _init_worker(snapshot):
    from app import engine as eng
    _G["snap"] = snapshot
    C.DB_FILE = snapshot
    pit = json.load(open(os.path.join(C.DATA_DIR, "pit_pools.json"), encoding="utf-8"))
    _G["pit_pools"] = pit["pools"]
    # 实时行情置空（引擎基线惯例，不出网）
    eng.df.fetch_quotes = lambda cs: {}


def _day_amount_map(codes):
    """code -> {date: amount}（只读快照 kline，供参与率分母）"""
    m = {}
    conn = sqlite3.connect("file:%s?mode=ro" % SNAPSHOT, uri=True, timeout=20)
    try:
        ph = ",".join("?" * len(codes))
        rows = conn.execute(
            "SELECT code, date, amount FROM kline WHERE period='day' AND code IN (%s) "
            "AND date>=? AND date<=?" % ph, tuple(codes) + (W0, W1)).fetchall()
    finally:
        conn.close()
    for c, d, a in rows:
        m.setdefault(c, {})[d] = a or 0
    return m


def _run_one(cell):
    from app import engine as eng
    cap = CAPS[cell]
    C.EXEC_PARTIAL_CAP = cap            # 运行时注入，零生产改动
    codes = _filtered_pool(_G["pit_pools"][PIT_KEY]["codes"])
    names = {c: c for c in codes}
    t0 = time.time()
    bt = eng.Backtest(codes, names, W0, W1, 100000.0, "score", dict(PARAMS_SCORE))
    r = bt.run()
    eq = [e["equity"] for e in bt.equity]
    n = len(eq)
    buys = [t for t in bt.trades if t["side"] == "buy"]
    sells = [t for t in bt.trades if t["side"] == "sell"]
    cash_mean = sum(e["cash"] for e in bt.equity) / n if n else 0.0
    out = {"cell": cell, "cap": cap, "window": "2023-24", "pit_key": PIT_KEY,
           "n_codes": len(codes),
           "total_return": r.get("total_return"), "max_drawdown": r.get("max_drawdown"),
           "annual_return": r.get("annual_return"), "sharpe": r.get("sharpe"),
           "win_rate": r.get("win_rate"), "trade_count": r.get("trade_count"),
           "n_buys": len(buys), "n_sells": len(sells),
           "failed_fills": bt.failed_fills, "cash_mean": round(cash_mean, 2),
           "elapsed": round(time.time() - t0, 1)}
    # 参与率分布（仅 base 格需要；cap003 缩量后分布无意义）
    if cell == "base":
        dam = _day_amount_map(codes)
        ratios = []
        for t in buys:
            d = t.get("date")
            a = t.get("amount") or 0
            da = (dam.get(t["code"]) or {}).get(d, 0)
            ratios.append({"date": d, "code": t["code"], "buy_amount": round(a, 2),
                           "day_amount": da,
                           "ratio": round(a / da, 4) if da > 0 else None})
        rr = [x["ratio"] for x in ratios if x["ratio"] is not None]
        rr_sorted = sorted(rr)
        def pct(p):
            if not rr_sorted:
                return None
            i = int(math.ceil(p / 100.0 * len(rr_sorted))) - 1
            return round(rr_sorted[max(0, min(i, len(rr_sorted) - 1))], 4)
        out["participation"] = {
            "n_ratios": len(rr),
            "p50": pct(50), "p90": pct(90), "p95": pct(95),
            "max": round(rr_sorted[-1], 4) if rr_sorted else None,
            "gt_1pct": round(100.0 * sum(1 for x in rr if x > 0.01) / len(rr), 2) if rr else None,
            "gt_3pct": round(100.0 * sum(1 for x in rr if x > 0.03) / len(rr), 2) if rr else None,
            "gt_5pct": round(100.0 * sum(1 for x in rr if x > 0.05) / len(rr), 2) if rr else None,
            "at_cap30": sum(1 for x in rr if x >= 0.299),   # 截断于 30% 上限的笔数（真实拟参与率更高）
            "no_day_amount": len(ratios) - len(rr),
        }
    return out


def _dump(out):
    assert os.path.basename(OUT_JSON) == "bt_fill_reality.json", OUT_JSON
    assert "sizing" not in OUT_JSON and "overnight" not in OUT_JSON and "pit" not in OUT_JSON, OUT_JSON
    tmp = OUT_JSON + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    os.replace(tmp, OUT_JSON)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--mode", choices=["process", "sequential"], default="process")
    a = ap.parse_args()
    t0 = time.time()
    if not os.path.isfile(SNAPSHOT):
        print("缺冻结快照:", SNAPSHOT, flush=True)
        return 1
    _init_worker(SNAPSHOT)
    print("[db] 快照:", SNAPSHOT, flush=True)
    print("[pit/%s] filtered196=%d" % (PIT_KEY, len(_filtered_pool(_G["pit_pools"][PIT_KEY]["codes"]))), flush=True)
    print("[prereg] 档位=1%%/3%%/5%%当日成交额；重放=2023-24窗 S0 cap=3%%；判据见报告§0", flush=True)
    print("[caps]", CAPS, flush=True)

    out = {}
    if os.path.isfile(OUT_JSON):
        try:
            out = json.load(open(OUT_JSON, encoding="utf-8"))
        except Exception:
            out = {}
    results = out.get("results") or {}
    pending = [c for c in CAPS if c not in results or results[c].get("error")]
    print("[resume] done=%s pending=%s" % (list(results.keys()), pending), flush=True)

    def _save():
        full = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "task": "Y3 撮合现实性审计：参与率强制缩量重放（2023-24窗 S0，零落地）",
                "snapshot": SNAPSHOT, "window": "2023-24", "pit_key": PIT_KEY,
                "params_score": PARAMS_SCORE, "caps": CAPS, "results": results}
        _dump(full)

    done_n = 0
    if a.mode == "process" and pending:
        from concurrent.futures import ProcessPoolExecutor, as_completed
        from concurrent.futures.process import BrokenProcessPool
        try:
            ex = ProcessPoolExecutor(max_workers=min(a.workers, 2),
                                     initializer=_init_worker, initargs=(SNAPSHOT,))
            futs = {ex.submit(_run_one, c): c for c in pending}
            for fut in as_completed(futs):
                c = futs[fut]
                try:
                    r = fut.result()
                    results[c] = r
                    _save()
                    print("完成 [%s] cap=%.2f ret=%.4f mdd=%.4f trades=%s failfill=%s (%.0fs)" % (
                        c, r["cap"], r["total_return"] or 0, r["max_drawdown"] or 0,
                        r["trade_count"], r["failed_fills"], r["elapsed"]), flush=True)
                except Exception as e:
                    import traceback
                    traceback.print_exc()
                    results[c] = {"cell": c, "error": repr(e)}
                    _save()
                    print("cell crash [%s]: %r" % (c, e), flush=True)
                done_n += 1
            ex.shutdown(wait=True)
        except BrokenProcessPool:
            print("!! ProcessPool 断裂，回退顺序执行", flush=True)
            for c in [x for x in pending if x not in results or results[x].get("error")]:
                try:
                    r = _run_one(c)
                    results[c] = r
                    _save()
                    print("完成(seq) [%s] ret=%.4f (%.0fs)" % (c, r["total_return"] or 0, r["elapsed"]), flush=True)
                except Exception as e:
                    results[c] = {"cell": c, "error": repr(e)}
                    _save()
                    print("cell crash(seq) [%s]: %r" % (c, e), flush=True)
    else:
        for c in pending:
            try:
                r = _run_one(c)
                results[c] = r
                _save()
                print("完成 [%s] cap=%.2f ret=%.4f mdd=%.4f trades=%s failfill=%s (%.0fs)" % (
                    c, r["cap"], r["total_return"] or 0, r["max_drawdown"] or 0,
                    r["trade_count"], r["failed_fills"], r["elapsed"]), flush=True)
            except Exception as e:
                import traceback
                traceback.print_exc()
                results[c] = {"cell": c, "error": repr(e)}
                _save()
                print("cell crash [%s]: %r" % (c, e), flush=True)

    # 增量
    if "base" in results and "cap003" in results and not results["base"].get("error") and not results["cap003"].get("error"):
        b, v = results["base"], results["cap003"]
        delta = {"ret_pp": round((b["total_return"] - v["total_return"]) * 100, 2),
                 "mdd_pp": round((v["max_drawdown"] - b["max_drawdown"]) * 100, 2),
                 "trades_delta": (v["trade_count"] or 0) - (b["trade_count"] or 0),
                 "failed_fill_delta": (v["failed_fills"] or 0) - (b["failed_fills"] or 0),
                 "cash_mean_delta": round((v["cash_mean"] or 0) - (b["cash_mean"] or 0), 2)}
        results["delta"] = delta
        _save()
        print("\n===== 判定增量（base − cap003） =====", flush=True)
        print("Δret=%spp ΔMDD=%spp Δtrades=%s Δfailed_fill=%s Δcash_mean=%s" % (
            delta["ret_pp"], delta["mdd_pp"], delta["trades_delta"],
            delta["failed_fill_delta"], delta["cash_mean_delta"]), flush=True)
    full = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "task": "Y3 撮合现实性审计：参与率强制缩量重放（2023-24窗 S0，零落地）",
            "snapshot": SNAPSHOT, "window": "2023-24", "pit_key": PIT_KEY,
            "params_score": PARAMS_SCORE, "caps": CAPS, "results": results}
    _dump(full)
    print("saved", OUT_JSON, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
