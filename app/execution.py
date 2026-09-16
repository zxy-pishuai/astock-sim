# -*- coding: utf-8 -*-
"""撮合真实性引擎（4.2）—— 借鉴 nautilus_trader FillModel / limit-up-sniper 排队模拟
解决"回测好做、实盘才暴露"的头号问题：回测里"按理想价格成交"的幻觉。

能力：
  1. adaptive_slippage(): 按流通市值分档滑点（微盘股大、大盘股小）
  2. queue_fill_prob():   涨停排队成交概率（一字板/封板，按封单资金/换手/封板时点）
  3. check_suspend():     停牌约束（无当日K线/行情 → 不可交易）
  4. execute_price():     综合成交价（滑点 + 冲击成本）
回测引擎与实盘交易中心共用，保证【回测执行约束 == 实盘执行约束】。
"""
import random

from . import config as C


def adaptive_slippage(float_mktcap):
    """按流通市值分档滑点。float_mktcap: 流通市值（元），None 时用默认 C.SLIPPAGE。
    返回滑点比例（如 0.001 = 0.1%）。
    """
    if not float_mktcap:
        return C.SLIPPAGE
    mcap_yi = float_mktcap / 1e8  # 亿
    for floor, slip in C.EXEC_SLIP_BY_MCAP:
        if mcap_yi >= floor:
            return slip
    return C.EXEC_SLIP_BY_MCAP[-1][1]


def queue_fill_prob(code, price, limit_up_price, quote=None, hour=None,
                    klines=None):
    """涨停排队成交概率（一字板/封板场景）：
    当买单接近涨停价时，成交与否取决于封单强度/换手/封板时点。
    返回 (prob, note)。
    """
    if not C.EXEC_QUEUE_FILL or limit_up_price <= 0:
        return 1.0, "未触板"
    # 距涨停价比例：0=贴板，1=远离
    dist = (limit_up_price - price) / limit_up_price if limit_up_price > 0 else 1.0
    if dist >= 0.005:
        return 1.0, "未触板"  # 离涨停还有距离，正常成交
    # 触板/封板：按封单强度定价
    q = quote or {}
    fund = q.get("fund", 0) or 0        # 封单资金（东财涨停池字段）
    mcap = q.get("float_mktcap", 0) or 0
    turnover = q.get("turnover", 0) or 0
    prob = 0.5
    note = "封板排队"
    if fund > 0:
        # 封单资金越大越难成交（排队靠后）
        prob -= min(0.45, fund / 1e8 * 0.1)
        if fund < C.EXEC_QUEUE_MIN_FUND:
            prob -= 0.2
            note = "封单弱排队"
        else:
            note = f"封单{fund/1e7:.0f}千万排队"
    if turnover > 25:
        prob -= 0.1  # 高换手炸板风险大
    if hour is not None:
        if hour < 10.5:
            prob -= 0.1  # 早盘封板难排
        elif hour >= 14.0:
            prob += 0.1  # 尾盘封板易排到
    # 历史炸板率修正（若提供K线，粗略用近期振幅）
    prob = max(0.02, min(0.95, prob))
    return prob, note


def check_suspend(code, bar=None, quote=None):
    """停牌约束：无当日K线 或 无有效行情 → 不可交易。
    返回 (ok, msg)。
    """
    if not C.EXEC_SUSPEND_BLOCK:
        return True, ""
    if bar is None and (quote is None or not quote.get("price")):
        return False, "停牌/无行情"
    if bar is not None and bar.get("volume", 0) <= 0:
        return False, "停牌(零成交)"
    return True, ""


def execute_price(base_price, side, float_mktcap=None, amount=0.0,
                  day_amount=0.0, impact=True):
    """综合成交价 = 基准价 × (1 ± 滑点) + 冲击成本。
    side: buy/sell。amount: 委托金额。day_amount: 当日成交额（冲击成本判定）。
    """
    slip = adaptive_slippage(float_mktcap)
    px = base_price * (1 + slip) if side == "buy" else base_price * (1 - slip)
    # 冲击成本：大单（占当日成交额>5%）额外滑点
    if impact and day_amount > 0 and amount / day_amount > 0.05:
        px = px * (1 + C.EXEC_IMPACT_SLIP) if side == "buy" else px * (1 - C.EXEC_IMPACT_SLIP)
    return px
