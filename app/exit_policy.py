# -*- coding: utf-8 -*-
"""I4（2026-09-13）：统一卖出定价共用函数。

背景：实盘 trader._sell_px 与回测 engine._sell_price 各自实现"卖出价"，代码
各写一遍、仅注释声称对称（P45/P59 审计多次暴露两边口径漂移）。本模块把卖出
定价收敛为单一函数 exit_price()，供实盘与回测共同调用，消除
"注释声称对称但代码各写一遍"的漂移风险。

口径（以 execution.execute_price 为单一滑点/冲击成本来源）：
  1) 可选固定滑点步 pre_slip：回测 legacy 的 `base*(1-slippage)`
     （engine.py:932）；实盘无此步 → 传 0 即跳过。迁移后建议回测去掉该步
     （双重滑点，见 docs/reports/I4_20260913.md §口径差异量化）。
  2) execute_price(px, side, mcap, amount=px*qty, day_amount, impact)
     —— 与实盘 P59 新口径（trader.py:905-907 语义）对齐；amount 用真实委托量
     （下限 100 股，对齐回测 engine.py:937 的 max(qty,100) 保护）。
  3) 涨跌停钳制：ctx 传 limit_up/limit_down（>0 才钳制；实盘取 quote 字段、
     回测取 limit_prices() 历史计算），钳制在滑点之后——与两侧现有实现一致。

调用点迁移：trader.py/engine.py 的调用点改动需与 F/H 道协调，排在它们之后
（见 docs/reports/I4_20260913.md §迁移清单）。本模块先行落地 + parity 测试
（tools/test_i4_exit_parity.py）证明与两侧现有实现的一致性/差异量。
"""
from __future__ import annotations

from . import config as C

# ---------------------------------------------------------------------------
# I4 任务 2：卖出侧结构化审计事件 schema（kind=position, event=strategy_sell）
# 权威实现：app/trader.py:1015 _sell（C5 交付）；本常量供 D4/周报等消费方引用。
# 字段与 strategy_buy 对称 + 卖出专有：hold_days/reason/exit_kind/slippage_bp。
# ---------------------------------------------------------------------------
SELL_EVENT = "strategy_sell"
SELL_EVENT_SCHEMA = {
    "kind": "position", "level": "SELL",
    "fields": ["code", "name", "price", "qty", "fee", "pnl",
               "hold_days", "reason", "exit_kind", "strategy",
               "trigger_price", "fill_price", "slippage_bp",
               "trigger_ts", "fill_ts", "delay_min"],
}

# reason_code 枚举（与 trader._EXIT_KIND_RULES 主枚举一致；trader.py 为权威实现）。
# I4 任务 2 要求与 trader 全部卖出分支一一对应——grep 清单见报告 §3：
#   两点半战法:次日收盘卖 / 止损 / 止盈 / 冲高回落止盈(峰) / 移动止损(峰) /
#   时间止损(N天亏) / 超时退出(N天)→时间止损 / 阶梯止盈 / 打板低开止损→止损 /
#   打板半仓止盈 / 波动战法文本(_mvr,未命中→原文截断) / 手动卖出(manual_sell)。
# 任务书示例的"信号反转/涨停打开/风控强制"经 grep 证实 trader 无对应卖出分支
# （炸板仅提醒、风控仅买入侧拦截）→ 不虚构加入，见报告 §3。
EXIT_KINDS = ("移动止损", "冲高回落止盈", "阶梯止盈", "时间止损",
              "止损", "两点半战法", "打板半仓止盈", "手动")


def exit_price(base_px, qty, code, side="sell", ctx=None):
    """统一卖出定价（与统一买入定价共用执行层）。

    Args:
        base_px: 基准价（卖出=触发价/收盘价，未扣滑点）
        qty: 委托数量（股）
        code: 股票代码（用于日志/审计追溯）
        side: 恒为 "sell"（保留参数位，未来与买入共用时对称）
        ctx: dict，可选字段：
            - mcap: float 流通市值（元）；0/None → 退回 C.SLIPPAGE 默认滑点
            - day_amount: float 当日成交额（冲击成本判定；0=不触发）
            - impact: bool 是否开冲击成本（默认 True，与回测/P59 新口径一致）
            - pre_slip: float 固定滑点步（回测 legacy 传 C.SLIPPAGE；实盘传 0）
            - limit_up / limit_down: float 涨跌停价（>0 才钳制）
            - name: str 股票名（审计/日志）

    Returns:
        float 成交价（已含滑点与钳制）
    """
    ctx = ctx or {}
    qty = int(qty or 0)
    if qty <= 0:
        qty = 100  # 防御：数量非法时按一手计（仅影响冲击成本判定量级）
    pre_slip = float(ctx.get("pre_slip") or 0.0)
    impact = bool(ctx.get("impact", True))
    mcap = ctx.get("mcap") or 0
    day_amount = ctx.get("day_amount") or 0

    # 1) 可选固定滑点步（回测 legacy；实盘 0 跳过）
    px = base_px * (1 - pre_slip) if pre_slip else base_px

    # 2) 市值分档滑点 + 冲击成本（唯一执行层，实盘/回测共用）
    try:
        from . import execution as ex
        px = ex.execute_price(px, side, mcap,
                              amount=px * max(qty, 100),
                              day_amount=day_amount, impact=impact)
    except Exception:
        px = px * (1 - C.SLIPPAGE)  # 异常回退：固定滑点（与两侧现有回退语义一致）

    # 3) 涨跌停钳制（ctx 提供；>0 才钳制）
    lu = ctx.get("limit_up") or 0
    ld = ctx.get("limit_down") or 0
    if lu > 0 and ld > 0:
        px = max(ld, min(lu, px))
    return px
