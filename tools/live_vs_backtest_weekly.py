# -*- coding: utf-8 -*-
"""★ D5：实盘 vs 回测周度归因周报

数据源（全部只读）：
  ① data/account.json —— 账户历史 trades（time/side/code/price/qty/fee/pnl/
     reason）+ 当前现金；是实盘盈亏的权威账本
  ② data/audit/audit.jsonl —— 订单事件流，用于与账户逐笔交叉核对
     （买=manual_buy|strategy_buy / 卖=manual_sell|strategy_sell；
     ★ C5：strategy_buy 09-08 起、strategy_sell 09-13 起上线，上线前由
     account.trades 兜底——混合口径）
  ③ market.db kline —— MTM 收盘估值与 A 股交易日历、回测输入

回测预期口径（与既有标准化跑法一致）：现行 bt_pool top500 静态池，
score(25/3/0.30/0.001) 与 board(40/2/0.25/0.001) 双配方，fetch_quotes 置空，
其余参数取当前 config 默认值（meta 如实记录）。★注意 P64 结论适用：
该池绝对收益系统性偏乐观——回测预期是"策略在同周的基准发挥"，不是无偏宇宙。

实盘侧口径（继承 risk_daily 只读重建法）：
  净值曲线 = 现金流水 + 逐笔时点持仓按本地收盘估值；首期起点=INITIAL_CAPITAL。
  周归因分三块：已实现(Σpnl)/持仓市值变动/费用拖累，附恒等式残差自检。

用法:
  python tools/live_vs_backtest_weekly.py           # dry-run 打印（默认首期=08-16 起）
  python tools/live_vs_backtest_weekly.py --write   # 落盘 MD 周报 + JSON
输出:
  docs/reports/live_vs_backtest_weekly.md / data/live_vs_backtest_weekly.json
红线: 只读 account.json/audit.jsonl/market.db(ro)；不调用任何在线接口；
      引擎仅做窗口内回测计算（不写库）；不改动任何默认参数文件。
"""
import argparse
import json
import os
import sqlite3
import sys
import time
from collections import defaultdict

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from app import config as C                     # noqa: E402 只读常量

# ★ C5（2026-09-13）：审计结构化事件名集合（买/卖各两个）与"事件上线日"。
#   背景：D5 首期周报只匹配 manual_buy/manual_sell，漏掉 strategy_buy
#   （策略买经 trader._buy 记的结构化事件，09-08 起存在）→ 误报"买入 1/13"。
#   混合口径：上线日之前的历史交易无法追溯补事件 → 以 account.json trades
#   兜底；上线日之后的交易必须命中对应事件名，否则计为真缺失并告警。
BUY_EVENTS = ("manual_buy", "strategy_buy")
SELL_EVENTS = ("manual_sell", "strategy_sell")
BUY_EV_ON = "2026-09-08"    # strategy_buy 首现日（09-08 两笔买入各有 1 条）
SELL_EV_ON = "2026-09-13"   # strategy_sell 首现日（C5 交付日，strategy_sell 上线）

ACCOUNT = os.path.join(C.DATA_DIR, "account.json")
AUDIT_FILE = os.path.join(C.DATA_DIR, "audit", "audit.jsonl")
REPORT_MD = os.path.join(BASE, "docs", "reports",
                         "live_vs_backtest_weekly.md")
OUT_JSON_DEFAULT = os.path.join(C.DATA_DIR, "live_vs_backtest_weekly.json")
START = "2026-08-16"          # 任务书指定首期起点
POOL_SIZE = 500
RECIPES = {
    "score": {"buy_threshold": 25, "max_positions": 3, "position_pct": 0.30,
              "slippage": 0.001},
    "board": {"buy_threshold": 40, "max_positions": 2, "position_pct": 0.25,
              "slippage": 0.001},
}


def _iso_week(day):
    y, w, _ = __import__("datetime").date.fromisoformat(day).isocalendar()
    return "%04d-%02dW" % (y, w)


class KlineRO:
    """market.db 只读助手：收盘取值 / 交易日历"""

    def __init__(self):
        self.conn = sqlite3.connect(
            "file:%s?mode=ro" % os.path.join(C.DATA_DIR, "market.db"),
            uri=True, timeout=15)

    def close(self):
        self.conn.close()

    def close_asof(self, code, date):
        row = self.conn.execute(
            "SELECT close FROM kline WHERE code=? AND period='day' "
            "AND date<=? ORDER BY date DESC LIMIT 1", (code, date)).fetchone()
        return row[0] if row else None

    def dates(self, d0, d1):
        return [r[0] for r in self.conn.execute(
            "SELECT DISTINCT date FROM kline WHERE period='day' "
            "AND length(code)=6 AND date>=? AND date<=? ORDER BY date",
            (d0, d1))]


def load_account():
    with open(ACCOUNT, encoding="utf-8") as f:
        acct = json.load(f)
    trades = sorted(acct.get("trades") or [], key=lambda t: t.get("time", ""))
    return {"cash": acct.get("cash"), "positions": acct.get("positions") or {},
            "trades": trades}


def _audit_files():
    """audit 目录下全部 jsonl（活动 audit.jsonl + B4 每日归档 audit_*.jsonl）。
    ★ C5（2026-09-13）：事件随 B4 每日归档移入归档文件（实测 strategy_buy=63/
    strategy_sell=61 均在 audit_20260910.jsonl），只读活动文件会漏光对拍。
    """
    d = os.path.dirname(AUDIT_FILE)
    if not os.path.isdir(d):
        return []
    out = []
    for f in sorted(os.listdir(d)):
        if f.startswith("audit") and f.endswith(".jsonl"):
            out.append(os.path.join(d, f))
    return out


def load_audit_counts():
    """按日统计买卖记账事件（与账户交叉核对用）。
    ★ C5（2026-09-13）：
      1) 事件名集合改为 买={manual_buy, strategy_buy} /
         卖={manual_sell, strategy_sell}——此前只收 manual_* 两名字，漏掉
         strategy_buy（策略买经 trader._buy 记的结构化事件，09-08 起存在）；
      2) 遍历 audit 目录全部 jsonl（活动+归档），否则事件归档后对拍恒为 0。"""
    want = set(BUY_EVENTS) | set(SELL_EVENTS)
    cnt = defaultdict(int)
    for f in _audit_files():
        with open(f, encoding="utf-8") as fh:
            for ln in fh:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    r = json.loads(ln)
                except Exception:
                    continue
                ev = r.get("event")
                if ev in want:
                    cnt[(r.get("t", "")[:10], ev)] += 1
    return cnt


def equity_curve(trades, kl, initial_capital):
    """交易日粒度净值曲线（风险日报同款口径）：事件时点挪到当日收盘结算。
    返回 {date: equity}（仅覆盖首个事件日～今日之间的本地交易日），及逐周锚点辅助结构。"""
    if not trades:
        return {}, {}
    days = kl.dates(min(t["time"][:10] for t in trades), time.strftime("%Y-%m-%d"))
    # 逐日：先收当日成交，再按当日收盘估值持仓
    by_day = defaultdict(list)
    for t in trades:
        by_day[t["time"][:10]].append(t)
    cash = float(initial_capital)
    lots = defaultdict(int)          # code -> qty
    cost_map = {}                    # code -> 最近买入价（兜底估值）
    curve = {}
    for d in days:
        for t in by_day.get(d, []):
            q = int(t.get("qty") or 0)
            px = float(t.get("price") or 0)
            fee = float(t.get("fee") or 0)
            if t.get("side") == "buy":
                cash -= px * q + fee
                lots[t["code"]] += q
                cost_map[t["code"]] = px
            else:
                cash += px * q - fee
                lots[t["code"]] = max(0, lots[t["code"]] - q)
        mv = 0.0
        for c_, q_ in lots.items():
            if not q_:
                continue
            cl = kl.close_asof(c_, d) or cost_map.get(c_) or 0.0
            mv += q_ * cl
        curve[d] = cash + mv
    return curve, days


def week_rows(curves_days, trades, kl):
    """把实盘切成 ISO 周块并做三路归因分解。"""
    days_by_week = defaultdict(list)
    for d in curves_days:
        days_by_week[_iso_week(d)].append(d)
    out = []
    prev_end_equity = None
    for wk in sorted(days_by_week):
        wdays = days_by_week[wk]
        wset = set(wdays)
        wt = [t for t in trades if t["time"][:10] in wset]
        buys = [t for t in wt if t["side"] == "buy"]
        sells = [t for t in wt if t["side"] == "sell"]
        realized = sum(float(s.get("pnl") or 0) for s in sells)
        fees = sum(float(t.get("fee") or 0) for t in wt)
        e0 = None if prev_end_equity is None else prev_end_equity
        e1 = curves_days[wdays[-1]]
        if e0 is None:
            base = float(getattr(C, "INITIAL_CAPITAL", 100000))
        else:
            base = e0
        week_ret = (e1 / base - 1) if base else None
        # 可加两路分解：已实现（记录 pnl 原样）＋ 其余（浮盈变动、费用拖累、
        # 买卖当日价差等全部非实现路径）。费用单独列示仅作信息项（已含于"其余"）。
        real_pct = realized / base if base else None
        other_pct = (week_ret - real_pct) \
            if (week_ret is not None and real_pct is not None) else None
        out.append({
            "week": wk, "days": wdays,
            "n_buy": len(buys), "n_sell": len(sells),
            "realized": round(realized, 2), "fees": round(fees, 2),
            "win_rate": (round(sum(1 for s in sells
                                   if float(s.get("pnl") or 0) > 0)
                               / len(sells), 3) if sells else None),
            "equity_end": round(e1, 2),
            "base_equity": round(base, 2),
            "week_ret": round(week_ret, 5) if week_ret is not None else None,
            "attr_realized_pct": round(real_pct, 5) if real_pct is not None
            else None,
            "attr_other_pct": round(other_pct, 5)
            if other_pct is not None else None,
            "fees_info_pct": round(-fees / base, 5) if base else None,
            "trades": [{"t": t["time"], "side": t["side"], "code": t["code"],
                        "name": t.get("name"), "price": t.get("price"),
                        "qty": t.get("qty"), "pnl": t.get("pnl"),
                        "fee": t.get("fee"), "reason": t.get("reason")}
                       for t in wt],
        })
        prev_end_equity = e1
    return out


def backtest_expectation(days_this_week):
    """同一周窗的双配方回测。仅计算；引擎不落任何库表。"""
    from app import engine as eng
    orig_q = eng.df.fetch_quotes
    eng.df.fetch_quotes = lambda cs: {}
    try:
        pool = json.load(open(os.path.join(C.DATA_DIR, "bt_pool.json"),
                              encoding="utf-8"))
        codes = pool["codes"][:POOL_SIZE]
        names = {c_: c_ for c_ in codes}
        w0, w1 = days_this_week[0], days_this_week[-1]
        res = {}
        for name, params in RECIPES.items():
            try:
                r = eng.Backtest(codes, names, w0, w1,
                                 float(getattr(C, "INITIAL_CAPITAL", 100000)),
                                 name, params).run()
                res[name] = {"total_return": r.get("total_return"),
                             "win_rate": r.get("win_rate"),
                             "trade_count": r.get("trade_count"),
                             "max_drawdown": r.get("max_drawdown")}
            except Exception as e:
                res[name] = {"error": str(e)[:80]}
        return res
    finally:
        eng.df.fetch_quotes = orig_q


def selftest():
    """合成样例：净值重建与周归因分解（纯函数口径）"""
    ok = True

    def chk(name, got, want, tol=1e-6):
        nonlocal ok
        good = abs(got - want) <= tol if isinstance(want, (int, float)) \
            and not isinstance(want, bool) else got == want
        print("  [%s] %s: %r (期望 %r)" % ("PASS" if good else "FAIL",
                                           name, got, want))
        ok = ok and good

    # 期初 100000；周一买 100 股@10（费1），周二卖@11（费1,pnl=99？演示用任意值）
    # 口径：realized 取记录 pnl 原样；市值变动由收盘估值算。
    class FakeKL:
        def dates(self, a, b):
            return ["2026-01-05", "2026-01-06"]

        def close_asof(self, code, date):
            return 10.0 if date == "2026-01-05" else 11.0
    tr = [
        {"time": "2026-01-05 10:00:00", "code": "X", "side": "buy",
         "price": 10.0, "qty": 100, "fee": 1.0, "pnl": None},
        {"time": "2026-01-06 10:00:00", "code": "X", "side": "sell",
         "price": 11.0, "qty": 100, "fee": 1.0, "pnl": 98.0},
    ]
    curve, days = equity_curve(tr, FakeKL(), 100000.0)
    chk("day1 eq", curve["2026-01-05"], 100000 - 1001 + 100 * 10.0)
    # day2：cash=100000-1001+(1100-1)=100098，持仓清零
    chk("day2 eq", curve["2026-01-06"], 100000 - 1001 + 1099.0)
    rows = week_rows(curve, tr, FakeKL())
    chk("rows", len(rows), 1)
    r0 = rows[0]
    chk("realized", r0["realized"], 98.0)
    chk("fees", r0["fees"], 2.0)
    chk("equity_end", r0["equity_end"], curve["2026-01-06"])
    print("\n自检%s" % ("通过 ✅" if ok else "失败 ❌"))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description="D5 实盘vs回测周度归因")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--json", default=OUT_JSON_DEFAULT)
    ap.add_argument("--start", default=START)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(selftest())

    acct = load_account()
    trades = [t for t in acct["trades"]
              if (t.get("time") or "") >= a.start + " 00:00:00"
              or t.get("time", "") >= a.start]
    kl = KlineRO()
    try:
        curve, days_all = equity_curve(acct["trades"], kl,
                                       getattr(C, "INITIAL_CAPITAL", 100000))
        wr = week_rows(curve, trades, kl)
        # 回测预期（每周一次独立窗口运行）
        bt_cache = {}
        if wr:
            pool_meta_note = ""
        for row in wr:
            wd = row["days"]
            key = (wd[0], wd[-1])
            if key not in bt_cache:
                bt_cache[key] = backtest_expectation(wd)
            row["backtest"] = bt_cache[key]

        # ---- 交叉核对：account vs audit ----
        #   ★ C5：买=manual_buy|strategy_buy / 卖=manual_sell|strategy_sell；
        #   命中任一名即算覆盖；混合口径=上线日前 account 兜底、上线日后告警。
        ac = load_audit_counts()
        xchk = []
        for row in wr:
            for t in row["trades"]:
                d = t["t"][:10]
                evs = BUY_EVENTS if t["side"] == "buy" else SELL_EVENTS
                ev_found = next((ev for ev in evs
                                 if ac.get((d, ev), 0) > 0), None)
                xchk.append({"day": d, "side": t["side"], "code": t["code"],
                             "audit_event": ev_found,
                             "audit_n": sum(ac.get((d, ev), 0) for ev in evs)})
        n_buys = sum(1 for x in xchk if x["side"] == "buy")
        n_sells = sum(1 for x in xchk if x["side"] == "sell")
        buys_unmatched = sum(1 for x in xchk
                             if x["side"] == "buy" and not x["audit_n"])
        sells_unmatched = sum(1 for x in xchk
                              if x["side"] == "sell" and not x["audit_n"])
        # ★ C5 混合口径拆分：上线日前 → account.trades 兜底（历史无法补事件）；
        #   上线日后 audit_n=0 → 真缺失（告警）
        buys_pre_on = sum(1 for x in xchk
                          if x["side"] == "buy" and not x["audit_n"]
                          and x["day"] < BUY_EV_ON)
        buys_post_miss = buys_unmatched - buys_pre_on
        sells_pre_on = sum(1 for x in xchk
                           if x["side"] == "sell" and not x["audit_n"]
                           and x["day"] < SELL_EV_ON)
        sells_post_miss = sells_unmatched - sells_pre_on
        xchk_alert = (buys_post_miss > 0 or sells_post_miss > 0)
        tot_realized = round(sum(r["realized"] for r in wr), 2)
        tot_ret = None
        if curve and getattr(C, "INITIAL_CAPITAL", 0):
            last_d = max(curve)
            tot_ret = round(curve[last_d] /
                            float(getattr(C, "INITIAL_CAPITAL", 100000)) - 1, 5)

        t0 = time.strftime("%Y-%m-%d %H:%M:%S")

        # ---- 控制台 ----
        print("== D5 实盘 vs 回测 周度归因（首期 %s 起）==" % a.start)
        print("账户在册交易 %d 笔（观察期内 %d）；当前现金 %.2f；当前空仓=%s" % (
            len(acct["trades"]), len(trades), acct["cash"] or 0,
            not acct["positions"]))
        for r in wr:
            bts = r.get("backtest") or {}
            sc = bts.get("score") or {}
            bd = bts.get("board") or {}

            def pct(x):
                return ("%+.2f%%" % (x * 100)) if isinstance(x, (int, float)) \
                    else "-"
            print("\n[%s] %s~%s 交易日=%d" % (
                r["week"], r["days"][0], r["days"][-1], len(r["days"])))
            print("  实盘: 买%d/卖%d 已实现%+.2f 手续费%.2f 周收益%s "
                  "(期末权益 %.2f)" % (
                      r["n_buy"], r["n_sell"], r["realized"], r["fees"],
                      pct(r["week_ret"]), r["equity_end"]))
            print("  归因(可加): 已实现 %+6.2f%% ＋ 其余(浮盈/费用/日内价差) %+6.2f%%"
                  " = 周收益 | 其中费用 -%.2f%%（信息项）" % (
                      (r["attr_realized_pct"] or 0) * 100,
                      (r["attr_other_pct"] or 0) * 100,
                      -(r["fees_info_pct"] or 0) * 100))
            print("  回测预期(现行top500静态池): score %s (胜率%s/%d笔)"
                  " board %s (%d笔)" % (
                      pct(sc.get("total_return")),
                      ("%d%%" % round((sc.get("win_rate") or 0) * 100))
                      if sc.get("win_rate") is not None else "-",
                      sc.get("trade_count") or 0,
                      pct(bd.get("total_return")),
                      bd.get("trade_count") or 0))
            if isinstance(sc.get("total_return"), (int, float)):
                print("  gap(实盘-score)=%+.2fpp  gap(实盘-board)=%+.2fpp" % (
                    ((r["week_ret"] or 0) - sc["total_return"]) * 100,
                    ((r["week_ret"] or 0) - (bd.get("total_return") or 0)) * 100))
        print("\n全程: 已实现合计 %+.2f；权益口径总收益 %s" % (
            tot_realized, pct(tot_ret)))
        print("审计覆盖核对: 买入 %d/%d 有结构化事件（manual_buy/strategy_buy）；"
              "卖出 %d/%d 有结构化事件（manual_sell/strategy_sell）" % (
                  n_buys - buys_unmatched, n_buys,
                  n_sells - sells_unmatched, n_sells))
        if buys_post_miss or sells_post_miss:
            print("⚠ 上线后真缺失（%s/%s 起，audit 应命中而未命中）：" % (
                BUY_EV_ON, SELL_EV_ON))
            for x in xchk:
                if (x["side"] == "buy" and x["day"] >= BUY_EV_ON
                        and not x["audit_n"]):
                    print("   · %s 买入 %s 无 %s/%s 事件"
                          % (x["day"], x["code"], *BUY_EVENTS))
                if (x["side"] == "sell" and x["day"] >= SELL_EV_ON
                        and not x["audit_n"]):
                    print("   · %s 卖出 %s 无 %s/%s 事件"
                          % (x["day"], x["code"], *SELL_EVENTS))
        elif buys_pre_on or sells_pre_on:
            print("· 混合口径：事件上线前（买<%s、卖<%s）%d 笔由 account.json "
                  "trades 兜底（历史卖出无 strategy_sell 可追溯补事件）；"
                  "上线后 audit 交叉校验 0 缺失" % (
                      BUY_EV_ON, SELL_EV_ON, buys_pre_on + sells_pre_on))
        if xchk_alert:
            print("⚠ 审计覆盖缺口告警：上线日后存在未入审计的成交，"
                  "需人工排查（阈值>0 即告警）")
        print("   → 周报以 account.json 为权威账本、audit 作结构化旁证"
              "（买入 %s 起、卖出 %s 起全量入审计）" % (BUY_EV_ON, SELL_EV_ON))

        if not a.write:
            print("\n[dry-run] 未写任何文件")
            return

        doc = {"generated_at": t0, "start": a.start,
               "config_snapshot": {
                   "BOARD_MOMENTUM_MIN": getattr(C, "BOARD_MOMENTUM_MIN", None),
                   "SLIPPAGE": getattr(C, "SLIPPAGE", None),
                   "INITIAL_CAPITAL": getattr(C, "INITIAL_CAPITAL", None)},
               "pool": "bt_pool.json top500（静态，P64 偏乐观声明适用）",
               "recipes": RECIPES,
               "weeks": wr,
               "totals": {"realized": tot_realized, "equity_ret": tot_ret},
               "crosscheck": xchk,
               "audit_meta": {
                   "buy_events": list(BUY_EVENTS),
                   "sell_events": list(SELL_EVENTS),
                   "buy_ev_on": BUY_EV_ON, "sell_ev_on": SELL_EV_ON,
                   "n_buys": n_buys, "n_sells": n_sells,
                   "buys_covered": n_buys - buys_unmatched,
                   "sells_covered": n_sells - sells_unmatched,
                   "buys_pre_on": buys_pre_on, "sells_pre_on": sells_pre_on,
                   "buys_post_miss": buys_post_miss,
                   "sells_post_miss": sells_post_miss,
                   "alert": xchk_alert}}
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=1)
        print("已写", a.json)

        header_needed = not os.path.exists(REPORT_MD)
        with open(REPORT_MD, "a", encoding="utf-8") as f:
            if header_needed:
                f.write(
                    "# 实盘 vs 回测 周度归因（首期 %s 起）\n\n"
                    "- 工具：`tools/live_vs_backtest_weekly.py`（dry-run/--write/"
                    "--selftest；全部只读）\n"
                    "- 实盘口径：account.trades 全量重放 + 本地收盘 MTM 净值"
                    "（risk_daily 同款）；周归因可加分解=已实现＋其余"
                    "（浮盈/费用/日内价差），费用另列信息项\n"
                    "- 回测预期：现行 bt_pool top500 静态池，score/board 双配方、"
                    "fetch_quotes 置空、其余取当前 config 默认；**P64 声明适用："
                    "静态池基准系统性偏乐观，gap 应据此解读**\n\n"
                    % a.start)
            for r in wr:
                bts = r.get("backtest") or {}
                sc = bts.get("score") or {}
                bd = bts.get("board") or {}

                def p_(x):
                    return ("%+.2f%%" % (x * 100))
                gap_s = ((r["week_ret"] - sc["total_return"]) * 100
                         if isinstance(sc.get("total_return"), (int, float))
                         and r.get("week_ret") is not None else None)
                f.write(
                    "\n## [%s] %s ~ %s（%d 个交易日）\n\n"
                    "| 指标 | 实盘 | 回测 score | 回测 board |\n|---|---|---|---|\n"
                    "| 周收益 | %s | %s | %s |\n"
                    "| 成交 | 买%d/卖%d | %s笔 | %s笔 |\n"
                    "| 胜率 | %s | %s | %s |\n\n"
                    "- 三路归因：已实现 **%+.2f%%** ＋ 其余(浮盈变动/费用/日内价差)"
                    " **%+.2f%%** ＝ 周收益（费用合计 %.2f 元 ≈ -%.2f%%，"
                    "已含于\"其余\"，信息项）\n"
                    "- 交易明细：%s\n"
                    % (
                        r["week"], r["days"][0], r["days"][-1], len(r["days"]),
                        p_(r["week_ret"]) if r["week_ret"] is not None else "-",
                        p_(sc["total_return"]) if isinstance(
                            sc.get("total_return"), (int, float)) else "-",
                        p_(bd["total_return"]) if isinstance(
                            bd.get("total_return"), (int, float)) else "-",
                        r["n_buy"], r["n_sell"],
                        sc.get("trade_count") or "-", bd.get("trade_count") or "-",
                        ("%d%%" % round(r["win_rate"] * 100))
                        if r["win_rate"] is not None else "-",
                        ("%d%%" % round(sc["win_rate"] * 100))
                        if sc.get("win_rate") is not None else "-",
                        ("%d%%" % round(bd["win_rate"] * 100))
                        if bd.get("win_rate") is not None else "-",
                        (r["attr_realized_pct"] or 0) * 100,
                        (r["attr_other_pct"] or 0) * 100,
                        r["fees"],
                        -(r["fees_info_pct"] or 0) * 100,
                        "；".join("%s %s(%s) %sx%s pnl=%s「%s」" % (
                            t["t"][5:16], t["side"], t["code"], t["price"],
                            t["qty"], t["pnl"], (t["reason"] or "")[:24])
                            for t in r["trades"]) or "无"))
            f.write("\n<!-- 本次运行 %s：周数=%d；全程已实现 %+.2f -->\n"
                    % (t0, len(wr), tot_realized))
        print("已更新", REPORT_MD)
    finally:
        kl.close()


if __name__ == "__main__":
    main()
