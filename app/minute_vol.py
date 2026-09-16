# -*- coding: utf-8 -*-
"""★ 4.4 分时量加速：盘中分钟级量能确认模块
问题背景：打板/自选监控之前只用 quote 的日级 vol_ratio（腾讯 f49），
没有分钟级量能确认 —— 冲高回落前"放量滞涨"、追涨前"缩量拉升"都看不见。

方案（零额外网络请求）：
  当日分钟量 = quote.volume（当日累计量）÷ 已交易分钟数
  前5日同时段均量 = min5.db 本地一年 5 分钟数据（同一时段累计，SQL 一次查询）
  时段量比 = 当日每分钟均量 ÷ 前5日同时段每分钟均量

输出：
  minute_vol_ratio()  时段量比（<0.7 量能不足，借鉴 limit-up-sniper 硬过滤）
  minute_vol_check()  分钟量能健康度判定（量能不足/温和/健康/爆量）
  vp_minute_signal()  冲高回落/追涨风险预警（放量滞涨 / 缩量拉升）
"""
import sqlite3
import time

from . import config as C
from . import datafeed as df
from . import db as _db            # ★ J3：统一连接工厂

# 内存缓存：{(code, day, 时段): (ts, 基准量)}，5 分钟有效
_min5_base_cache = {}
_MIN5_BASE_TTL = 300

# ★ 4.5：前N交易日列表按日缓存 —— 原实现每次调用都对 470 万行 min5 全表
#   DISTINCT substr 扫描（实测 3s/次，扫描循环里每只候选都触发一次 → 拖死扫描）
_prev_days_cache = {"day": "", "val": []}


def _min5_conn():
    try:
        conn = _db.open_rw(C.MIN5_DB_FILE)   # ★ J3：统一连接工厂（WAL 初始化已由 db.py 一次完成）
        return conn
    except Exception:
        return None


def _trading_minutes_elapsed():
    """当日已交易分钟数（9:30-11:30 + 13:00-15:00），非交易时段按整日240算"""
    now = time.localtime()
    hm = now.tm_hour * 100 + now.tm_min
    if hm < 930:
        return 0
    if 930 <= hm <= 1130:
        return (now.tm_hour - 9) * 60 + now.tm_min - 30
    if 1130 < hm < 1300:
        return 120
    if 1300 <= hm <= 1500:
        return 120 + (now.tm_hour - 13) * 60 + now.tm_min
    return 240


def _prev_n_trading_days(n=5):
    """最近 n 个交易日日期列表（不含今天），优先 min5.db 实际数据（★ 按日缓存）"""
    today = time.strftime("%Y-%m-%d")
    if _prev_days_cache["day"] == today and len(_prev_days_cache["val"]) >= n:
        return _prev_days_cache["val"][:n]
    try:
        conn = _min5_conn()
        if conn:
            rows = conn.execute(
                "SELECT DISTINCT substr(date,1,10) FROM kline_min5 "
                "WHERE substr(date,1,10)<? ORDER BY 1 DESC LIMIT ?",
                (today, n)).fetchall()
            conn.close()
            if len(rows) >= n:
                _prev_days_cache["day"] = today
                _prev_days_cache["val"] = [r[0] for r in rows]
                return _prev_days_cache["val"][:n]
    except Exception:
        pass
    # 兜底：交易日历
    try:
        from . import trading_calendar as tcal
        out = []
        d = tcal.prev_trading_day(today)
        while d and len(out) < n:
            out.append(d)
            d = tcal.prev_trading_day(d)
        if out:
            _prev_days_cache["day"] = today
            _prev_days_cache["val"] = out
        return out
    except Exception:
        return []


def _same_period_base(code, minutes_elapsed):
    """前5日同时段累计量均值（本地 min5.db，一次 SQL）。
    返回 每分钟均量（0 表示无数据）。缓存 5 分钟。
    """
    today = time.strftime("%Y-%m-%d")
    key = (code, today, minutes_elapsed)
    now = time.time()
    hit = _min5_base_cache.get(key)
    if hit and now - hit[0] < _MIN5_BASE_TTL:
        return hit[1]
    base = 0.0
    try:
        days = _prev_n_trading_days(5)
        if not days:
            return 0.0
        sym = code if code.startswith(("sh", "sz", "bj")) else df._prefix(code)
        # 当前时段对应的时间字符串（如 "10:00"）
        hm = time.localtime()
        cur = "%02d:%02d" % (hm.tm_hour, hm.tm_min)
        conn = _min5_conn()
        if conn:
            total = 0.0
            for d in days:
                row = conn.execute(
                    "SELECT COALESCE(SUM(volume),0) FROM kline_min5 "
                    "WHERE code=? AND date LIKE ? AND substr(date,12,5)<=?",
                    (sym, d + "%", cur)).fetchone()
                total += row[0] if row else 0.0
            conn.close()
            if total > 0:
                base = total / (len(days) * minutes_elapsed) if minutes_elapsed > 0 else 0.0
    except Exception:
        base = 0.0
    _min5_base_cache[key] = (now, base)
    return base


def minute_vol_ratio(code, quote):
    """时段量比：当日每分钟均量 ÷ 前5日同时段每分钟均量。
    quote: 实时行情（需 volume 当日累计量）。返回 float（0=数据不足）。
    """
    vol = (quote.get("volume") or 0)
    if vol <= 0:
        return 0.0
    minutes = _trading_minutes_elapsed()
    if minutes <= 0:
        return 0.0
    per_min = vol / minutes
    base = _same_period_base(code, minutes)
    if base <= 0:
        return 0.0
    return per_min / base


def minute_vol_check(code, quote):
    """分钟量能健康度：返回 (ratio, verdict)。
    ratio<0.7 量能不足（追涨危险） / 0.7~1.5 温和 / 1.5~3 健康放量 / >3 爆量（警惕分歧）
    """
    ratio = minute_vol_ratio(code, quote)
    if ratio <= 0:
        return 0.0, "无分时基准"
    if ratio < 0.7:
        return ratio, f"量能不足({ratio:.2f})"
    if ratio < 1.5:
        return ratio, f"量能温和({ratio:.2f})"
    if ratio < 3.0:
        return ratio, f"放量确认({ratio:.2f})"
    return ratio, f"爆量分歧({ratio:.2f})"


def vp_minute_signal(code, quote, klines=None):
    """★ 4.4 冲高回落/追涨风险预警（分钟量能 + 价格位置结合）。
    quote: 实时行情。klines: 可选日K（用于判断近期高点）。
    返回 (signal_text, level) 或 (None, None)。
    """
    try:
        ratio, _ = minute_vol_check(code, quote)
        if ratio <= 0:
            return None, None
        pct = quote.get("pct_chg", 0) or 0
        price = quote.get("price", 0) or 0
        # 场景1：冲高回落预警 —— 涨幅已高 + 量能萎缩（高位缩量=承接不足）
        if pct >= 5.0 and ratio < 0.7:
            return (f"⚠高位缩量({ratio:.2f}) 冲高回落风险（涨幅{pct:.1f}%量能跟不上）", "WARN")
        # 场景2：追涨打板预警 —— 涨幅中高 + 爆量分歧（放量滞涨/对倒出货嫌疑）
        if 3.0 <= pct < 9.5 and ratio > 3.0:
            return (f"⚠爆量分歧({ratio:.2f}) 涨幅{pct:.1f}%量能暴增，谨防对倒出货", "WARN")
        # 场景3：健康信号 —— 放量拉升（量价配合，可关注）
        if pct >= 2.0 and 1.5 <= ratio <= 3.0:
            return (f"量价配合({ratio:.2f}) 放量拉升", "OK")
        return None, None
    except Exception:
        return None, None


def minute_volp_sell(code, quote):
    """★ 4.6 盘中量价背离卖出（秒级实时 + 分钟量比，不依赖日K；config.VOLP_SELL_MIN_ENABLED 控制）
    判据：
      - 放量滞涨（对倒派发）：分钟量比 > VOLP_SELL_MIN_SURGE 且 价格自当日高点回落 ≥VOLP_SELL_MIN_PULLBACK → 清仓
      - 高位缩量上冲（无量上涨）：日内涨幅 ≥VOLP_SELL_RISE_NEWHIGH 且 分钟量比 <VOLP_SELL_MIN_SHRINK → 卖半仓锁利
    秒级调用用 quote 实时 volume/pct_chg/high；分钟量比有 5 分钟缓存，不卡轮询。
    返回 (signal_key, sell_ratio, reason) 或 (None, 0.0, None)。
    """
    try:
        from . import config as _C
        if not _C.VOLP_SELL_MIN_ENABLED:
            return None, 0.0, None
        ratio, _ = minute_vol_check(code, quote)
        if ratio <= 0:
            return None, 0.0, None
        pct = quote.get("pct_chg", 0) or 0
        price = quote.get("price", 0) or 0
        high = quote.get("high", 0) or 0
        # ---- 放量滞涨：分钟爆量 + 价格自当日高点明显回落（涨不动了）→ 清仓 ----
        if ratio > _C.VOLP_SELL_MIN_SURGE and high > 0 and price > 0:
            pull = (high - price) / high
            if pull >= _C.VOLP_SELL_MIN_PULLBACK:
                return "min_surge_stall", 1.0, (
                    f"分钟爆量滞涨(量比{ratio:.2f}×价回{pull*100:.1f}%) 对倒 清仓")
        # ---- 高位缩量上冲：涨幅达标 + 分钟量能不足（无量上涨）→ 卖半仓锁利 ----
        if pct >= _C.VOLP_SELL_RISE_NEWHIGH and ratio < _C.VOLP_SELL_MIN_SHRINK:
            return "min_shrink_rise", 0.5, (
                f"高位缩量上冲(涨{pct:.1f}%但分钟量比{ratio:.2f}不足) 卖半仓锁利")
        return None, 0.0, None
    except Exception:
        return None, 0.0, None


def clear_cache():
    _min5_base_cache.clear()
