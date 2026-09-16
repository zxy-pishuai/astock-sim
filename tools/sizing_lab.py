# -*- coding: utf-8 -*-
"""W3 仓位科学实验室 —— vol-target / ATR 风险预算 vs 现行 3×0.30（研究·重回测·零落地）

快照: data/snapshots/2026-08-27/market.db（与 R3/act12 同快照同 196 口径）
策略: base_score（buy_threshold 25 / max_positions 3 / position_pct 0.30 / slippage 0.001 /
      zt_eco_gate False / dd_gate False）—— 只换仓位规则，选股/退出/费用不变
变体:
  S0 基线   = 原生 portfolio_method=risk_parity（config 默认，与 R3 base 同口径，不做 monkey-patch）
  S1 vol-target   = weight=min(0.30, (0.15/3)/sqrt(244) / atr_daily)，atr_daily=ATR14/close
  S2 风险预算止损联动 = atr_scale=clamp(atr_daily/0.025,0.5,2.0); stop=0.05*atr_scale;
                      weight=min(0.20, 0.01/stop)
  S3 逆 ATR 配权  = weight_i=0.90*(1/atr_i)/Σ(1/atr_j)，cap 0.30
池: PIT 为主（pit_pools.json 每窗 500 − 196 → 455/434/426/445）；省格理由：多代理并发算力减半，
    只做 PIT 单臂 16 格（4 变体 × 4 窗），不做 static 对照。
注入方式（不改 app/*.py）: monkey-patch app.portfolio.compute_weights —— method 为
    "w3_s1/w3_s2/w3_s3" 时返回 ATR 权重（ATR 从冻结快照只读预计算，PIT：只用信号日 prev_date
    及之前的收盘/高低价），其余 method 委托原函数。S0 不触发 patch（原生 risk_parity）。
纪律（继承 R1 act12_t4_pit.py 骨架）: OUT 路径断言、逐格增量落盘、幂等续跑、workers≤2、
    ProcessPool 断裂回退顺序执行。
用法: python tools/sizing_lab.py [--workers 2] [--mode process|sequential] [--limit N]
输出: data/bt_sizing_lab.json
"""
import argparse
import bisect
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
from app import portfolio as _port     # noqa: E402
_ORIG_COMPUTE_WEIGHTS = _port.compute_weights   # 捕获原函数（patch 后委托它，防递归）

SNAPSHOT = os.path.join(BASE, "data", "snapshots", "2026-08-27", "market.db")
OUT_JSON = os.path.join(BASE, "data", "bt_sizing_lab.json")
WINDOWS = [["2019-01-01", "2020-12-31"], ["2021-01-01", "2022-12-31"],
           ["2023-01-01", "2024-12-31"], ["2025-08-18", "2026-08-21"]]
WIN_NAMES = ["2019-20", "2021-22", "2023-24", "牛市"]
PIT_WIN_MAP = ["2019-20", "2021-22", "2023-24", "近1年"]
WARMUP = 260            # engine 默认 warmup_days
LOAD_DAYS_NEEDED = WARMUP + 60
PARAMS_SCORE = {"buy_threshold": 25, "max_positions": 3, "position_pct": 0.30,
                "slippage": 0.001, "zt_eco_gate": False, "dd_gate": False}
VARIANTS = ["s0", "s1", "s2", "s3"]

_G = {}


# ---------------- ATR 预计算（只读冻结快照，PIT） ----------------
def _load_code_bars(code):
    """读 code 全史有效日K（date/open/high/low/close），缓存。返回 (dates, atr14s)。"""
    if code in _G.get("bars_cache", {}):
        return _G["bars_cache"][code]
    if "bars_cache" not in _G:
        _G["bars_cache"] = {}
    conn = sqlite3.connect("file:%s?mode=ro" % SNAPSHOT, uri=True, timeout=20)
    try:
        rows = conn.execute(
            "SELECT date, open, high, low, close FROM kline "
            "WHERE period='day' AND code=? ORDER BY date", (code,)).fetchall()
    finally:
        conn.close()
    dates, highs, lows, closes = [], [], [], []
    prev_close = None
    trs = []
    atrs = []
    for d, o, h, l, cl in rows:
        if cl is None or cl <= 0 or h is None or l is None:
            prev_close = None
            continue
        dates.append(d)
        highs.append(h); lows.append(l); closes.append(cl)
        if prev_close is not None and prev_close > 0:
            tr = max(h - l, abs(h - prev_close), abs(l - prev_close))
            trs.append(tr)
            if len(trs) > 14:
                trs.pop(0)
            atrs.append(sum(trs) / len(trs) if len(trs) >= 14 else None)
        else:
            trs = []
            atrs.append(None)
        prev_close = cl
    # 对齐：dates[i] ↔ atrs[i]
    _G["bars_cache"][code] = (dates, atrs)
    return dates, atrs


def _w3_compute_weights(closes_by_code, method="risk_parity", target_vol=0.15,
                        days=60, max_leverage=1.0):
    """monkey-patch portfolio.compute_weights：
    method 以 w3_ 开头 → ATR 权重；否则委托原函数（S0 的 risk_parity 不受影响）。"""
    if not (method or "").startswith("w3_"):
        return _ORIG_COMPUTE_WEIGHTS(closes_by_code, method=method,
                                     target_vol=target_vol, days=days,
                                     max_leverage=max_leverage)
    # 当前格窗口 → _start = 窗口起 − (LOAD_DAYS_NEEDED+30) 天（engine _load_data 同口径）
    w0 = _G["cell_window"][0]
    import datetime as _dt
    _start = (_dt.datetime.strptime(w0, "%Y-%m-%d")
              - _dt.timedelta(days=LOAD_DAYS_NEEDED + 30)).strftime("%Y-%m-%d")
    out = {}
    cands = []          # (code, atr_daily)
    for code, closes in closes_by_code.items():
        if len(closes) < 15:
            continue
        dates, atrs = _load_code_bars(code)
        if not dates:
            continue
        idx_first = bisect.bisect_left(dates, _start)
        L = len(closes)
        pos = L - 1 + idx_first
        if pos < 0 or pos >= len(dates):
            continue
        atr = atrs[pos]
        cl = closes[-1]
        if atr is None or atr <= 0 or cl is None or cl <= 0:
            continue
        atr_daily = atr / cl
        if atr_daily <= 0:
            continue
        cands.append((code, atr_daily))
    if not cands:
        n = len(closes_by_code)
        return {c: 1.0 / n for c in closes_by_code} if n else {}
    if method == "w3_s1":
        tgt_daily = (0.15 / 3.0) / math.sqrt(244.0)
        for code, ad in cands:
            out[code] = min(0.30, tgt_daily / ad)
    elif method == "w3_s2":
        risk_pct, stop_base, ref = 0.01, 0.05, 0.025
        for code, ad in cands:
            scale = max(0.5, min(2.0, ad / ref))
            stop_dyn = stop_base * scale
            out[code] = min(0.20, risk_pct / stop_dyn)
    elif method == "w3_s3":
        inv = [(c, 1.0 / ad) for c, ad in cands]
        s = sum(v for _, v in inv)
        for c, v in inv:
            out[c] = min(0.30, 0.90 * v / s) if s > 0 else 0.0
    return out


# ---------------- 引擎基线环境 ----------------
def _filtered_pool(codes):
    excl = set(getattr(C, "DATA_EXCLUDE_CODES", []) or [])
    return [c for c in codes if c not in excl]


def _init_worker(snapshot):
    from app import engine as eng
    _G["snap"] = snapshot
    C.DB_FILE = snapshot
    pit = json.load(open(os.path.join(C.DATA_DIR, "pit_pools.json"), encoding="utf-8"))
    _G["pit_pools"] = pit["pools"]
    _G["pit_generated_at"] = pit.get("generated_at", "")
    _G["bars_cache"] = {}
    # 只 patch 一次（幂等）
    if not _G.get("patched"):
        _port.compute_weights = _w3_compute_weights
        _G["patched"] = True
    # 实时行情置空（引擎基线惯例）
    eng.df.fetch_quotes = lambda cs: {}


# ---------------- 单格回测 ----------------
def _run_one(task):
    variant, widx = task
    from app import engine as eng
    w0, w1 = WINDOWS[widx]
    key = PIT_WIN_MAP[widx]
    codes = _filtered_pool(_G["pit_pools"][key]["codes"])
    names = {c: c for c in codes}
    params = dict(PARAMS_SCORE)
    if variant != "s0":
        params["portfolio_method"] = "w3_" + variant
    _G["cell_window"] = (w0, w1)
    t0 = time.time()
    bt = eng.Backtest(codes, names, w0, w1, 100000.0, "score", params)
    r = bt.run()
    # 自定义指标
    eq = [e["equity"] for e in bt.equity]
    n = len(eq)
    rets = [eq[i] / eq[i - 1] - 1 for i in range(1, n)] if n > 1 else []
    mean_r = sum(rets) / len(rets) if rets else 0.0
    daily_vol = math.sqrt(sum((x - mean_r) ** 2 for x in rets) / len(rets)) if rets else 0.0
    sells = [t for t in bt.trades if t["side"] == "sell"]
    buy_amt = sum(t.get("amount") or 0 for t in bt.trades if t["side"] == "buy")
    sell_amt = sum(t.get("amount") or 0 for t in sells)
    avg_eq = sum(eq) / n if n else 1.0
    turnover = ((buy_amt + sell_amt) / 2.0) / avg_eq if avg_eq > 0 else 0.0
    stop_count = sum(1 for t in sells if t.get("reason") and "止损" in str(t.get("reason")))
    cash_mean = sum(e["cash"] for e in bt.equity) / n if n else 0.0
    return {"variant": variant, "widx": widx, "window": WIN_NAMES[widx],
            "pit_key": key, "n_codes": len(codes),
            "total_return": r.get("total_return"),
            "max_drawdown": r.get("max_drawdown"),
            "annual_return": r.get("annual_return"),
            "sharpe": r.get("sharpe"),
            "win_rate": r.get("win_rate"),
            "trade_count": r.get("trade_count"),
            "daily_vol": round(daily_vol, 5),
            "turnover": round(turnover, 4),
            "stop_count": stop_count,
            "cash_mean": round(cash_mean, 2),
            "elapsed": round(time.time() - t0, 1)}


# ---------------- 落盘 / 续跑 / 判定 ----------------
def _dump(out):
    assert os.path.basename(OUT_JSON) == "bt_sizing_lab.json", OUT_JSON
    assert "c_lane" not in OUT_JSON and "overnight" not in OUT_JSON and "act12" not in OUT_JSON, OUT_JSON
    tmp = OUT_JSON + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    os.replace(tmp, OUT_JSON)


def _load_existing():
    if not os.path.isfile(OUT_JSON):
        return {}
    try:
        with open(OUT_JSON, encoding="utf-8") as f:
            j = json.load(f)
    except Exception:
        return {}
    res = {}
    for k, v in (j.get("results") or {}).items():
        parts = k.split("/")
        if len(parts) == 2 and parts[0] in VARIANTS and parts[1].startswith("w") and parts[1][1:].isdigit():
            if not v or v.get("error"):
                continue          # 跳过空格/error 格，允许重跑
            res[(parts[0], int(parts[1][1:]))] = v
    return res


def _build_out(results, meta, t0):
    # 矩阵 + 判定
    base = {(wi): results.get(("s0", wi)) for wi in range(4)}
    matrix = {}
    judgement = {}
    for v in VARIANTS:
        rows = {}
        for wi in range(4):
            cell = results.get((v, wi))
            b = base.get(wi)
            if cell and b and cell.get("total_return") is not None and b.get("total_return") is not None:
                ret_loss_pp = (b["total_return"] - cell["total_return"]) * 100
                mdd_improve_pp = ((cell.get("max_drawdown") or 0)
                                  - (b.get("max_drawdown") or 0)) * 100
            else:
                ret_loss_pp = mdd_improve_pp = None
            rows[WIN_NAMES[wi]] = {
                "ret": cell.get("total_return") if cell else None,
                "mdd": cell.get("max_drawdown") if cell else None,
                "ret_loss_pp": round(ret_loss_pp, 2) if ret_loss_pp is not None else None,
                "mdd_improve_pp": round(mdd_improve_pp, 2) if mdd_improve_pp is not None else None,
                "window_ok": bool(mdd_improve_pp is not None and ret_loss_pp is not None
                                  and mdd_improve_pp >= 5 and ret_loss_pp <= 3)}
        matrix[v] = rows
    for v in VARIANTS:
        if v == "s0":
            judgement[v] = {"windows_ok": None, "bull_ok": None, "pass": None, "note": "基线"}
            continue
        rows = matrix[v]
        ok_wins = sum(1 for wn in WIN_NAMES if rows[wn]["window_ok"])
        bull_ok = bool(rows["牛市"]["ret_loss_pp"] is not None and rows["牛市"]["ret_loss_pp"] <= 0)
        judgement[v] = {"windows_ok": ok_wins, "bull_ok": bull_ok,
                        "pass": bool(ok_wins >= 3 and bull_ok),
                        "note": ">=3/4窗达标且牛市收益不劣化"}
    any_pass = any(judgement[v]["pass"] for v in ("s1", "s2", "s3"))
    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "task": "W3 仓位科学实验室（vol-target / ATR 风险预算 vs 现行 3×0.30）",
        "meta": meta,
        "pit_pools_generated_at": _G.get("pit_generated_at", ""),
        "results": {f"{v}/w{wi}": results.get((v, wi)) for v in VARIANTS for wi in range(4)},
        "matrix": matrix,
        "judgement": judgement,
        "summary": ("值得接线" if any_pass
                    else "仓位层救不了选股层，ML/数据才是主矛盾（本层无变体满足预注册判据）"),
        "cells_done": len(results),
        "cells_total": 16,
        "elapsed_s": round(time.time() - t0, 1),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--mode", choices=["process", "sequential"], default="process")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    t0 = time.time()
    if not os.path.isfile(SNAPSHOT):
        print("缺冻结快照:", SNAPSHOT, flush=True)
        return 1
    print("[db] 冻结快照: %s" % SNAPSHOT, flush=True)

    _init_worker(SNAPSHOT)
    print("[pit] pools", list(_G["pit_pools"].keys()), "generated", _G["pit_generated_at"], flush=True)
    for k in PIT_WIN_MAP:
        print("  [pit/%s] filtered196=%d" % (k, len(_filtered_pool(_G["pit_pools"][k]["codes"]))), flush=True)
    print("[params] base_score:", PARAMS_SCORE, "| S0 portfolio_method=risk_parity(原生)", flush=True)

    tasks = [(v, wi) for v in VARIANTS for wi in range(4)]
    if a.limit > 0:
        tasks = tasks[:a.limit]

    existing = _load_existing()
    pending = [t for t in tasks if t not in existing]
    print("[resume] existing=%d pending=%d (of %d)" % (len(existing), len(pending), len(tasks)), flush=True)

    results = dict(existing)
    meta = {"snapshot": SNAPSHOT, "excluded_codes": len(set(getattr(C, "DATA_EXCLUDE_CODES", []) or [])),
            "params_score": PARAMS_SCORE, "windows": WINDOWS, "win_names": WIN_NAMES,
            "pit_map": PIT_WIN_MAP, "pool": "PIT only（省格理由：多代理并发算力减半，任务书许可）",
            "method": "monkey-patch portfolio.compute_weights（w3_s1/s2/s3 返回 ATR 权重，S0 原生 risk_parity）",
            "exec_mode": a.mode, "workers": a.workers,
            "prereg": "单窗达标=MDD改善>=5pp且收益损失<=3pp；值得接线=任一变体>=3/4窗达标且牛市收益不劣化"}

    def _save():
        _dump(_build_out(results, meta, t0))

    def _record(r):
        results[(r["variant"], r["widx"])] = r
        _save()

    def _record_err(t, e):
        v, wi = t
        results[(v, wi)] = {"variant": v, "widx": wi, "window": WIN_NAMES[wi], "error": repr(e)}
        _save()

    done_n = 0
    if a.mode == "process" and pending:
        from concurrent.futures import ProcessPoolExecutor, as_completed
        from concurrent.futures.process import BrokenProcessPool
        try:
            ex = ProcessPoolExecutor(max_workers=a.workers, initializer=_init_worker,
                                     initargs=(SNAPSHOT,))
            futs = {ex.submit(_run_one, t): t for t in pending}
            for fut in as_completed(futs):
                t = futs[fut]
                try:
                    r = fut.result()
                    _record(r)
                    print("完成 [%s/%s] ret=%.4f mdd=%.4f vol=%.5f trades=%s (%.0fs)" % (
                        r["variant"], r["window"], r["total_return"] or 0,
                        r["max_drawdown"] or 0, r["daily_vol"] or 0,
                        r["trade_count"], r["elapsed"]), flush=True)
                except Exception as e:
                    import traceback
                    traceback.print_exc()
                    _record_err(t, e)
                    print("cell crash [%s/%s]: %r" % (t[0], WIN_NAMES[t[1]], e), flush=True)
                done_n += 1
            ex.shutdown(wait=True)
        except BrokenProcessPool:
            import traceback
            traceback.print_exc()
            print("!! ProcessPool 断裂，剩余格回退顺序执行", flush=True)
            for t in [t for t in pending if t not in results]:
                try:
                    r = _run_one(t)
                    _record(r)
                    print("完成(seq) [%s/%s] ret=%.4f mdd=%.4f (%.0fs)" % (
                        r["variant"], r["window"], r["total_return"] or 0,
                        r["max_drawdown"] or 0, r["elapsed"]), flush=True)
                except Exception as e:
                    import traceback
                    traceback.print_exc()
                    _record_err(t, e)
                    print("cell crash(seq) [%s/%s]: %r" % (t[0], WIN_NAMES[t[1]], e), flush=True)
                done_n += 1
    else:
        for t in pending:
            try:
                r = _run_one(t)
                _record(r)
                print("完成 [%s/%s] ret=%.4f mdd=%.4f vol=%.5f trades=%s (%.0fs)" % (
                    r["variant"], r["window"], r["total_return"] or 0,
                    r["max_drawdown"] or 0, r["daily_vol"] or 0,
                    r["trade_count"], r["elapsed"]), flush=True)
            except Exception as e:
                import traceback
                traceback.print_exc()
                _record_err(t, e)
                print("cell crash [%s/%s]: %r" % (t[0], WIN_NAMES[t[1]], e), flush=True)
            done_n += 1

    out = _build_out(results, meta, t0)
    _dump(out)
    print("\n===== 判定（S0 为基线对照） =====", flush=True)
    for v in VARIANTS:
        j = out["judgement"][v]
        dd = "; ".join("%s: ret_loss=%spp mdd_imp=%spp%s" % (
            wn, matrix_v[wn].get("ret_loss_pp"), matrix_v[wn].get("mdd_improve_pp"),
            " [达标]" if matrix_v[wn].get("window_ok") else "")
            for wn, matrix_v in ((wn, out["matrix"][v]) for wn in WIN_NAMES))
        print("[%s] 达标窗=%s 牛市不劣化=%s pass=%s\n    %s" % (
            v, j["windows_ok"], j["bull_ok"], j["pass"], dd), flush=True)
    print("summary:", out["summary"], flush=True)
    print("saved", OUT_JSON, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
