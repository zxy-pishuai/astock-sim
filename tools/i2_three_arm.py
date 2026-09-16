# -*- coding: utf-8 -*-
"""I2 三臂对照回放：旧实盘口径 / 旧回测口径 / 新统一口径
同一时间窗（2026-06-01~08-31）同一 threshold，量化口径不一致的差异。
- A 旧实盘口径：prescreen 粗筛 + score_final(ctx=回测重建加分)（模拟 trader）
- B 旧回测口径：全候选 + score_final(ctx=None)（= 现 engine 默认 SCORING_UNIFIED=False）
- C 新统一口径：prescreen + score_final(ctx=重建)（= SCORING_UNIFIED=True）
交易：T 日收盘信号 → T+1 开盘买入 → 持有 5 交易日收盘卖；成本双边 0.29%（T+1 规则）。
输出三臂 total_return/trade_count/win_rate + 每日选股 Jaccard 重合度。
用法：py -3.13 tools/i2_three_arm.py
"""
import os
import sqlite3
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from app import scoring as sc  # noqa: E402

DB = os.path.join(_ROOT, "data", "snapshots", "2026-09-02", "market.db")
START, END = "2026-06-01", "2026-08-31"
THRESHOLD = 60
HOLD = 5
COST = 0.0029  # 双边合计

_conn = None


def conn():
    global _conn
    if _conn is None:
        _conn = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
    return _conn


def trading_days(start, end):
    rows = conn().execute(
        "SELECT DISTINCT date FROM kline WHERE period='day' AND code='000001' "
        "AND date>=? AND date<=? ORDER BY date", (start, end)).fetchall()
    return [r[0] for r in rows]


def universe_on(date):
    """信号日宇宙：主板(60/00)、价格[5,150]、当日非涨跌停、K线≥300根。"""
    rows = conn().execute(
        "SELECT code, date, open, high, low, close, volume, amount FROM kline "
        "WHERE period='day' AND date=? AND (code LIKE '60%' OR code LIKE '00%')",
        (date,)).fetchall()
    out = []
    for r in rows:
        code, d, o, h, l, c, v, amt = r
        if not (5 <= c <= 150):
            continue
        yc = conn().execute(
            "SELECT close FROM kline WHERE period='day' AND code=? AND date<? "
            "ORDER BY date DESC LIMIT 1", (code, date)).fetchone()
        if not yc or yc[0] <= 0:
            continue
        pct = (c - yc[0]) / yc[0] * 100
        if pct >= 9.7 or pct <= -9.7:
            continue
        out.append((code, {"pct_chg": pct, "close": c, "open": o, "high": h,
                           "low": l, "volume": v, "amount": amt or 0}))
    return out


_kl_cache = {}


def klines_of(code, date, days=300):
    key = (code, date)
    if key in _kl_cache:
        return _kl_cache[key]
    rows = conn().execute(
        "SELECT date,open,high,low,close,volume,amount FROM kline "
        "WHERE period='day' AND code=? AND date<=? ORDER BY date DESC LIMIT ?",
        (code, date, days)).fetchall()
    rows = list(reversed(rows))
    kl = [{"date": r[0], "open": r[1], "high": r[2], "low": r[3],
           "close": r[4], "volume": r[5], "amount": r[6] or 0} for r in rows]
    _kl_cache[key] = kl
    return kl


def next_day(date, days_list):
    for d in days_list:
        if d > date:
            return d
    return None


def build_ctx(date, quotes):
    """回测 ctx 重建：板块强度/龙头用信号日行情（PIT），focus 空（无历史 watchlist）。"""
    strength = sc.calc_sector_strength(quotes)
    top = sc.get_top_sectors(strength)
    strong = {s[0] for s in top}
    leads = set()
    for _, info in top:
        for c, n, p in info.get("leaders", []):
            leads.add(c)
    return {"focus": set(), "strong_sectors": strong, "leaders": leads}


def run_arm(days, arm):
    """arm: 'A'|'B'|'C'。返回 (trades, picks_by_date)。"""
    trades = []
    picks = {}
    for date in days:
        univ = universe_on(date)
        if not univ:
            continue
        quotes = {c: q for c, q in univ}
        ctx = build_ctx(date, quotes) if arm in ("A", "C") else None
        # 候选集
        if arm in ("A", "C"):
            pool = sc.prescreen(list(univ))
            cands = [(c, q) for c, q in univ if c in pool]
        else:
            cands = univ
        # 评分
        sel = []
        for code, q in cands:
            kl = klines_of(code, date)
            if len(kl) < 60:
                continue
            if arm == "B":
                s, _sig = sc.score_stock(kl, code=code, as_of=date)
            else:
                s, _sig, _bd = sc.score_final(kl, q, code=code,
                                              name=code, as_of=date, ctx=ctx)
            if s >= THRESHOLD:
                sel.append((code, s, q))
        sel.sort(key=lambda x: -x[1])
        picks[date] = {c for c, _, _ in sel}
        # 执行：T+1 开盘买入，持有 HOLD 日
        nd = next_day(date, days)
        if nd is None or not sel:
            continue
        for code, s, q in sel:
            kl_next = klines_of(code, nd)
            if len(kl_next) < 2:
                continue
            buy = kl_next[-1]["open"]
            if buy <= 0:
                continue
            # 找持有期末日
            tgt = None
            for d2 in days:
                if d2 > nd:
                    tgt = d2
                    if len([x for x in days if x > nd and x <= d2]) >= HOLD:
                        break
            if tgt is None:
                continue
            kl_end = klines_of(code, tgt)
            if len(kl_end) < 2:
                continue
            sell = kl_end[-1]["close"]
            ret = sell / buy - 1 - COST
            trades.append({"code": code, "date": date, "buy": buy,
                           "sell": sell, "ret": ret})
        if len(picks) % 15 == 0:
            print("  [%s] %s 已评分 %d 日" % (arm, date, len(picks)), flush=True)
    return trades, picks


def jaccard(a, b):
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def main():
    t0 = time.time()
    days = trading_days(START, END)
    print("窗口 %s~%s，交易日 %d 天" % (START, END, len(days)))
    res = {}
    all_picks = {}
    for arm in ("A", "B", "C"):
        print("运行 %s 臂..." % arm, flush=True)
        tr, pk = run_arm(days, arm)
        res[arm] = tr
        all_picks[arm] = pk
        n = len(tr)
        wr = sum(1 for x in tr if x["ret"] > 0) / n if n else 0
        tr_avg = sum(x["ret"] for x in tr) / n if n else 0
        tr_total = sum(x["ret"] for x in tr)
        print("  %s: 笔数=%d 胜率=%.2f%% 单笔平均=%.3f%% 简单合计=%.1f%%"
              % (arm, n, wr * 100, tr_avg * 100, tr_total * 100))
    # Jaccard：A vs B / A vs C / B vs C（按日平均）
    print("\n=== 每日选股重合度（Jaccard，按日平均）===")
    for p, q in (("A", "B"), ("A", "C"), ("B", "C")):
        js = [jaccard(all_picks[p].get(d, set()), all_picks[q].get(d, set()))
              for d in days if all_picks[p].get(d) or all_picks[q].get(d)]
        print("  %s vs %s: %.3f（%d 个信号日）" % (p, q, sum(js) / len(js) if js else 0, len(js)))
    print("\n总耗时 %.0fs" % (time.time() - t0))


if __name__ == "__main__":
    main()
