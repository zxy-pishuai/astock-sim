# -*- coding: utf-8 -*-
"""X2 离线重放 —— S1 vol-target 权重层独立实现交叉验证（只读，2 窗，零生产改动）

预注册判据（docs/reports/sizing_wiring_proposal.md §0，落盘后不改）：
  牛市窗 + 2023-24 窗，独立实现重放 S1，结果 vs data/bt_sizing_lab.json 的 s1/w2、s1/w3：
    |Δret| <= 1pp 且 |ΔMDD| <= 2pp → 通过（双实现语义等价证据）

独立性边界（预注册声明）：
  - 复用 engine.Backtest 的选股信号 / 撮合 / 退出 / 费用（基线已验证，W3 已证 S0=act12 逐位一致）
  - S1 变体层为独立实现：ATR14 序列计算（本文件独立代码，固定窗口求和，
    非 sizing_lab 的 append/pop 滑动法）+ 权重公式 + min(0.30) cap
  - 不 import tools/sizing_lab.py 的任何函数（脚本内自检断言）
  - 对齐参数 LOAD_DAYS_NEEDED=WARMUP(260)+60 与 W3 相同（引擎 _load_data 语义，非变体层）

用法: python tools/sizing_replay.py [--workers 2] [--mode process|sequential]
输出: data/bt_sizing_replay.json
"""
import argparse
import bisect
import datetime as _dt
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
_ORIG_COMPUTE_WEIGHTS = _port.compute_weights

# ---- 零依赖断言：不得复用 sizing_lab 的实现 ----
_loaded_mods = set(sys.modules)
if any("sizing_lab" in m for m in _loaded_mods):
    raise RuntimeError("sizing_replay 不得 import sizing_lab（独立性自检失败）")

SNAPSHOT = os.path.join(BASE, "data", "snapshots", "2026-08-27", "market.db")
LAB_JSON = os.path.join(BASE, "data", "bt_sizing_lab.json")
OUT_JSON = os.path.join(BASE, "data", "bt_sizing_replay.json")
WINDOWS = [["2023-01-01", "2024-12-31"], ["2025-08-18", "2026-08-21"]]
WIN_NAMES = ["2023-24", "牛市"]
PIT_WIN_MAP = ["2023-24", "近1年"]
WARMUP = 260
LOAD_DAYS_NEEDED = WARMUP + 60
PARAMS_SCORE = {"buy_threshold": 25, "max_positions": 3, "position_pct": 0.30,
                "slippage": 0.001, "zt_eco_gate": False, "dd_gate": False}
TGT_DAILY = (0.15 / 3.0) / math.sqrt(244.0)

_G = {}


# ---------------- 独立 ATR14 实现（固定窗口求和，非 sizing_lab 滑动法） ----------------
def _atr_series(rows):
    """rows: [(date, open, high, low, close)] → (dates, atr14s)
    独立实现：TR 定义同（max(h-l,|h-pc|,|l-pc|)），但用固定 14 根窗口求和，
    与 sizing_lab 的 append/pop 滑动均值代码路径不同（数学等价）。"""
    dates, atrs = [], []
    trs = []
    prev_close = None
    for d, o, h, l, cl in rows:
        if cl is None or cl <= 0 or h is None or l is None:
            prev_close = None
            trs = []
            continue
        dates.append(d)
        if prev_close is not None and prev_close > 0:
            tr = max(h - l, abs(h - prev_close), abs(l - prev_close))
            trs.append(tr)
            if len(trs) >= 14:
                # 固定最后 14 根窗口均值
                atrs.append(sum(trs[-14:]) / 14.0)
            else:
                atrs.append(None)
        else:
            trs = []
            atrs.append(None)
        prev_close = cl
    return dates, atrs


def _load_bars(code):
    """读冻结快照全史有效日K，独立缓存。返回 (dates, atrs)。"""
    if code in _G.get("bars", {}):
        return _G["bars"][code]
    if "bars" not in _G:
        _G["bars"] = {}
    conn = sqlite3.connect("file:%s?mode=ro" % SNAPSHOT, uri=True, timeout=20)
    try:
        rows = conn.execute(
            "SELECT date, open, high, low, close FROM kline "
            "WHERE period='day' AND code=? ORDER BY date", (code,)).fetchall()
    finally:
        conn.close()
    dates, atrs = _atr_series(rows)
    _G["bars"][code] = (dates, atrs)
    return dates, atrs


def _atr_daily_at(code, at_dates, pos):
    """取 dates[pos] 的 ATR14/close（独立）。"""
    if pos < 0 or pos >= len(at_dates):
        return None
    atr = _G["bars"].get(code, (None, None))[1]
    if atr is None or pos >= len(atr):
        return None
    a = atr[pos]
    if a is None or a <= 0:
        return None
    # close 用 dates[pos] 对应收盘（从 bars 重查，确保同源）
    conn = sqlite3.connect("file:%s?mode=ro" % SNAPSHOT, uri=True, timeout=20)
    try:
        r = conn.execute("SELECT close FROM kline WHERE period='day' AND code=? AND date=?",
                         (code, at_dates[pos])).fetchone()
    finally:
        conn.close()
    if not r or r[0] is None or r[0] <= 0:
        return None
    return a / r[0]


def _replay_compute_weights(closes_by_code, method="risk_parity", target_vol=0.15,
                            days=60, max_leverage=1.0):
    """独立 S1 vol-target 权重注入（monkey-patch portfolio.compute_weights）：
    method == 'x2_s1' → 独立 S1；否则委托原函数（S0 等不受影响）。"""
    if not (method or "").startswith("x2_"):
        return _ORIG_COMPUTE_WEIGHTS(closes_by_code, method=method,
                                     target_vol=target_vol, days=days,
                                     max_leverage=max_leverage)
    w0 = _G["cell_window"][0]
    _start = (_dt.datetime.strptime(w0, "%Y-%m-%d")
              - _dt.timedelta(days=LOAD_DAYS_NEEDED + 30)).strftime("%Y-%m-%d")
    out = {}
    for code, closes in closes_by_code.items():
        if len(closes) < 15:
            continue
        dates, atrs = _load_bars(code)
        if not dates:
            continue
        idx_first = bisect.bisect_left(dates, _start)
        L = len(closes)
        pos = L - 1 + idx_first
        if pos < 0 or pos >= len(dates):
            continue
        ad = _atr_daily_at(code, dates, pos)
        if ad is None or ad <= 0:
            continue
        # S1 公式（独立实现）：weight = min(0.30, tgt_daily / atr_daily)
        out[code] = min(0.30, TGT_DAILY / ad)
    if not out and closes_by_code:
        n = len(closes_by_code)
        return {c: 1.0 / n for c in closes_by_code}
    return out


# ---------------- 引擎基线环境（同 W3 惯例） ----------------
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
    _G["bars"] = {}
    if not _G.get("patched"):
        _port.compute_weights = _replay_compute_weights
        _G["patched"] = True
    eng.df.fetch_quotes = lambda cs: {}


# ---------------- 单窗重放 ----------------
def _run_one(task):
    widx = task
    from app import engine as eng
    w0, w1 = WINDOWS[widx]
    key = PIT_WIN_MAP[widx]
    codes = _filtered_pool(_G["pit_pools"][key]["codes"])
    names = {c: c for c in codes}
    params = dict(PARAMS_SCORE)
    params["portfolio_method"] = "x2_s1"
    _G["cell_window"] = (w0, w1)
    t0 = time.time()
    bt = eng.Backtest(codes, names, w0, w1, 100000.0, "score", params)
    r = bt.run()
    eq = [e["equity"] for e in bt.equity]
    n = len(eq)
    rets = [eq[i] / eq[i - 1] - 1 for i in range(1, n)] if n > 1 else []
    mean_r = sum(rets) / len(rets) if rets else 0.0
    daily_vol = math.sqrt(sum((x - mean_r) ** 2 for x in rets) / len(rets)) if rets else 0.0
    sells = [t for t in bt.trades if t["side"] == "sell"]
    cash_mean = sum(e["cash"] for e in bt.equity) / n if n else 0.0
    return {"widx": widx, "window": WIN_NAMES[widx], "pit_key": key, "n_codes": len(codes),
            "total_return": r.get("total_return"),
            "max_drawdown": r.get("max_drawdown"),
            "sharpe": r.get("sharpe"),
            "trade_count": r.get("trade_count"),
            "daily_vol": round(daily_vol, 5),
            "cash_mean": round(cash_mean, 2),
            "elapsed": round(time.time() - t0, 1)}


# ---------------- 落盘 / 判定 ----------------
def _dump(out):
    assert os.path.basename(OUT_JSON) == "bt_sizing_replay.json", OUT_JSON
    assert "c_lane" not in OUT_JSON and "act12" not in OUT_JSON and "sizing_lab" not in OUT_JSON, OUT_JSON
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
        if v and not v.get("error") and v.get("total_return") is not None:
            res[k] = v
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--mode", choices=["process", "sequential"], default="process")
    a = ap.parse_args()
    t0 = time.time()
    if not os.path.isfile(SNAPSHOT):
        print("缺冻结快照:", SNAPSHOT, flush=True)
        return 1
    if not os.path.isfile(LAB_JSON):
        print("缺 W3 产物:", LAB_JSON, flush=True)
        return 1
    print("[db] 冻结快照: %s" % SNAPSHOT, flush=True)

    _init_worker(SNAPSHOT)
    lab = json.load(open(LAB_JSON, encoding="utf-8"))
    lab_s1 = {k: v for k, v in (lab.get("results") or {}).items()
              if k.startswith("s1/") and v and v.get("total_return") is not None}
    # 防御：重放对照必须存在（2023-24=w2, 牛市=w3），缺失即报错
    for _need in ("s1/w2", "s1/w3"):
        if _need not in lab_s1:
            print("!! lab 缺对照格 %s，判据不可用，退出" % _need, flush=True)
            return 2
    print("[lab] s1 基准:", {k: {"ret": v["total_return"], "mdd": v["max_drawdown"]}
                             for k, v in lab_s1.items()}, flush=True)

    tasks = [0, 1]  # widx: 0=2023-24, 1=牛市
    existing = _load_existing()
    pending = [t for t in tasks if ("w%d" % t) not in existing]
    print("[resume] existing=%d pending=%d" % (len(existing), len(pending)), flush=True)

    results = dict(existing)
    meta = {"snapshot": SNAPSHOT, "excluded_codes": len(set(getattr(C, "DATA_EXCLUDE_CODES", []) or [])),
            "params_score": PARAMS_SCORE, "windows": WINDOWS, "win_names": WIN_NAMES,
            "pool": "PIT（同 W3）", "method": "独立实现 monkey-patch portfolio.compute_weights(x2_s1)",
            "independence": "ATR14 固定窗口求和 + 独立权重公式；不 import sizing_lab",
            "exec_mode": a.mode, "workers": a.workers,
            "prereg": "|Δret|<=1pp 且 |ΔMDD|<=2pp（vs bt_sizing_lab.json s1/w2,w3）"}

    def _record(r):
        results["w%d" % r["widx"]] = r
        _dump(build_out(results, meta, lab_s1, t0))

    def _record_err(t, e):
        results["w%d" % t] = {"widx": t, "window": WIN_NAMES[t], "error": repr(e)}
        _dump(build_out(results, meta, lab_s1, t0))

    if a.mode == "process" and pending:
        from concurrent.futures import ProcessPoolExecutor, as_completed
        try:
            ex = ProcessPoolExecutor(max_workers=a.workers, initializer=_init_worker,
                                     initargs=(SNAPSHOT,))
            futs = {ex.submit(_run_one, t): t for t in pending}
            for fut in as_completed(futs):
                t = futs[fut]
                try:
                    r = fut.result()
                    _record(r)
                    print("完成 [%s] ret=%.4f mdd=%.4f (%.0fs)" % (
                        WIN_NAMES[t], r["total_return"] or 0, r["max_drawdown"] or 0, r["elapsed"]), flush=True)
                except Exception as e:
                    import traceback
                    traceback.print_exc()
                    _record_err(t, e)
            ex.shutdown(wait=True)
        except Exception:
            import traceback
            traceback.print_exc()
            for t in [t for t in pending if ("w%d" % t) not in results]:
                try:
                    r = _run_one(t)
                    _record(r)
                    print("完成(seq) [%s] ret=%.4f (%.0fs)" % (WIN_NAMES[t], r["total_return"] or 0, r["elapsed"]), flush=True)
                except Exception as e:
                    _record_err(t, e)
    else:
        for t in pending:
            try:
                r = _run_one(t)
                _record(r)
                print("完成 [%s] ret=%.4f mdd=%.4f (%.0fs)" % (
                    WIN_NAMES[t], r["total_return"] or 0, r["max_drawdown"] or 0, r["elapsed"]), flush=True)
            except Exception as e:
                import traceback
                traceback.print_exc()
                _record_err(t, e)

    out = build_out(results, meta, lab_s1, t0)
    _dump(out)
    print("\n===== 判定 =====", flush=True)
    for k, c in (out.get("compare") or {}).items():
        print("  %s: d_ret=%.3fpp d_mdd=%.3fpp pass=%s" % (
            k, c.get("d_ret_pp"), c.get("d_mdd_pp"), c.get("pass")), flush=True)
    print("overall_pass:", out.get("pass"), flush=True)
    print("saved", OUT_JSON, flush=True)
    return 0


def build_out(results, meta, lab_s1, t0):
    # 重放窗口索引 → W3 lab results 的 4 窗 key（2023-24=w2, 牛市=w3）
    LAB_WIN_KEY = {0: "s1/w2", 1: "s1/w3"}
    compare = {}
    ok = 0
    total = 0
    for widx in (0, 1):
        k = "w%d" % widx
        lab_key = LAB_WIN_KEY[widx]
        cell = results.get(k)
        labv = lab_s1.get(lab_key)
        if cell and not cell.get("error") and labv:
            total += 1
            d_ret = (cell["total_return"] - labv["total_return"]) * 100
            d_mdd = (cell["max_drawdown"] - labv["max_drawdown"]) * 100
            p = abs(d_ret) <= 1.0 and abs(d_mdd) <= 2.0
            if p:
                ok += 1
            compare[WIN_NAMES[widx]] = {
                "lab_ret": labv["total_return"], "lab_mdd": labv["max_drawdown"],
                "replay_ret": cell["total_return"], "replay_mdd": cell["max_drawdown"],
                "d_ret_pp": round(d_ret, 3), "d_mdd_pp": round(d_mdd, 3), "pass": p}
        else:
            compare[WIN_NAMES[widx]] = {"error": True, "lab_ret": labv and labv["total_return"],
                                        "replay": cell and cell.get("total_return")}
    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "task": "X2 S1 vol-target 权重层独立实现交叉验证（2 窗）",
        "meta": meta,
        "results": results,
        "compare": compare,
        "pass": bool(total and ok == total),
        "pass_detail": "%d/%d 窗满足 |Δret|<=1pp 且 |ΔMDD|<=2pp" % (ok, total),
        "cells_done": len(results), "cells_total": 2,
        "elapsed_s": round(time.time() - t0, 1),
    }


if __name__ == "__main__":
    sys.exit(main())
