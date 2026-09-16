# -*- coding: utf-8 -*-
"""★ B3：score_stock 负 IC 纯减法实验 —— 同日 A/B + judge_kit 判定 + PIT 双列

背景链路：
  P28-A factor_ic.md：18 个 K 线成分 IC 审计 → 1 有效(vp_divergence)/5 弱/12 负
  P28-B factor_reweight.md：已测「清零+幸存者∝IC 重配」合并臂（1/4 改善、牛窗
        -37.65pp → 不启用）；但其 A 臂复用历史基线、非同日重跑，且剔除与权重
        重配两变量混杂。
  本阶段 B3：**纯减法**（负 IC 权重→0，预算不重配、阈值不动）经 P66 入口
  （config.FACTOR_WEIGHT_OVERRIDE，scoring.py 单一应用点）做同日 A/B：
    base 臂：FACTOR_WEIGHT_OVERRIDE=None（现状）
    sub  臂：W=dict(SCORE_WEIGHTS)，data/factor_ic_audit.json 中 class==负 的 key→0
  两臂除权重字典外全部相同（同日、同池、同参、fetch_quotes 置空、gate 关）。

判定（事先写死，judge_kit.landing_rule）：损失 pp = 基线收益−减法臂收益（正=更差）；
≥3/4 窗口损失 ≤+1pp 且 牛市窗口损失 ≤+2pp → 建议；否则维持 None。
PIT 双列：产出仅单臂 runs 的派生 JSON，交 tools/report_pit_columns.py 出
静态池 vs PIT 池对照段（P72 机制）。

用法: python tools/b3_score_subtract_ab.py [--workers 4]
输出: data/bt_b3_score_subtract.json（含 pit 输入派生文件 * _pit_base/_pit_sub.json）
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

from app import config as C          # noqa: E402
from app import engine as eng        # noqa: E402
from judge_kit import landing_rule, window_pass, bull_ok, rel_mdd_improve  # noqa: E402

AUDIT_JSON = os.path.join(BASE, "data", "factor_ic_audit.json")
OUT_JSON = os.path.join(BASE, "data", "bt_b3_score_subtract.json")
WINDOWS = [
    ["2019-01-01", "2020-12-31"],
    ["2021-01-01", "2022-12-31"],
    ["2023-01-01", "2024-12-31"],
    ["2025-08-18", "2026-08-26"],
]
WINDOW_TAGS = ["2019-20", "2021-22", "2023-24", "近1年"]
STRAT_PARAMS = {"buy_threshold": 25, "max_positions": 3, "position_pct": 0.30,
                "slippage": 0.001}

# ---- 模块级构建减法字典（spawn 子进程随模块加载自动获得）----
_audit = json.load(open(AUDIT_JSON, encoding="utf-8"))
NEG_KEYS = sorted(r["key"] for r in _audit["components"] if r.get("class") == "负")
SUB_W = dict(C.SCORE_WEIGHTS)
for _k in NEG_KEYS:
    if _k in SUB_W:
        SUB_W[_k] = 0


def _run(task):
    """单格回测 worker。task=(arm, widx, codes)。★模块级（Windows spawn 可 pickle）"""
    arm, widx, codes = task
    w0, w1 = WINDOWS[widx]
    t0 = time.time()
    orig_quotes = eng.df.fetch_quotes
    orig_ov = getattr(C, "FACTOR_WEIGHT_OVERRIDE", None)
    eng.df.fetch_quotes = lambda cs: {}       # 确定性口径（Phase25 先例）
    try:
        params = dict(STRAT_PARAMS)
        params.update({"zt_eco_gate": False, "dd_gate": False})
        if arm == "sub":
            C.FACTOR_WEIGHT_OVERRIDE = SUB_W   # ★P66 入口（进程内覆盖，不改 config）
        names = {c: c for c in codes}
        bt = eng.Backtest(codes, names, w0, w1, 100000.0, "score", params)
        r = bt.run()
    finally:
        eng.df.fetch_quotes = orig_quotes
        C.FACTOR_WEIGHT_OVERRIDE = orig_ov
    row = {"arm": arm, "strategy": "score", "widx": widx, "window": [w0, w1],
           "total_return": r.get("total_return"),
           "annual_return": r.get("annual_return"),
           "max_drawdown": r.get("max_drawdown"),
           "sharpe": r.get("sharpe"),
           "trade_count": r.get("trade_count"),
           "elapsed": round(time.time() - t0, 1)}
    if "error" in r:
        row["error"] = str(r["error"])
        print("  [%s %s] ERROR %s" % (arm, WINDOW_TAGS[widx], row["error"]), flush=True)
        return row
    print("  [%s %-7s] ret=%+.4f trades=%s (%.0fs)"
          % (arm, WINDOW_TAGS[widx], row["total_return"] or 0,
             row["trade_count"], row["elapsed"]), flush=True)
    return row


def _override_bite_check():
    """合成数据自检：sub 臂权重字典确实生效（触发量价齐升时分数应下降）。"""
    from app import scoring as sc
    def bars(pct_last):
        ks = []
        px = 10.0
        for i in range(80):
            c = px * (1.0 + pct_last if i == 79 else 0.001)
            ks.append({"date": "d%02d" % i, "open": px, "high": max(px, c) * 1.001,
                       "low": min(px, c) * 0.999, "close": c,
                       "volume": 1000.0 * (6.0 if i == 79 else 1.0)})
            px = c
        return ks
    orig = C.FACTOR_WEIGHT_OVERRIDE
    try:
        C.FACTOR_WEIGHT_OVERRIDE = None
        s0, sig0 = sc.score_stock(bars(0.03))
        C.FACTOR_WEIGHT_OVERRIDE = SUB_W
        s1, sig1 = sc.score_stock(bars(0.03))
    finally:
        C.FACTOR_WEIGHT_OVERRIDE = orig
    return {"base_score": s0, "sub_score": s1, "bites": bool(s1 < s0),
            "base_signals": len(sig0), "sub_signals": len(sig1)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()
    t0 = time.time()

    bite = _override_bite_check()
    print("覆盖入口自检: %s" % json.dumps(bite, ensure_ascii=False), flush=True)
    assert bite["bites"], "FACTOR_WEIGHT_OVERRIDE 未生效，禁止继续"

    from tools.backtest_zt_eco import build_pool
    codes, names = build_pool()
    codes = codes[:500]
    print("池: %d 只 | 减法成分 %d 个: %s" % (len(codes), len(NEG_KEYS), NEG_KEYS),
          flush=True)

    tasks = [("base" if arm == "base" else "sub", w, codes)
             for arm in ("base", "sub") for w in range(len(WINDOWS))]
    runs = []
    from concurrent.futures import ProcessPoolExecutor, as_completed
    ex = ProcessPoolExecutor(max_workers=a.workers)
    futs = {ex.submit(_run, t): t for t in tasks}
    try:
        for fut in as_completed(futs):
            t = futs[fut]
            try:
                runs.append(fut.result())
            except Exception as e:
                runs.append({"arm": t[0], "strategy": "score", "widx": t[1],
                             "window": WINDOWS[t[1]], "error": repr(e)})
                print("  worker crash [%s/%d]: %r" % (t[0], t[1], e), flush=True)
    finally:
        try:
            ex.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass

    grid = {(r["arm"], r["widx"]): r for r in runs}
    base_rows, sub_rows, losses, table = [], [], [], []
    ok = True
    for wi, tag in enumerate(WINDOW_TAGS):
        b, s = grid.get(("base", wi), {}), grid.get(("sub", wi), {})
        br, sr = b.get("total_return"), s.get("total_return")
        loss = round(((br or 0) - (sr or 0)) * 100, 2) \
            if (br is not None and sr is not None) else None
        if loss is None:
            ok = False
        losses.append(loss)
        mdd_imp = rel_mdd_improve(b.get("max_drawdown"), s.get("max_drawdown")) \
            if loss is not None else None
        table.append({"window": tag, "base_ret": br, "sub_ret": sr,
                      "loss_pp": loss, "base_trades": b.get("trade_count"),
                      "sub_trades": s.get("trade_count"),
                      "rel_mdd_improve": round(mdd_imp, 4) if mdd_imp is not None else None})
        base_rows.append(b)
        sub_rows.append(s)

    passed, rule_detail = False, None
    if ok:
        passed, rule_detail = landing_rule(losses, losses[3])
    doc = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
           "phase": "B3",
           "design": {"mode": "纯减法（负IC权重→0，不重配预算、不改阈值）",
                      "entry": "scoring.FACTOR_WEIGHT_OVERRIDE（P66，进程内覆盖）",
                      "negative_keys": NEG_KEYS,
                      "audit_source": AUDIT_JSON,
                      "bite_check": bite},
           "windows": WINDOWS, "params": STRAT_PARAMS,
           "table": table, "runs": runs,
           "losses_pp": losses,
           "verdict": {"rule": "landing_rule: >=3/4 窗口损失<=1pp 且 牛市窗口<=2pp",
                       "passed": passed,
                       "detail": rule_detail,
                       "recommendation": ("建议启用（另走 config 落地审批）" if passed
                                          else "维持 FACTOR_WEIGHT_OVERRIDE=None 不启用")},
           }
    with open(OUT_JSON, "w", encoding="utf-8", newline="\n") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    # PIT 双列输入（P72 report_pit_columns 对 (strategy,widx) 去重 → 各臂单独出文件）
    for arm, rowsx in (("base", [r for r in runs if r.get("arm") == "base"]),
                       ("sub", [r for r in runs if r.get("arm") == "sub"])):
        p = OUT_JSON.replace(".json", "_pit_%s.json" % arm)
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            json.dump({"runs": rowsx}, f, ensure_ascii=False, indent=1)
        print("PIT 双列输入:", p, flush=True)

    print("\n===== 同日 A/B 对照 =====", flush=True)
    for t in table:
        print("  %-8s base=%s sub=%s 损失=%spp trades %s→%s" % (
            t["window"], t["base_ret"], t["sub_ret"], t["loss_pp"],
            t["base_trades"], t["sub_trades"]))
    print("判定:", doc["verdict"]["recommendation"],
          "| landing_rule detail:", rule_detail, flush=True)
    print("已写出 %s（%.0fs）" % (OUT_JSON, time.time() - t0), flush=True)


if __name__ == "__main__":
    main()
