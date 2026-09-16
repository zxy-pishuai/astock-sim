# -*- coding: utf-8 -*-
"""★ Phase44：PORTFOLIO_METHOD 对照 —— 默认在跑，从未被验证

config.PORTFOLIO_METHOD="risk_parity" 是默认值，但从未与等权对照过。
本工具零代码修改：
  - 方法来源确认：engine.Backtest.__init__ 读 params["portfolio_method"]
    （缺省回退 config.PORTFOLIO_METHOD）→ worker 内直接用 params 切换；
    权重记录器以 monkeypatch 挂在 app.portfolio.compute_weights 上
    （worker 进程内运行期替换属性，结束恢复，不改源码）
  - 网格：method ∈ {equal, risk_parity(基线), hrp, vol_target} × {score, board}
    × 4 窗口 = 32 组；配方同 Phase21/22/25/26/30（池500、board 镜像实盘参数、
    闸门全关、fetch_quotes 置空、seed=42），risk_parity 组须精确复现基线
  - 记录：收益 / 最大回撤 / Calmar / 实际单仓权重分布
    （vol_target 的总和收缩幅度、risk_parity 的权重集中度 HHI）

判定（事先写死）：某方法使两策略合计 ≥6/8 组"改善或持平"（Δ≥-1pp），
且无单组恶化 >3pp → 建议换该方法（只写报告，不改 config）；
否则维持 risk_parity。

用法: python tools/portfolio_method_ab.py [--analyze-only]
输出: data/bt_portfolio_ab.json
"""
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
BOARD_PARAMS = {"buy_threshold": 40, "max_positions": 2,
                "position_pct": 0.25, "slippage": 0.001}
BASE_PARAMS = {"zt_eco_gate": False, "dd_gate": False}
METHODS = ["equal", "risk_parity", "hrp", "vol_target"]
BASELINE_METHOD = "risk_parity"
OUT_JSON = os.path.join(BASE, "data", "bt_portfolio_ab.json")

# 基线目标（bt_clean_results.json）：[score 4窗, board 4窗]
BASELINE_TARGETS = {
    ("score", 0): -0.0765, ("score", 1): -0.2159,
    ("score", 2): -0.3695, ("score", 3): 0.3068,
    ("board", 0): -0.1177, ("board", 1): -0.0665,
    ("board", 2): -0.0166, ("board", 3): 0.2730,
}

TASKS = [(m, s, w) for m in METHODS for s in ("score", "board")
         for w in range(len(WINDOWS))]


def build_pool():
    from app import config as C
    with open(os.path.join(C.DATA_DIR, "bt_pool.json"), encoding="utf-8") as f:
        pool = json.load(f)
    codes = list(pool["codes"])[:POOL_SIZE]
    return codes, {c: c for c in codes}


def _weight_stats(records, pos_pct):
    """实际单仓权重分布统计。
    引擎落仓规则：min(max(w, cap), pos_pct)——统计里同时给出
    "触顶占比"（w>=pos_pct，即与固定比例等效的交易占比）。"""
    n = len(records)
    if n == 0:
        return {"weight_calls": 0}
    sums, maxes, hhis, capped = [], [], [], 0
    for w in records:
        vals = list(w.values())
        s = sum(vals)
        sums.append(s)
        maxes.append(max(vals) if vals else 0.0)
        hhis.append(sum(v * v for v in vals))
        if vals and min(vals) >= pos_pct * 0.999:
            capped += 1   # 全部成员权重>=pos_pct → 与固定比例完全等效
    return {
        "weight_calls": n,
        "avg_members": round(sum(len(w) for w in records) / n, 2),
        "avg_sum_w": round(sum(sums) / n, 4),
        "avg_max_w": round(sum(maxes) / n, 4),
        "avg_hhi": round(sum(hhis) / n, 4),      # 集中度：1/n 等权 ~ 0.33(3成员)
        "all_capped_ratio": round(capped / n, 4),  # 与固定比例等效的调用占比
        "note": "引擎落仓=min(w,pos_pct)；sum_w<1 仅 vol_target 可能出现（降仓）",
    }


def run_one(task):
    """worker 入口：单方法×策略×窗口回测 + 权重记录"""
    method, strategy, widx = task
    from app import config as C
    from app import engine as eng
    from app import portfolio as port
    w0, w1 = WINDOWS[widx]
    t0 = time.time()

    orig_quotes = eng.df.fetch_quotes
    orig_cw = port.compute_weights
    records = []

    def recorded_compute(closes_by_code, **kw):
        w = orig_cw(closes_by_code, **kw)
        records.append(dict(w))
        return w

    eng.df.fetch_quotes = lambda cs: {}
    port.compute_weights = recorded_compute     # monkeypatch：仅本 worker 进程内
    try:
        params = dict(BOARD_PARAMS) if strategy == "board" else {}
        params.update(BASE_PARAMS)
        params["portfolio_method"] = method
        pos_pct = params.get("position_pct", C.POSITION_PCT)
        bt = eng.Backtest(codes, names, w0, w1, 100000.0, strategy, params)
        r = bt.run()
    except Exception as e:
        return {"method": method, "strategy": strategy, "widx": widx,
                "window": [w0, w1], "error": str(e)[:120]}
    finally:
        eng.df.fetch_quotes = orig_quotes
        port.compute_weights = orig_cw          # 恢复

    ann, dd = r.get("annual_return") or 0.0, r.get("max_drawdown") or 0.0
    row = {
        "method": method, "strategy": strategy, "widx": widx,
        "window": [w0, w1],
        "total_return": r.get("total_return"),
        "annual_return": r.get("annual_return"),
        "max_drawdown": r.get("max_drawdown"),
        "calmar": round(ann / abs(dd), 3) if dd else None,
        "win_rate": r.get("win_rate"),
        "sharpe": r.get("sharpe"),
        "trade_count": len(bt.trades),
        "weights": _weight_stats(records, pos_pct),
        "elapsed": round(time.time() - t0, 1),
    }
    print("  [%s/%s %s] ret=%.4f dd=%.4f calmar=%s (%.0fs)" % (
        method, strategy, WINDOW_TAGS[widx], row["total_return"] or 0,
        dd, row["calmar"], row["elapsed"]), flush=True)
    return row


codes = names = None   # worker 全局池（spawn 后各进程初始化一次）


def _init_worker():
    global codes, names
    codes, names = build_pool()


def _run_task(task):
    global codes, names
    if codes is None:
        _init_worker()
    return run_one(task)


# ============ 主流程 ============
def _dump(runs, extra=None):
    payload = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "phase": "44",
        "recipe": {
            "pool": "data/bt_pool.json 前%d只, names=代码兜底" % POOL_SIZE,
            "windows": WINDOWS,
            "methods": METHODS,
            "baseline": BASELINE_METHOD,
            "board_params": BOARD_PARAMS,
            "gates_off": BASE_PARAMS,
            "quotes_patch": "fetch_quotes置空",
            "seed": 42,
            "method_switch": "params['portfolio_method']（engine 原生支持）；"
                             "权重记录=monkeypatch app.portfolio.compute_weights（worker 内）",
        },
        "runs": runs,
    }
    if extra:
        payload.update(extra)
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)


def judge(runs):
    """事先写死的判定：Δ 相对同策略同窗口 risk_parity 基线"""
    base = {(r["strategy"], r["widx"]): r["total_return"]
            for r in runs if r["method"] == BASELINE_METHOD and "error" not in r}
    verdicts = {}
    for m in METHODS:
        if m == BASELINE_METHOD:
            continue
        rows = [r for r in runs if r["method"] == m and "error" not in r]
        deltas = []
        for r in sorted(rows, key=lambda x: (x["strategy"], x["widx"])):
            b = base.get((r["strategy"], r["widx"]))
            if b is None or r["total_return"] is None:
                continue
            deltas.append({"strategy": r["strategy"], "widx": r["widx"],
                           "delta_pp": round((r["total_return"] - b) * 100, 2)})
        good = sum(1 for d in deltas if d["delta_pp"] >= -1.0)
        worst = min((d["delta_pp"] for d in deltas), default=None)
        ok = len(deltas) >= 8 and good >= 6 and worst is not None and worst > -3.0
        verdicts[m] = {
            "deltas": deltas,
            "improved_or_flat": good,
            "of": len(deltas),
            "worst_delta_pp": worst,
            "switch_recommended": ok,
        }
    any_switch = [m for m, v in verdicts.items() if v["switch_recommended"]]
    return {"per_method": verdicts,
            "rule": "合计>=6/8组改善或持平(Δ>=-1pp) 且 无单组恶化>3pp",
            "switch_to": any_switch[0] if any_switch else None,
            "keep_risk_parity": not any_switch}


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--analyze-only", action="store_true")
    a = ap.parse_args()
    t0 = time.time()
    os.makedirs(os.path.dirname(OUT_JSON), exist_ok=True)

    if a.analyze_only:
        with open(OUT_JSON, encoding="utf-8") as f:
            runs = json.load(f)["runs"]
        print("analyze-only: 复用 %d 组结果" % len(runs), flush=True)
    else:
        from concurrent.futures import ProcessPoolExecutor, as_completed
        runs = []
        print("网格：%d 方法 × 2 策略 × %d 窗口 = %d 组（并行 8 进程）" % (
            len(METHODS), len(WINDOWS), len(TASKS)), flush=True)
        ex = ProcessPoolExecutor(max_workers=8, initializer=_init_worker)
        futs = {ex.submit(_run_task, t): t for t in TASKS}
        done_cnt = 0
        try:
            for fut in as_completed(futs):
                t = futs[fut]
                try:
                    row = fut.result()
                except Exception as e:
                    print("任务异常 %s: %s" % (t, e), flush=True)
                    continue
                runs.append(row)
                _dump(sorted(runs, key=lambda r: (
                    METHODS.index(r["method"]), r["strategy"], r["widx"])))
                done_cnt += 1
                print("进度 %d/%d 完成，累计 %.0fs" % (
                    done_cnt, len(TASKS), time.time() - t0), flush=True)
        finally:
            try:
                ex.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass
        # 缺漏补跑（顺序）
        done_keys = {(r["method"], r["strategy"], r["widx"]) for r in runs}
        for t in TASKS:
            if t not in done_keys:
                print("补跑 %s" % (t,), flush=True)
                _init_worker()
                runs.append(_run_task(t))
                _dump(sorted(runs, key=lambda r: (
                    METHODS.index(r["method"]), r["strategy"], r["widx"])))
        order = {tt: i for i, tt in enumerate(TASKS)}
        runs.sort(key=lambda r: order.get(
            (r["method"], r["strategy"], r.get("widx")), 99))

    verdict = judge(runs)
    # 基线复现校验
    mism = []
    for r in runs:
        if r["method"] == BASELINE_METHOD and "error" not in r:
            tgt = BASELINE_TARGETS.get((r["strategy"], r["widx"]))
            if tgt is not None and abs((r["total_return"] or 0) - tgt) > 0.0005:
                mism.append((r["strategy"], r["widx"], r["total_return"], tgt))
    extra = {
        "verdict": verdict,
        "baseline_check": {"ok": not mism, "mismatch": [
            {"strategy": s, "widx": w, "got": g, "target": t} for s, w, g, t in mism]},
        "elapsed_sec": round(time.time() - t0, 1),
    }
    _dump(runs, extra)

    # 终端摘要
    print("\n===== PORTFOLIO_METHOD 对照摘要 =====")
    grid = {(r["method"], r["strategy"], r["widx"]): r for r in runs
            if "error" not in r}
    hdr = "%-12s" + "%-14s" * len(WINDOWS)
    for strategy in ("score", "board"):
        print("[%s]" % strategy)
        print(hdr % tuple(["method"] + WINDOW_TAGS))
        for m in METHODS:
            cells = []
            for wi in range(len(WINDOWS)):
                r = grid.get((m, strategy, wi))
                cells.append("%.2f%%" % ((r["total_return"] or 0) * 100) if r else "-")
            print(hdr % tuple([m] + cells))
    for m, v in verdict["per_method"].items():
        ds = " ".join("%+.2f" % d["delta_pp"] for d in v["deltas"])
        print("%s: Δ(pp) %s | 达标组 %d/%d | 最差 %+.2fpp | %s" % (
            m, ds, v["improved_or_flat"], v["of"], v["worst_delta_pp"],
            "→建议换用" if v["switch_recommended"] else "→不换"))
    print("结论:", "建议切换到 %s" % verdict["switch_to"] if verdict["switch_to"]
          else "维持 risk_parity（默认选择站得住）")
    print("基线复现:", "一致 ✓" if not mism else "不一致 ✗ %s" % mism[:3])
    print("已写出 %s（耗时 %.0fs）" % (OUT_JSON, time.time() - t0), flush=True)


if __name__ == "__main__":
    main()
