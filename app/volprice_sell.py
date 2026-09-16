# -*- coding: utf-8 -*-
"""★ 4.6 量价择时卖出（双信号，卖出比例由信号类型决定）
参考量价理论 + 邢不行背离统计 + 本地一年回测实证（tools/backtest_volprice_sell.py）：
  - 放量滞涨：量>2×5日均量 但价格不涨/收阴 → 主力派发，卖出有效 → 清仓(比例 1.0)
  - 缩量新高：价创新高但量<0.8×5日均量 → 惜售锁筹，回测显示后续仍常涨(+6%)，
    不宜全卖 → 阶梯式卖一半(0.5)锁利润，留一半吃趋势
纯规则、基于日K列表计算；实盘与回测共用同一实现（零漂移）。
开关 config.VOLP_SELL_ENABLED（默认关闭；回测达标后再开）。
返回: (signal_key, sell_ratio, reason) — 未触发返回 (None, 0, None)。
"""
from . import config as C


def _sma5(volumes, i):
    if i < 0:
        return 0.0
    s = volumes[max(0, i - 4):i + 1]
    if not s:
        return 0.0
    return sum(s) / len(s)


def volp_sell_signal(klines, idx=None):
    """对某根K线（默认最后一根）判定量价卖出信号。
    返回: (signal_key, sell_ratio, reason) 或 (None, 0, None)。
    klines: 日K列表（dict），时间升序，每条含 open/high/low/close/volume。
    """
    if not C.VOLP_SELL_ENABLED:
        return None, 0, None
    if idx is None:
        idx = len(klines) - 1
    if idx < 5:
        return None, 0, None
    k = klines[idx]
    close = k["close"]
    high = k["high"]
    vol = k["volume"] or 0
    if close <= 0 or high <= 0:
        return None, 0, None
    av5 = _sma5([x["volume"] for x in klines], idx - 1)  # 不含当日的 5 日均量
    prev_close = klines[idx - 1]["close"]
    rise = (close - prev_close) / prev_close if prev_close > 0 else 0
    # ---- ② 放量滞涨：量大但价滞涨/收阴 → 清仓 ----
    if C.VOLP_SELL_SURGE_STALL and av5 > 0 and vol > C.VOLP_SELL_SURGE * av5:
        if rise <= C.VOLP_SELL_SURGE_STALL_PCT_THRESH:
            return "volp_surge_stall", 1.0, (
                f"放量滞涨(量{vol/av5:.2f}×但{rise:+.2%}收阴) 主力派发 清仓")
    # ---- ① 缩量新高：价创新高但量萎缩 → 卖一半锁利润 ----
    if C.VOLP_SELL_SHRINK_NEWHIGH and av5 > 0 and vol < C.VOLP_SELL_SHRINK * av5:
        win = [x["high"] for x in klines[max(0, idx - C.VOLP_SELL_NEWHIGH_D):idx]]
        prev_high = max(win) if win else 0
        if high > prev_high and rise >= C.VOLP_SELL_MIN_RISE:
            return "volp_shrink_newhigh", 0.5, (
                f"缩量新高(价新高但量{vol/av5:.2f}×缩) 卖半仓锁利")
    return None, 0, None
