# -*- coding: utf-8 -*-
"""交易日历（4.2）—— 解决"自然日≠交易日"导致的回测/实盘错位
A股节假日规则：周六日恒休（含调休补班周末）+ 法定节假日。
节假日表本地维护（rainx/cn_stock_holidays 风格），每年初更新一次。
功能：
  - is_trading_day(date)         是否交易日
  - next_trading_day(date)       下一交易日
  - prev_trading_day(date)       上一交易日
  - trading_days(start, end)     区间交易日序列（含两端）
"""
import os
import json

from . import config as C

# ★ 2025-2026 沪深节假日（含调休补班；每年更新）
# 格式：{"YYYY-MM-DD": "节假日名称"}，仅列非周末的休市日
HOLIDAYS = {
    # 2026
    "2026-01-01": "元旦",
    "2026-02-16": "春节", "2026-02-17": "春节", "2026-02-18": "春节",
    "2026-02-19": "春节", "2026-02-20": "春节", "2026-02-23": "春节",
    "2026-04-06": "清明",
    "2026-05-01": "劳动节", "2026-05-04": "劳动节", "2026-05-05": "劳动节",
    "2026-06-19": "端午",
    "2026-09-25": "中秋",
    "2026-10-01": "国庆", "2026-10-02": "国庆", "2026-10-05": "国庆",
    "2026-10-06": "国庆", "2026-10-07": "国庆", "2026-10-08": "国庆",
    # 2025
    "2025-01-01": "元旦",
    "2025-01-28": "春节", "2025-01-29": "春节", "2025-01-30": "春节",
    "2025-01-31": "春节", "2025-02-03": "春节", "2025-02-04": "春节",
    "2025-04-04": "清明",
    "2025-05-01": "劳动节", "2025-05-02": "劳动节", "2025-05-05": "劳动节",
    "2025-06-02": "端午",
    "2025-10-01": "国庆", "2025-10-02": "国庆", "2025-10-03": "国庆",
    "2025-10-06": "国庆", "2025-10-07": "国庆", "2025-10-08": "国庆",
}

# 调休补班日（周末上班）。★ A股周末恒休市（交易所不跟随调休），此表仅作存档，不参与交易日判定
WORKDAYS = {
    "2026-02-14": "春节调休补班", "2026-02-28": "春节调休补班",
    "2026-10-10": "国庆调休补班",
    "2025-01-26": "春节调休补班", "2025-02-08": "春节调休补班",
    "2025-09-28": "国庆调休补班", "2025-10-11": "国庆调休补班",
}

# 可扩展：data/ 下的交易日历覆盖文件（用户可更新）
_CAL_FILE = os.path.join(C.DATA_DIR, "trading_calendar.json")


def _load_user_calendar():
    """读取用户自定义日历覆盖（可选）"""
    try:
        if os.path.exists(_CAL_FILE):
            with open(_CAL_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
            return d.get("holidays", {}), d.get("workdays", {})
    except Exception:
        pass
    return {}, {}


_user_holidays, _user_workdays = _load_user_calendar()

# ★ 节假日表年份覆盖检查：未覆盖年份（如 2027 起）警告一次，工作日按交易日处理
_covered_years = {int(k[:4]) for k in HOLIDAYS} | {int(k[:4]) for k in _user_holidays}
_warned_years = set()


def _warn_uncovered_year(y):
    if y in _covered_years or y in _warned_years:
        return
    _warned_years.add(y)
    print('[trading_calendar] 警告：%d 年节假日表未登记（当前覆盖 %s），该年所有工作日将按交易日处理，请尽快更新 HOLIDAYS 或 data/trading_calendar.json' % (y, sorted(_covered_years)))


def is_trading_day(date_str):
    """是否交易日。date_str: 'YYYY-MM-DD'"""
    from datetime import date
    try:
        y, m, d = map(int, date_str.split("-"))
        dt = date(y, m, d)
    except (ValueError, AttributeError):
        return False
    if dt.weekday() >= 5:
        return False   # ★ 周末恒休市（含调休补班周末；沪深交易所不跟随国务院调休）
    _warn_uncovered_year(y)
    # 工作日：节假日休市
    return date_str not in HOLIDAYS and date_str not in _user_holidays


def _shift(date_str, delta):
    """从 date_str 出发，返回跳过非交易日的日期"""
    from datetime import date, timedelta
    y, m, d = map(int, date_str.split("-"))
    dt = date(y, m, d)
    step = 1 if delta > 0 else -1
    for _ in range(abs(delta) * 5 + 5):  # 安全上限
        dt += timedelta(days=step)
        if is_trading_day(dt.strftime("%Y-%m-%d")):
            delta -= step
            if delta == 0:
                return dt.strftime("%Y-%m-%d")
    return date_str


def next_trading_day(date_str):
    """下一交易日"""
    from datetime import date, timedelta
    y, m, d = map(int, date_str.split("-"))
    dt = date(y, m, d) + timedelta(days=1)
    for _ in range(10):
        if is_trading_day(dt.strftime("%Y-%m-%d")):
            return dt.strftime("%Y-%m-%d")
        dt += timedelta(days=1)
    return date_str


def prev_trading_day(date_str):
    """上一交易日"""
    from datetime import date, timedelta
    y, m, d = map(int, date_str.split("-"))
    dt = date(y, m, d) - timedelta(days=1)
    for _ in range(10):
        if is_trading_day(dt.strftime("%Y-%m-%d")):
            return dt.strftime("%Y-%m-%d")
        dt -= timedelta(days=1)
    return date_str


def trading_days(start, end):
    """区间内交易日列表（含两端，升序）"""
    from datetime import date, timedelta
    y1, m1, d1 = map(int, start.split("-"))
    y2, m2, d2 = map(int, end.split("-"))
    dt, end_dt = date(y1, m1, d1), date(y2, m2, d2)
    out = []
    while dt <= end_dt:
        s = dt.strftime("%Y-%m-%d")
        if is_trading_day(s):
            out.append(s)
        dt += timedelta(days=1)
    return out


def next_n_trading_days(date_str, n):
    """从 date_str 之后（不含当日）数 n 个交易日，返回日期列表"""
    out = []
    cur = date_str
    for _ in range(n):
        cur = next_trading_day(cur)
        out.append(cur)
    return out
