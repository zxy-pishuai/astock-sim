# -*- coding: utf-8 -*-
"""★ Phase32：分钟级量价卖出历史验证（只读 + 新增文件，不改任何现有代码）

目的：config.VOLP_SELL_MIN_*（分钟爆量滞涨清仓 / 高位缩量上冲卖半仓）默认关闭，
本脚本用 min5.db（2025-08-18 ~ 2026-08-21 全市场 5 分钟数据）做开启前历史回放验证。

方法：
1) 基线回测：score 策略默认参数、近一年窗口（min5 覆盖范围内）、top500 池
   （Phase21/27 基线配方：fetch_quotes 置空），取全部已平仓交易（FIFO 配对，
   含阶梯止盈的部分平仓）。
2) 逐笔持仓回放持仓期间每根 5 分钟 bar，按现行参数模拟 minute_vol.minute_volp_sell
   的两条规则（语义与 trader.py 实盘路径一致：仅入场次日及以后生效、半仓每笔一次）：
   - min_surge_stall 清仓：分钟量比>SURGE 且 自当日高点回落≥PULLBACK
   - min_shrink_rise 卖半仓：日内涨幅≥5%（对昨收）且 分钟量比<SHRINK
   分钟量比复刻 minute_vol.minute_vol_ratio：当日截至该 bar 累计量/已交易分钟数
   ÷ 前5个交易日同时段(≤bar 时刻)累计均量的每分钟量。触发价计滑点 0.1%。
3) 统计：触发率、规则出场 vs 实际出场的收益改善分布、★卖飞成本（触发价到原
   出场日收盘、及之后第 5 个交易日收盘的走势）。
4) 参数敏感性：SURGE{1.5,2.0,2.5} × PULLBACK{0.3%,0.5%,0.8%} 九组合。

判定规则（事先写死）：建议开启须同时满足——
  C1: 被触发交易的收益改善占比 ≥55%；C2: 触发交易平均改善 >0；
  C3: 九组参数网格中 ≥半数组合满足与基准参数同向结论（C1+C2 同过）。
满足 → 报告给出建议开启的具体参数（是否改 config 由验收方决定）；否则如实写维持关闭。

用法: python tools/min_sell_verify.py
输出: data/bt_min_sell.json
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
except Exception:
    pass

from app import config as C
from app import engine as eng

OUT_JSON = os.path.join(BASE, "data", "bt_min_sell.json")
W_START, W_END = "2025-08-18", "2026-08-21"      # 近一年窗口 ∩ min5 覆盖
SCORE_PARAMS = {"buy_threshold": C.BUY_SCORE_THRESHOLD,
                "max_positions": C.MAX_POSITIONS,
                "position_pct": C.POSITION_PCT,
                "slippage": C.SLIPPAGE}
SLIP = C.SLIPPAGE
# ---- 判定规则常量（事先写死）----
RULE_MIN_IMPROVE_RATIO = 0.55
RULE_GRID_MAJORITY = 0.5
# ---- 参数网格 ----
GRID_SURGE = [1.5, 2.0, 2.5]
GRID_PULLBACK = [0.003, 0.005, 0.008]


def _min5_code(code):
    return ("sh" if code.startswith("6") else "sz") + code


def run_baseline():
    """基线回测并按 FIFO 配对出全部已平仓记录"""
    from tools.backtest_zt_eco import build_pool
    codes, names = build_pool()
    orig = eng.df.fetch_quotes
    eng.df.fetch_quotes = lambda cs: {}
    try:
        bt = eng.Backtest(codes, names, W_START, W_END, 100000.0, "score",
                          dict(SCORE_PARAMS))
        res = bt.run()
    finally:
        eng.df.fetch_quotes = orig
    assert "error" not in res, res
    print("基线: ret=%.4f mdd=%.4f trades=%d" % (
        res["total_return"], res["max_drawdown"], res["trade_count"]), flush=True)
    # FIFO 配对（trades 按时间序；buy 入队，sell 消耗最老手数）
    open_lots = {}
    records = []
    for t in sorted(bt.trades, key=lambda x: x["date"]):
        q = open_lots.setdefault(t["code"], [])
        if t["side"] == "buy":
            q.append({"date": t["date"], "px": t["price"], "qty": t["qty"]})
        else:
            need = t["qty"]
            while need > 0 and q:
                lot = q[0]
                take = min(need, lot["qty"])
                records.append({
                    "code": t["code"], "name": t.get("name"),
                    "entry_date": lot["date"], "entry_px": lot["px"],
                    "exit_date": t["date"], "exit_px": t["price"],
                    "reason": t.get("reason"), "qty": take})
                lot["qty"] -= take
                need -= take
                if lot["qty"] <= 0:
                    q.pop(0)
    print("已平仓记录: %d 条（未平 %d 手队列）" % (
        len(records), sum(l["qty"] for q in open_lots.values() for l in q)), flush=True)
    return records


class Min5Store(object):
    """懒加载每股 min5 序列 + 日线昨收映射；前5日同时段基准用前缀和二分"""

    def __init__(self):
        self._bars = {}       # code -> [(day, hhmm, o,h,l,c,v)]
        self._days = None     # 全局交易日列表
        self._prefix = {}     # (code, day) -> (times[], cumvol[])
        self._prev_close = {} # code -> {day: 昨收}
        self.conn = sqlite3.connect(
            "file:%s?mode=ro" % os.path.join(BASE, "data", "min5.db"),
            uri=True, timeout=20)

    def _load_code(self, code):
        if code in self._bars:
            return
        sym = _min5_code(code)
        rows = self.conn.execute(
            "SELECT date, open, high, low, close, volume FROM kline_min5 "
            "WHERE code=? ORDER BY date", (sym,)).fetchall()
        bars = []
        for d, o, h, l, c, v in rows:
            bars.append((d[:10], d[11:16], o, h, l, c, v or 0))
        self._bars[code] = bars
        pf = {}
        cur_day, times, cums, cum = None, [], [], 0.0
        for day, tm, _o, _h, _l, _c, v in bars:
            if day != cur_day:
                cur_day, times, cums, cum = day, [], [], 0.0
                self._prefix[(code, day)] = (times, cums)
            times.append(tm)
            cum += v
            cums.append(cum)
        # 昨收（日线）
        conn = sqlite3.connect("file:%s?mode=ro" % C.DB_FILE, uri=True, timeout=15)
        pc = {}
        last = None
        for d, cl in conn.execute(
                "SELECT date, close FROM kline WHERE period='day' AND code=? "
                "AND close>0 ORDER BY date", (code,)):
            if last is not None:
                pc[d] = last
            last = cl
        conn.close()
        self._prev_close[code] = pc

    def trading_days(self):
        if self._days is None:
            rows = self.conn.execute(
                "SELECT DISTINCT substr(date,1,10) FROM kline_min5 ORDER BY 1").fetchall()
            self._days = [r[0] for r in rows]
        return self._days

    def bars_in(self, code, day_from, day_to):
        """[day_from, day_to] 闭区间的 5m bars"""
        self._load_code(code)
        return [b for b in self._bars.get(code, []) if day_from <= b[0] <= day_to]

    def base_per_min(self, code, day, hhmm, minutes):
        """前5个交易日同时段累计量 ÷ (5×minutes)。数据不足返回 0（与实盘一致→无量比）"""
        days = self.trading_days()
        try:
            i = days.index(day)
        except ValueError:
            return 0.0
        prevs = days[max(0, i - 5):i]
        total = 0.0
        for pd in prevs:
            pf = self._prefix.get((code, pd))
            if not pf:
                continue
            times, cum = pf
            # 二分找 ≤hhmm 的最后一根
            lo, hi, pos = 0, len(times) - 1, -1
            while lo <= hi:
                mid = (lo + hi) // 2
                if times[mid] <= hhmm:
                    pos = mid; lo = mid + 1
                else:
                    hi = mid - 1
            if pos >= 0:
                total += cum[pos]
        n = i - max(0, i - 5)
        if total <= 0 or n == 0 or minutes <= 0:
            return 0.0
        return total / (n * minutes)

    def prev_close(self, code, day):
        self._load_code(code)
        return self._prev_close.get(code, {}).get(day)


def _minutes_elapsed(hhmm):
    hm = int(hhmm.replace(":", ""))
    if hm < 930:
        return 0
    if hm <= 1130:
        return (int(hhmm[:2]) - 9) * 60 + int(hhmm[3:]) - 30
    if hm < 1300:
        return 120
    if hm <= 1500:
        return 120 + (int(hhmm[:2]) - 13) * 60 + int(hhmm[3:])
    return 240


def scan_record(store, rec, surge, pullback, shrink, rise_newhigh):
    """回放单笔已平仓记录，返回规则事件与统计字段"""
    # 持仓期间：入场次日起（T+1，与 trader 一致）至原出场日（含）
    days = store.trading_days()
    try:
        ei = days.index(rec["entry_date"])
    except ValueError:
        return {"fired": False, "skip": "entry日无min5"}
    hold_days = days[ei + 1:]
    out = {"fired": False, "events": []}
    if not hold_days or hold_days[0] > rec["exit_date"]:
        return out
    bars = store.bars_in(rec["code"], hold_days[0], rec["exit_date"])
    if not bars:
        out["skip"] = "无min5覆盖"
        return out
    day_state = {}   # day -> [cum_vol, day_high]
    fired_shrink = None
    fired_surge = None
    for day, tm, _o, h, _l, c, v in bars:
        st = day_state.setdefault(day, [0.0, 0.0])
        st[0] += v
        if h > st[1]:
            st[1] = h
        M = _minutes_elapsed(tm)
        if M <= 0 or st[1] <= 0:
            continue
        base = store.base_per_min(rec["code"], day, tm, M)
        if base <= 0:
            continue
        ratio = (st[0] / M) / base
        pc = store.prev_close(rec["code"], day)
        pct = (c - pc) / pc if pc else 0.0
        # 缩量上冲卖半仓（每笔一次；先判清仓优先级与实盘一致：滞涨在前但互斥场景少）
        if (fired_shrink is None and pct >= rise_newhigh and ratio < shrink):
            fired_shrink = {"day": day, "time": tm, "px": c * (1 - SLIP),
                            "ratio": round(ratio, 3), "pct": round(pct * 100, 2)}
        # 爆量滞涨清仓
        pull = (st[1] - c) / st[1] if st[1] > 0 else 0.0
        if (fired_surge is None and ratio > surge and pull >= pullback):
            fired_surge = {"day": day, "time": tm, "px": c * (1 - SLIP),
                           "ratio": round(ratio, 3), "pullback_pct": round(pull * 100, 2)}
        if fired_surge:      # 清仓后不再扫描（与实盘一致）
            break
    events = []
    if fired_shrink:
        events.append(("min_shrink_rise", fired_shrink))
    if fired_surge:
        events.append(("min_surge_stall", fired_surge))
    out["events"] = events
    out["fired"] = bool(events)
    # 规则出场价值 vs 实际出场价值（净额，含卖出费用与滑点）
    qty = rec["qty"]
    actual_net = rec["exit_px"] * qty * (1 - SLIP) - eng.sell_fee(rec["exit_px"] * qty)
    rule_net = 0.0
    remain = float(qty)
    half_done = False
    for ev_name, ev in events:
        if ev_name == "min_shrink_rise":
            hq = math.floor(qty * C.VOLP_SELL_HALF_PCT / 100) * 100
            if hq >= 100:
                amt = ev["px"] * hq
                rule_net += amt - eng.sell_fee(amt)
                remain -= hq
                half_done = True
        elif ev_name == "min_surge_stall":
            if remain > 0:
                amt = ev["px"] * remain
                rule_net += amt - eng.sell_fee(amt)
                remain = 0.0
    if remain > 0:
        amt = rec["exit_px"] * remain * (1 - SLIP)
        rule_net += amt - eng.sell_fee(rec["exit_px"] * remain)
    out["actual_net"] = round(actual_net, 2)
    out["rule_net"] = round(rule_net, 2)
    out["improve"] = round(rule_net - actual_net, 2)
    out["improve_pct"] = round((rule_net - actual_net) / max(1.0, actual_net) * 100, 3)
    # 卖飞成本：触发价到原出场日收盘 / 出场日后第5个交易日收盘
    days_all = days
    try:
        xi = days_all.index(rec["exit_date"])
    except ValueError:
        xi = None
    fwd = {}
    for name, ev in (("shrink", fired_shrink), ("surge", fired_surge)):
        if not ev:
            continue
        d = {"to_exit_close": None, "to_exit5_close": None}
        if xi is not None:
            ex_closes = daily_closes_for(store, rec["code"])
            c_exit = ex_closes.get(rec["exit_date"])
            if c_exit:
                d["to_exit_close"] = round((c_exit / ev["px"] - 1) * 100, 2)
            j = xi + 5
            if j < len(days_all):
                c5 = ex_closes.get(days_all[j])
                if c5:
                    d["to_exit5_close"] = round((c5 / ev["px"] - 1) * 100, 2)
        fwd[name] = d
    out["flyaway"] = fwd
    return out


_DAILY_CLOSES_CACHE = {}


def daily_closes_for(store, code):
    """code -> {day: close}（日线，含窗口外延后 5 日）"""
    if code not in _DAILY_CLOSES_CACHE:
        conn = sqlite3.connect("file:%s?mode=ro" % C.DB_FILE, uri=True, timeout=15)
        m = {d: cl for d, cl in conn.execute(
            "SELECT date, close FROM kline WHERE period='day' AND code=? AND close>0 "
            "AND date>=?", (code, W_START))}
        conn.close()
        _DAILY_CLOSES_CACHE[code] = m
    return _DAILY_CLOSES_CACHE[code]


def agg_stats(rows):
    fired = [r for r in rows if r.get("fired")]
    improved = [r for r in fired if r.get("improve", 0) > 0]
    imps = [r["improve_pct"] for r in fired if r.get("improve_pct") is not None]
    fly_exit = [v["to_exit_close"] for r in fired
                for k, v in (r.get("flyaway") or {}).items() if v.get("to_exit_close") is not None]
    fly_5d = [v["to_exit5_close"] for r in fired
              for k, v in (r.get("flyaway") or {}).items() if v.get("to_exit5_close") is not None]
    def med(xs):
        if not xs:
            return None
        s = sorted(xs)
        n = len(s)
        return round(s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2, 3)
    return {
        "records": len(rows),
        "triggered": len(fired),
        "trigger_rate": round(len(fired) / max(1, len(rows)), 4),
        "improved": len(improved),
        "improve_ratio": round(len(improved) / max(1, len(fired)), 4) if fired else None,
        "mean_improve_pct": round(sum(imps) / len(imps), 3) if imps else None,
        "total_improve_yuan": round(sum(r.get("improve", 0) for r in fired), 0),
        "median_improve_pct": med(imps),
        "flyaway_to_exit_med_pct": med(fly_exit),
        "flyaway_to_exit5_med_pct": med(fly_5d),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-records", type=int, default=0, help="调试用：限制回放笔数")
    a = ap.parse_args()
    t0 = time.time()
    records = run_baseline()
    if a.max_records:
        records = records[:a.max_records]
    store = Min5Store()

    def scan_all(surge, pullback, shrink, rise):
        rows = []
        for rec in records:
            r = dict(rec)
            r.pop("name", None)
            r.update(scan_record(store, rec, surge, pullback, shrink, rise))
            rows.append(r)
        return rows

    print("== 现行参数回放 (SURGE=%.1f PULLBACK=%.3f SHIRNK=%.2f RISE=%.2f)" % (
        C.VOLP_SELL_MIN_SURGE, C.VOLP_SELL_MIN_PULLBACK,
        C.VOLP_SELL_MIN_SHRINK, C.VOLP_SELL_RISE_NEWHIGH), flush=True)
    base_rows = scan_all(C.VOLP_SELL_MIN_SURGE, C.VOLP_SELL_MIN_PULLBACK,
                         C.VOLP_SELL_MIN_SHRINK, C.VOLP_SELL_RISE_NEWHIGH)
    base_stats = agg_stats(base_rows)
    print(json.dumps(base_stats, ensure_ascii=False), flush=True)

    grid = []
    for s in GRID_SURGE:
        for p in GRID_PULLBACK:
            rows = scan_all(s, p, C.VOLP_SELL_MIN_SHRINK, C.VOLP_SELL_RISE_NEWHIGH)
            st = agg_stats(rows)
            same = (st["improve_ratio"] is not None
                    and st["improve_ratio"] >= RULE_MIN_IMPROVE_RATIO
                    and (st["mean_improve_pct"] or 0) > 0)
            st.update({"surge": s, "pullback": p, "passes_direction": same})
            grid.append(st)
            print("grid S=%.1f P=%.1f%%: trig=%d improve=%.1f%% mean=%s pass=%s" % (
                s, p * 100, st["triggered"],
                (st["improve_ratio"] or 0) * 100, st["mean_improve_pct"], same), flush=True)

    c1 = (base_stats["improve_ratio"] is not None
          and base_stats["improve_ratio"] >= RULE_MIN_IMPROVE_RATIO)
    c2 = (base_stats["mean_improve_pct"] is not None
          and base_stats["mean_improve_pct"] > 0)
    c3 = (sum(1 for g in grid if g["passes_direction"]) >= len(grid) * RULE_GRID_MAJORITY)
    verdict = {
        "rule": {"C1_improve_ratio_ge": RULE_MIN_IMPROVE_RATIO,
                 "C2_mean_improve_gt": 0,
                 "C3_grid_majority": RULE_GRID_MAJORITY},
        "C1_pass": c1, "C2_pass": c2, "C3_pass": c3,
        "recommend_enable": bool(c1 and c2 and c3),
        "reason": ("C1=%s C2=%s C3=%s" % (c1, c2, c3)) +
                 ("；建议开启（是否改 config 由验收方决定）"
                  if (c1 and c2 and c3) else "；维持关闭")},
    payload = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "phase": "32",
        "window": [W_START, W_END],
        "params_current": {"surge": C.VOLP_SELL_MIN_SURGE,
                           "pullback": C.VOLP_SELL_MIN_PULLBACK,
                           "shrink": C.VOLP_SELL_MIN_SHRINK,
                           "rise_newhigh": C.VOLP_SELL_RISE_NEWHIGH},
        "baseline_stats": base_stats,
        "baseline_rows_sample": [
            {k: r.get(k) for k in ("code", "entry_date", "entry_px", "exit_date",
                                   "exit_px", "reason", "fired", "events",
                                   "improve", "improve_pct")}
            for r in base_rows if r.get("fired")][:200],
        "grid": grid,
        "verdict": verdict,
        "notes": {
            "ratio_def": "分钟量比=当日截至bar累计量/已交易分钟 ÷ 前5交易日同时段每分钟量（minute_vol 复刻）",
            "t_plus_1": "规则仅入场次日及以后生效（与 trader 一致）",
            "slippage": SLIP,
        },
    }
    with open(OUT_JSON, "w", encoding="utf-8", newline="\n") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    print("\n判定:", json.dumps(verdict, ensure_ascii=False), flush=True)
    print("已写出 %s | 总耗时 %.0fs" % (OUT_JSON, time.time() - t0), flush=True)


if __name__ == "__main__":
    main()
