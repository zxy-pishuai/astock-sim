# -*- coding: utf-8 -*-
"""★ 4.6 量价信号包（阶段1：吸收券商量价研报 + myhhub/stock 思路）
纯规则信号，全部基于日K列表计算（最后一根=信号日，已收盘完整K线）。
设计要点：
  1. 与 scoring.score_stock 共用本模块 —— 回测与线上评分走同一实现，零漂移；
  2. 无第三方依赖、无时间依赖（PIT 安全，回测不会前视）；
  3. 每个信号先经 tools/backtest_volprice.py 本地回测（胜率/期望）验证，
     胜率<52% 或期望为负者弃用（见 docs/reports/phase1_volprice_signals.md）。
"""
from . import indicators as ind

# 信号键 -> 中文名（config.VOLPRICE_WEIGHTS 对应键）
SIGNAL_NAMES = {
    "breakout_platform": "放量突破平台",
    "pullback_ma250": "回踩年线低吸",
    "vol_up_confirm": "放量上涨确认",
    "chip_peak_support": "筹码峰支撑",
}


def _pct(prev, cur):
    return (cur - prev) / prev if prev else 0.0


def detect_breakout_platform(closes, volumes, i):
    """a) 放量突破平台：收盘创近20日新高 且 量>1.5×20日均量 且 涨幅3~7%（不追贴板）"""
    if i < 20:
        return False
    c = closes[i]
    prev20 = closes[i - 20:i]
    if not prev20 or max(prev20) <= 0:
        return False
    if c <= max(prev20):                       # 收盘必须高于前20日收盘（创新高）
        return False
    v20 = sum(volumes[i - 20:i]) / 20.0
    if not v20 or volumes[i] <= 1.5 * v20:     # 量 > 1.5×20日均量
        return False
    p = _pct(closes[i - 1], c)
    return 0.03 <= p <= 0.07                   # 涨幅 3~7%（不追贴板）


def detect_pullback_ma250(closes, lows, volumes, i):
    """b) 回踩年线低吸：回踩 MA250 偏离<3% 且 近5日缩量(<0.8×20日均量) 且 未有效跌破
    ★ 注意：需 i>=250（至少 251 根K线）；实盘评分默认只拉 250 根，此信号在实盘路径不会触发。
      若将来重新启用，须先把取K根数提到 260+，否则永远是死信号。"""
    if i < 250:
        return False
    ma250 = ind.sma(closes[i - 249:i + 1], 250)[-1]
    if not ma250 or ma250 <= 0:
        return False
    c = closes[i]
    dev = (c - ma250) / ma250                  # 偏离度（正=线上方）
    if abs(dev) >= 0.03:                       # 偏离 <3%（含轻破3%内）
        return False
    v20 = sum(volumes[i - 20:i]) / 20.0
    v5 = sum(volumes[i - 4:i + 1]) / 5.0
    if not v20 or v5 >= 0.8 * v20:             # 近5日缩量 < 0.8×20日均量
        return False
    if min(lows[i - 4:i + 1]) < ma250 * 0.97:  # 未有效跌破（近5日低点在年线3%上方）
        return False
    return True


def detect_vol_up_confirm(closes, volumes, i):
    """c) 放量上涨确认：涨>3% 且 量>2×5日均量 且 MA20 向上（趋势确认）"""
    if i < 20:
        return False
    c = closes[i]
    p = _pct(closes[i - 1], c)
    if p <= 0.03:
        return False
    v5 = sum(volumes[i - 5:i]) / 5.0           # 前5日均量（不含当日）
    if not v5 or volumes[i] <= 2.0 * v5:
        return False
    ma20 = ind.sma(closes, 20)
    if not ma20[i] or not ma20[i - 1]:
        return False
    return ma20[i] > ma20[i - 1]               # MA20 向上（趋势确认）


def detect_chip_peak_support(chip, lows, closes, i):
    """d) 筹码峰支撑：价格站上主筹码峰(cost50) 且 回踩峰位不破（近3日低点≥峰位×0.99）"""
    if i < 1:
        return False
    c50 = chip["cost50"][i]
    if not c50 or c50 <= 0:
        return False
    c = closes[i]
    if c <= c50:                               # 站上主筹码峰
        return False
    if min(lows[max(0, i - 2):i + 1]) < c50 * 0.99:   # 回踩峰位不破（±1%容差）
        return False
    return True


def detect_signals(klines, i=None):
    """对 klines 的第 i 根（默认最后一根）检测全部量价信号。
    返回 [(key, desc)]：命中信号的 (键, 描述)；未命中返回 []。
    klines: 日K列表 [{date,open,high,low,close,volume,amount}]，升序。
    """
    n = len(klines)
    if n < 30:
        return []
    i = n - 1 if i is None else i
    closes = [k["close"] for k in klines]
    volumes = [k.get("volume", 0) or 0 for k in klines]
    lows = [k["low"] for k in klines]
    out = []
    if detect_breakout_platform(closes, volumes, i):
        out.append(("breakout_platform", "放量突破平台"))
    if detect_pullback_ma250(closes, lows, volumes, i):
        out.append(("pullback_ma250", "回踩年线低吸"))
    if detect_vol_up_confirm(closes, volumes, i):
        out.append(("vol_up_confirm", "放量上涨确认"))
    chip = ind.chip_profile(klines)
    if detect_chip_peak_support(chip, lows, closes, i):
        out.append(("chip_peak_support", "筹码峰支撑"))
    return out