# -*- coding: utf-8 -*-
"""★ Phase69: 竞价弱转强 v2 —— 作为 board 入场前置过滤器的同日 A/B。

思路变更（P34 独立策略已证否）：不再把弱转强当独立策略，而是测试
"昨日强势的 board 候选，今日竞价高开 +1%~+4% 才允许买入"这一前置过滤器
是否能改善 board 基线。口径全部为竞价时刻可得信息（开盘缺口），PIT 安全；
"带量"维度因首根 5 分钟 bar 在买入时点（开盘）尚未形成而不纳入，如实声明。

方法：同一数据/参数/种子下跑两臂——
  对照臂：board 基线原样
  过滤臂：_signals_on 包装，仅保留"次日开盘缺口 ∈ [+1%,+4%]"的信号
四窗口各一对 A/B。判定（事先写死）：过滤臂 ≥3/4 窗口收益改善或持平(Δ≥-1pp)
且牛市恶化 ≤2pp 且交易数下降幅度 ≤50%（过滤太狠=不可用）→ 有增益；
否则如实关闭该方向。
"""
import sys
import os
import json
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from app import config as C
from app import engine as eng
from app import data_snapshot as ds   # ★ Phase46：冻结快照口径（活库会被前复权重写）

WINDOWS = [["2019-01-01", "2020-12-31"], ["2021-01-01", "2022-12-31"],
           ["2023-01-01", "2024-12-31"], ["2025-08-18", "2026-08-18"]]
WIN_NAMES = ["2019-20", "2021-22", "2023-24", "牛市"]
PARAMS = {"buy_threshold": 40, "max_positions": 2, "position_pct": 0.25,
          "slippage": 0.001, "zt_eco_gate": False, "dd_gate": False}
GAP_MIN, GAP_MAX = 1.0, 4.0        # 今日开盘缺口区间(%)


def run_arm(codes, names, tag, widx, filtered):
    w0, w1 = WINDOWS[widx]
    t0 = time.time()
    orig_quotes = eng.df.fetch_quotes
    eng.df.fetch_quotes = lambda cs: {}
    try:
        bt = eng.Backtest(codes, names, w0, w1, 100000.0, "board", dict(PARAMS))
        bt._load_data()
        if filtered:
            orig_signals = bt._signals_on

            def gap_filtered(date):
                sigs = orig_signals(date)
                out = []
                di = bt.trading_days.index(date)
                if di + 1 >= len(bt.trading_days):
                    return out
                nxt = bt.trading_days[di + 1]
                for s in sigs:
                    rec = bt.klines.get(s["code"])
                    if not rec:
                        continue
                    j = bt.date_index.get(s["code"], {}).get(nxt)
                    if j is None or j < 1:
                        continue
                    g = (rec[j]["open"] / rec[j - 1]["close"] - 1.0) * 100.0
                    if GAP_MIN <= g <= GAP_MAX:
                        out.append(s)
                return out

            bt._signals_on = gap_filtered
        r = bt.run()
    finally:
        eng.df.fetch_quotes = orig_quotes
    sells = [t for t in bt.trades if t["side"] == "sell" and t.get("pnl") is not None]
    wins = sum(1 for t in sells if t["pnl"] > 0)
    row = {"arm": tag, "filtered": filtered, "window": WIN_NAMES[widx],
           "total_return": r.get("total_return"), "max_drawdown": r.get("max_drawdown"),
           "trade_count": r.get("trade_count"),
           "win_rate": round(wins / len(sells), 4) if sells else None,
           "elapsed": round(time.time() - t0, 1)}
    print("  [%s %s] ret=%+.4f dd=%.4f trades=%d win=%s (%.0fs)" % (
        tag, WIN_NAMES[widx], row["total_return"] or 0, row["max_drawdown"] or 0,
        row["trade_count"], row["win_rate"], row["elapsed"]), flush=True)
    return row


def main():
    t0 = time.time()
    # ★ B2（2026-09-13）：快照新鲜度断言——超期（默认 >3 天）直接拒绝运行。
    snap = ds.latest_snapshot_path(max_age_days=3)
    if not snap:
        print("[B2] 无可用快照或最新快照超期（>3 天，含无快照），拒绝运行——"
              "请先补快照（python tools/data_snapshot.py --force 或等服务收盘自动生成）",
              flush=True)
        sys.exit(2)
    snap_info = ds.snapshot_info(snap)
    C.DB_FILE = snap                    # ★ 冻结快照口径（可复现）
    print("[db] 使用冻结快照:", snap, "tag=%s age=%sd coverage=%s" % (
        snap_info["tag"], snap_info["age_days"], snap_info["coverage"] or {}),
        flush=True)
    with open(os.path.join(C.DATA_DIR, "bt_pool.json"), encoding="utf-8") as f:
        codes = json.load(f)["codes"][:500]
    names = {c: c for c in codes}
    rows = []
    improve = 0
    for wi in range(len(WINDOWS)):
        base = run_arm(codes, names, "base", wi, False)
        filt = run_arm(codes, names, "filt", wi, True)
        delta = (filt["total_return"] or 0) - (base["total_return"] or 0)
        ok = delta >= -1.0
        improve += 1 if ok else 0
        bull_ok = None
        rows.append({"window": WIN_NAMES[wi], "base": base, "filtered": filt,
                     "delta_pp": round(delta * 100, 2), "delta_ok": ok})
        print("[Δ] %s %+0.2fpp (%s)" % (WIN_NAMES[wi], delta * 100,
                                        "达标" if ok else "未达标"), flush=True)
    bull_delta = rows[3]["delta_pp"]
    trade_ok = all(abs(r["filtered"]["trade_count"] - r["base"]["trade_count"])
                   <= 0.5 * max(r["base"]["trade_count"], 1) for r in rows)
    passed = improve >= 3 and bull_delta >= -2.0 and trade_ok
    verdict = ("有增益：可作为 board 入场前置过滤器进入集成评估"
               if passed else
               "无增益：如实关闭该方向（弱转强作为打板前置过滤不成立）")
    out = {
        "meta": {"pool": "bt_pool top500", "params": PARAMS,
                 "filter": "今日开盘缺口 ∈ [+1%%,+4%%]（竞价可得信息，PIT 安全）；"
                           "'带量'维度因首根5分钟bar在买点未形成而未纳入（声明）"},
        # ★ B2：快照新鲜度追溯（tag/age/coverage）
        "snapshot_tag": snap_info["tag"],
        "snapshot_age_days": snap_info["age_days"],
        "snapshot_coverage": snap_info["coverage"] or {},
        "ab": rows,
        "judge": {"improved_or_flat_windows": improve, "bull_delta_pp": bull_delta,
                  "trade_shrink_le50pct": trade_ok, "pass": passed},
        "verdict": verdict,
        "elapsed_s": round(time.time() - t0, 1),
    }
    path = os.path.join(C.DATA_DIR, "attribution", "board_w2s_filter_ab.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    print("\n=== 判定 ===")
    print(json.dumps(out["judge"], ensure_ascii=False))
    print(verdict)
    print("saved", path)


if __name__ == "__main__":
    main()
