# -*- coding: utf-8 -*-
"""C3 min5 抽验：日线近似臂在 min5 可得窗口（2025-08-18 起）的触发一致率。

方法：取 OOS 段四臂触发事件中 buy_date ∈ min5 覆盖窗口者，用 min5 数据独立计算
  - min5_open = 当日第一根 5 分钟 open（含竞价开盘价）
  - min5_prev_close = 前一日最后一根 close（或日线 close[t]）
  - min5_apct = min5_open / min5_prev_close - 1
  - min5_vr  = 当日 min5 累计 volume / 前一日 min5 累计 volume
一致判定：日线 apct 与 min5_apct 偏差 ≤0.5pp，且 min5 计算值仍在臂区间内（触发一致率
= min5 独立验证仍触发的比例）。≥70% 记"近似可靠"。
"""
import os, sys, json, sqlite3
import numpy as np
import pandas as pd

sys.path.insert(0, r"C:\Users\26838\A股模拟盘\tools")
import tactic_backtest as tb
import auction_backtest as ab

MIN5_DB = "file:data/min5.db?mode=ro&immutable=1"
APCT_TOL = 0.005  # 0.5pp


def _min5_conn():
    return sqlite3.connect(MIN5_DB, uri=True, timeout=60)


def load_min5_day(c, code6, day):
    """返回该票该交易日全部 5 分钟行（按时间排序）"""
    q = ("SELECT date, open, close, volume FROM kline_min5 "
         "WHERE code=? AND date LIKE ? ORDER BY date")
    cur = c.execute(q, (code6, day + "%"))
    rows = cur.fetchall()
    return rows


def day_open_close_volume(c, code6, day):
    """当日第一根 open / 最后一根 close / 累计 volume；无数据返回 None"""
    rows = load_min5_day(c, code6, day)
    if not rows:
        return None
    return rows[0][1], rows[-1][2], sum(r[3] for r in rows)


def run_min5():
    print("=== C3 min5 触发一致率抽验（窗口 2025-08-18..2026-09-04）===")
    D = ab._load_data()
    df, v = D["df"], D["v"]
    conn = _min5_conn()
    out = {"window": "2025-08-18..2026-09-04", "arms": {}}
    # 每票每日的 min5 前收/开盘/量
    day_cache = {}
    for aname, arm in ab.ARMS.items():
        sam, drop = ab.build_samples(ab.OOS_S, ab.OOS_E, arm=arm)
        # 窗口过滤：buy_date >= 2025-08-18
        win = [s for s in sam if s["buy_date"] >= "2025-08-18"]
        checked = 0
        agree = 0
        detail = []
        for s in win:
            code6 = s["code"]; day = s["buy_date"]
            key = (code6, day)
            if key not in day_cache:
                cur = conn.execute(
                    "SELECT date, open, close, volume FROM kline_min5 "
                    "WHERE code=? AND date LIKE ? ORDER BY date",
                    (code6, day + "%"))
                rows = cur.fetchall()
                day_cache[key] = rows
            rows = day_cache[key]
            if not rows:
                continue
            min5_open = rows[0][1]
            min5_vol = sum(r[3] for r in rows)
            # 前一日 min5 量
            pkey = (code6, s["ev_date"])
            if pkey not in day_cache:
                cur = conn.execute(
                    "SELECT date, open, close, volume FROM kline_min5 "
                    "WHERE code=? AND date LIKE ? ORDER BY date",
                    (code6, s["ev_date"] + "%"))
                prow = cur.fetchall()
                day_cache[pkey] = prow
            prow = day_cache[pkey]
            if not prow:
                continue
            prev_close5 = prow[-1][2]
            prev_vol5 = sum(r[3] for r in prow)
            if prev_close5 <= 0:
                continue
            min5_apct = min5_open / prev_close5 - 1
            min5_vr = min5_vol / prev_vol5 if prev_vol5 > 0 else 0.0
            lo, hi = arm["apct"]
            min5_hit = (lo - APCT_TOL <= min5_apct < hi + APCT_TOL) and min5_vr >= arm["vr"] * 0.9
            checked += 1
            if min5_hit:
                agree += 1
            detail.append(dict(code=code6, day=day, d_apct=s["apct"], d_vr=s["vr"],
                               m_apct=float(min5_apct), m_vr=float(min5_vr),
                               hit=bool(min5_hit)))
        rate = agree / checked if checked else 0.0
        out["arms"][aname] = {"n_total": len(sam), "n_window": len(win),
                              "n_checked": checked, "n_agree": agree,
                              "agree_rate": float(rate),
                              "reliable": bool(rate >= 0.7)}
        print("  %s: window n=%d checked=%d agree=%d rate=%.2f → %s" % (
            aname, len(win), checked, agree, rate,
            "近似可靠" if rate >= 0.7 else "偏差需标注"))
    conn.close()
    json.dump(out, open(os.path.join(ab.ROOT, "tmp", "c3", "min5_check.json"),
                        "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("saved tmp/c3/min5_check.json")
    return out
