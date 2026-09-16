# -*- coding: utf-8 -*-
"""★ Phase42: 真打板回测 —— 三档成交模型 × 同期对照。

窗口 = min5 覆盖期（2025-08-18~2026-08-21）；池 = bt_pool 前 300。
四路并排：
  M1 保守 / M2 中性 / M3 激进（app/board_true.py，min5 触板+排板成交假设）
  board   （现有日线近似，engine.Backtest，当前 config 默认）
  board_intraday（engine_minute.MinuteBoardBacktest，5分钟盘中扫板）
另产出封板生态统计（封死率/炸板率/炸板单次日表现）。

用法：python tools/backtest_true_board.py
"""
import sys
import os
import json
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import config as C          # noqa: E402
from app import engine as eng        # noqa: E402
from app.board_true import TrueBoardBacktest   # noqa: E402

OUT_JSON = os.path.join(C.DATA_DIR, "bt_true_board.json")
W0, W1 = "2025-08-18", "2026-08-21"
POOL_N = 300


def main():
    t0 = time.time()
    with open(os.path.join(C.DATA_DIR, "bt_pool.json"), encoding="utf-8") as f:
        codes = json.load(f)["codes"][:POOL_N]
    names = {c: c for c in codes}
    print("[pool] %d codes" % len(codes), flush=True)

    out = {"meta": {
        "window": [W0, W1], "pool_n": POOL_N,
        "capital": 100000.0, "max_positions": 2, "position_pct": 0.25,
        "slippage": C.SLIPPAGE, "fees": "engine.buy_fee/sell_fee（只读引用）",
        "fill_models": {
            "M1": "仅炸板单成交（封死默认排不进），成交价=涨停价",
            "M2": "炸板单100%成交；封死单按首触时间 p(10:30前)=0.3 / p(10:30后)=0.6（seed=42）",
            "M3": "全部触板单成交（上限对照）"},
        "exits": "阶梯止盈%s→止损%.2f→止盈%.2f→时间止损%dd→超时%dd（收盘触发，卖出计滑点）；"
                 "未复刻冲高回落/移动止损/主力出货" % (
                     C.LADDER_TP_STEPS, C.STOP_LOSS_PCT, C.TAKE_PROFIT_PCT,
                     C.TIME_STOP_DAYS, C.MAX_HOLD_DAYS),
        "board_note": "对照用现有 board 日线近似按当前 config 默认（含 Phase33 动量下限 8.0）",
    }, "true_board": {}, "compare": {}, "ecosystem": {}}

    # ---- 真打板三档（共享一次 min5 扫描）----
    tb_first = None
    daily = None
    for model in ("M1", "M2", "M3"):
        tb = TrueBoardBacktest(codes, names, W0, W1, fill_model=model)
        if tb_first is not None:
            tb._events_cache = tb_first._events_cache
            tb.calendar = tb_first.calendar
            daily = daily or tb_first.daily
        r = tb.run(daily=daily)
        if tb_first is None:
            tb_first = tb
            eco = tb.ecosystem_stats()
            out["ecosystem"] = eco
            print("[eco] touches=%d 封死率=%.2f 炸板率=%.2f" % (
                eco["total_touches"], eco["seal_rate"] or -1, eco["break_rate"] or -1),
                flush=True)
        r["model"] = model
        out["true_board"][model] = r
        print("[true-%s] ret=%+.4f mdd=%.4f trades=%d win=%s" % (
            model, r["total_return"], r["max_drawdown"], r["trade_count"],
            r["win_rate"]), flush=True)

    # ---- 对照1：现有 board 日线近似 ----
    orig_q = eng.df.fetch_quotes
    eng.df.fetch_quotes = lambda cs: {}
    try:
        bt = eng.Backtest(codes, names, W0, W1, 100000.0, "board", {})
        rb = bt.run()
    finally:
        eng.df.fetch_quotes = orig_q
    out["compare"]["board_daily"] = {
        "total_return": rb.get("total_return"), "max_drawdown": rb.get("max_drawdown"),
        "trade_count": rb.get("trade_count"), "win_rate": rb.get("win_rate")}
    print("[board日线] ret=%+.4f trades=%s win=%s" % (
        rb.get("total_return") or 0, rb.get("trade_count"), rb.get("win_rate")), flush=True)

    # ---- 对照2：board_intraday（5分钟盘中扫板）----
    try:
        from app.engine_minute import MinuteBoardBacktest
        mbt = MinuteBoardBacktest(codes, names, W0, W1, 100000.0, "board_intraday", {})
        rm = mbt.run()
        sells = [t for t in getattr(mbt, "trades", []) if t.get("side") == "sell"
                 and t.get("pnl") is not None]
        wins = sum(1 for t in sells if t["pnl"] > 0)
        out["compare"]["board_intraday"] = {
            "total_return": rm.get("total_return"), "max_drawdown": rm.get("max_drawdown"),
            "trade_count": rm.get("trade_count"),
            "win_rate": round(wins / len(sells), 4) if sells else rm.get("win_rate")}
        print("[intraday] ret=%+.4f trades=%s" % (
            rm.get("total_return") or 0, rm.get("trade_count")), flush=True)
    except Exception as e:
        out["compare"]["board_intraday"] = {"error": str(e)}
        print("[intraday] FAILED:", e, flush=True)

    out["elapsed_s"] = round(time.time() - t0, 1)
    tmp = OUT_JSON + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    os.replace(tmp, OUT_JSON)
    print("saved", OUT_JSON)


if __name__ == "__main__":
    main()
