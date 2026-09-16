# -*- coding: utf-8 -*-
"""★ Phase16：重建全历史情绪序列（2019-01-01 起），落库 qg_sentiment_history
方法（与 app/sentiment_series.py 一致）：
  - 涨停检测：前复权日K，主板>=9.2%、创业科创>=19.2%（9.5/19.5 留容差）
  - 每日情绪分 sentiment_score = min(100, zt_count*1.2)
  - 昨日涨停今日溢价 prev_zt_premium（情绪温度计）
  - 阶段标签：用 app/sentiment._phase_of 规则（冰点/发酵/高潮/退潮）
统计：
  - 各阶段"次日收益 / 未来5日收益 / 上涨概率" —— 全市场等权口径
    （库内该日有收盘价的全部股票等权平均收益）
  - 样本数 <30 的阶段标注置信度低

落库（新表 qg_ 前缀，不动现有表）：
  qg_sentiment_history(period TEXT PRIMARY KEY, zt_count REAL, prev_zt_premium REAL,
                       sentiment_score REAL, phase TEXT, date_count INT, payload TEXT)
  —— 单行快照存最近状态 + 历史统计块，避免巨表

用法: python tools/build_sentiment_history.py [--start 2019-01-01] [--dry]
"""
import argparse
import json
import os
import sqlite3
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DB = os.path.join(BASE, "data", "market.db")
TABLE = "qg_sentiment_history"
FULL_TABLE = "qg_zt_full"   # 每日涨停池（可选落库，供复核；默认只统计不落全量）


def _ro_conn():
    return sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=15)


def load_kline_all(start="2019-01-01"):
    """一次性读取全市场 day 日K（code, date, close），返回 {code: [(date, close)]}"""
    conn = _ro_conn()
    rows = conn.execute(
        "SELECT code, date, close FROM kline WHERE period='day' AND date>=? "
        "AND close IS NOT NULL ORDER BY code, date",
        (start,)).fetchall()
    conn.close()
    by_code = {}
    for code, d, c in rows:
        by_code.setdefault(code, []).append((d, c))
    return by_code


def detect_zt_dates(code, seq):
    """基于 (date, close) 序列检测涨停日期。返回 {date: pct}"""
    lim = 19.2 if code.startswith(("30", "68")) else 9.2
    prev = None
    out = {}
    for d, c in seq:
        if prev is not None and prev > 0:
            pct = (c - prev) / prev * 100
            if pct >= lim:
                out[d] = round(pct, 2)
        prev = c
    return out


def main(start="2019-01-01", dry=False):
    t0 = time.time()
    print("读取全市场日K（%s 起）..." % start, flush=True)
    by_code = load_kline_all(start)
    # ★ Phase21: 过滤复权不可靠的例外票（DATA_EXCLUDE_CODES）
    try:
        from app import config as C
        _ex = set(getattr(C, "DATA_EXCLUDE_CODES", None) or [])
        if _ex:
            by_code = {c: v for c, v in by_code.items() if c not in _ex}
    except Exception:
        pass
    codes = list(by_code.keys())
    print("  股票数:", len(codes), flush=True)

    # 1. 检测每日涨停池
    print("检测涨停池...", flush=True)
    zt_by_day = {}          # date -> {code: pct}
    for code, seq in by_code.items():
        for d, pct in detect_zt_dates(code, seq).items():
            zt_by_day.setdefault(d, {})[code] = pct
    dates = sorted(zt_by_day.keys())
    print("  涨停日: %d 天（%s ~ %s）" % (len(dates), dates[0] if dates else "?", dates[-1] if dates else "?"), flush=True)

    # 2. 每日情绪指标 + 昨日涨停溢价
    #    昨涨停今日溢价：昨 zt 股列表 → 今日 close/昨close-1（全市场等权近似）
    prev_zt_codes = set()
    series = []
    for d in dates:
        zt = zt_by_day.get(d, {})
        prev_premium = None
        if prev_zt_codes:
            rets = []
            for c in list(prev_zt_codes)[:100]:
                # 找 c 在 d 的前一段 close（用 by_code 序列查 d 及之前）
                seq = by_code.get(c)
                if seq is None:
                    continue
                prev_c = None
                cur_c = None
                for dd, cc in seq:
                    if dd < d:
                        prev_c = cc
                    elif dd == d:
                        cur_c = cc
                        break
                if prev_c and cur_c and prev_c > 0:
                    rets.append((cur_c - prev_c) / prev_c)
            if rets:
                prev_premium = round(sum(rets) / len(rets), 4)
        score = round(min(100, len(zt) * 1.2), 1)
        series.append({"date": d, "zt_count": len(zt),
                       "prev_zt_premium": prev_premium, "sentiment_score": score})
        prev_zt_codes = set(zt.keys())

    # 3. 阶段标签（复用 sentiment._phase_of 规则，含连板高度追踪）
    #    重建连板高度：从 zt_by_day 追踪每只票连续涨停天数 → 每日空间板
    print("追踪连板高度...", flush=True)
    streak = {}       # code -> 当前连续涨停天数
    day_max_days = {} # date -> 当日最大连板
    for d in dates:
        cur_zt = zt_by_day.get(d, {})
        mx = 0
        new_streak = {}
        for c in cur_zt:
            s = streak.get(c, 0) + 1
            new_streak[c] = s
            mx = max(mx, s)
        day_max_days[d] = mx
        streak = new_streak
        # 未涨停的票连板中断（下一天开始 streak 自然清空）
    from app import sentiment as senti
    for s in series:
        d = s["date"]
        zt = s["zt_count"]
        yzt = s["prev_zt_premium"]
        mx = day_max_days.get(d, 0)
        # 用真实 _phase_of 规则（跌停数未知 → 用 0，冰点判定仅 zt<25）
        if zt < 25:
            phase = "冰点"
        elif zt >= 60 and mx >= 5:
            phase = "高潮"
        elif yzt is not None and yzt < -0.02 and mx <= 3:
            phase = "退潮"
        else:
            phase = "发酵"
        s["phase"] = phase
        s["max_days"] = mx

    print("阶段分布:", flush=True)
    from collections import Counter
    cnt = Counter(s["phase"] for s in series)
    for ph, n in cnt.items():
        print("  %s: %d 天" % (ph, n), flush=True)

    # 4. 各阶段收益统计（全市场等权：该日库内全股票平均次日/5日收益）
    print("统计各阶段收益（全市场等权）...", flush=True)
    # 预取每日全市场平均收益（仓内全部股票 next-day / 5-day 收益）
    # 用 by_code 序列按日计算
    all_days_idx = {d: i for i, d in enumerate(sorted(set(d for code in by_code for d, _ in by_code[code])))}
    # 简化：对每只票算 (date -> next1, next5)，再按日聚合
    day_next1 = {}
    day_next5 = {}
    day_up1 = {}
    for code, seq in by_code.items():
        closes = {d: c for d, c in seq}
        for i, (d, c) in enumerate(seq):
            nxt = seq[i + 1] if i + 1 < len(seq) else None
            nxt5 = seq[i + 5] if i + 5 < len(seq) else None
            if c and c > 0:
                if nxt and nxt[1]:
                    r1 = (nxt[1] - c) / c
                    d1 = day_next1.setdefault(d, [0.0, 0])
                    d1[0] += r1; d1[1] += 1
                    day_up1.setdefault(d, [0, 0])
                    day_up1[d][0] += 1 if r1 > 0 else 0
                    day_up1[d][1] += 1
                if nxt5 and nxt5[1]:
                    r5 = (nxt5[1] - c) / c
                    d5 = day_next5.setdefault(d, [0.0, 0])
                    d5[0] += r5; d5[1] += 1

    stats = {}
    for s in series:
        d = s["date"]
        st = stats.setdefault(s["phase"], {"n": 0, "next1_sum": 0.0, "next5_sum": 0.0,
                                           "up1": 0, "n1": 0})
        st["n"] += 1
        if d in day_next1 and day_next1[d][1] > 0:
            st["next1_sum"] += day_next1[d][0] / day_next1[d][1]
            st["n1"] += 1
            st["up1"] += day_up1[d][0] / max(1, day_up1[d][1])
        if d in day_next5 and day_next5[d][1] > 0:
            st["next5_sum"] += day_next5[d][0] / day_next5[d][1]

    result_stats = {}
    for ph, st in stats.items():
        n = st["n"]
        avg1 = st["next1_sum"] / max(1, st["n1"])
        avg5 = st["next5_sum"] / max(1, st["n"])
        up1 = st["up1"] / max(1, st["n1"])
        result_stats[ph] = {
            "samples": n,
            "next1_avg": round(avg1, 5),
            "next5_avg": round(avg5, 5),
            "up1_ratio": round(up1, 4),
            "low_confidence": n < 30,
            "n1_used": st["n1"],
        }
    print("\n===== 阶段收益统计（全市场等权）=====", flush=True)
    for ph, st in sorted(result_stats.items()):
        lc = " ⚠️低置信" if st["low_confidence"] else ""
        print("  %s: n=%d 次日%+.3f%% 5日%+.3f%% 次日上涨%.1f%%%s" % (
            ph, st["samples"], st["next1_avg"] * 100, st["next5_avg"] * 100,
            st["up1_ratio"] * 100, lc), flush=True)

    # 5. 落库（逐日表 + 单行快照）
    if not dry:
        conn = sqlite3.connect(DB, timeout=60)
        conn.execute("PRAGMA journal_mode=WAL")
        # 逐日情绪序列表：回测按日查阶段用
        try:
            conn.execute("DROP TABLE IF EXISTS %s" % FULL_TABLE)
        except Exception:
            pass
        conn.execute(
            "CREATE TABLE IF NOT EXISTS %s("
            "date TEXT PRIMARY KEY, zt_count INT, max_days INT, "
            "prev_zt_premium REAL, sentiment_score REAL, phase TEXT)" % FULL_TABLE)
        conn.executemany(
            "INSERT OR REPLACE INTO %s(date,zt_count,max_days,prev_zt_premium,"
            "sentiment_score,phase) VALUES(?,?,?,?,?,?)" % FULL_TABLE,
            [(s["date"], s["zt_count"], s.get("max_days", 0),
              s.get("prev_zt_premium"), s["sentiment_score"], s["phase"]) for s in series])
        conn.execute(
            "CREATE TABLE IF NOT EXISTS %s("
            "period TEXT PRIMARY KEY, zt_count REAL, prev_zt_premium REAL, "
            "sentiment_score REAL, phase TEXT, date_count INT, payload TEXT)" % TABLE)
        payload = {
            "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "start": start, "end": dates[-1] if dates else "",
            "dates": len(dates),
            "phase_stats": result_stats,
            "methods": "sentiment_series + _phase_of(含连板); 全市场等权口径",
        }
        conn.execute(
            "INSERT OR REPLACE INTO %s(period,zt_count,prev_zt_premium,sentiment_score,"
            "phase,date_count,payload) VALUES('2019_%s',?,?,?,?,?,?)" % (TABLE, dates[-1] if dates else ""),
            (0.0, None, 0.0, "全周期", len(dates), json.dumps(payload, ensure_ascii=False)))
        conn.commit()
        conn.close()
        print("\n已落库 %s（逐日） + %s（快照）" % (FULL_TABLE, TABLE), flush=True)
    else:
        print("\n[dry] 未写库", flush=True)
    print("总耗时 %.1fs" % (time.time() - t0), flush=True)
    return result_stats


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2019-01-01")
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    main(start=a.start, dry=a.dry)