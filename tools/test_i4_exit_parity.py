# -*- coding: utf-8 -*-
"""I4（2026-09-13）：卖出定价对称性自动验证。

对同一组 (base_px, qty, code, 市值档位, day_amount, 涨跌停) 输入，断言：
  A. exit_policy.exit_price（统一函数，回测 ctx）== engine._sell_price（回测路径）
  B. exit_policy.exit_price（统一函数，实盘新 ctx）== trader._buy_px(side="sell", 新口径)
  C. 实盘旧口径 vs 回测 vs 统一函数：差异量化（pp + 收益影响量级）

参考实现直接调用生产代码（轻量构造实例，不跑引擎）：
  - 回测路径：engine.Backtest.__new__ + 设置 slippage/mcap_of/names/_hist_klines stub
  - 实盘路径：trader.TradingEngine.__new__（_buy_px 无 self 状态依赖）

覆盖：市值档位（<5亿/5-20亿/20-50亿/50-200亿/>200亿/None）、qty（100/1000/50000/50）、
day_amount（0 / 不触发 impact / 触发 impact）、涨跌停（远离/贴近涨停/贴近跌停）、
pre_slip（回测 C.SLIPPAGE / 实盘 0）。
"""
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from app import config as C
from app import engine as eng
from app import exit_policy as ep
from app import trader as tr

MCAP_TIERS = [  # (标签, 流通市值元, 期望分档滑点)
    ("微盘<5亿", 3e8, 0.008),
    ("小盘5-20亿", 1e9, 0.004),
    ("中盘20-50亿", 3e9, 0.002),
    ("大盘50-200亿", 1e10, 0.001),
    ("超大盘>200亿", 5e10, 0.0005),
    ("无市值(None)", 0, C.SLIPPAGE),
]
QTYS = [100, 1000, 50000, 50]
DAY_AMOUNTS = [0.0, 1e8, 1e5]  # 0=不触发impact；1e8=不触发(量占比小)；1e5=触发(占比>5%)
BASE_PX = 10.0
PREV_CLOSE = 10.0
CODE = "sh600000"  # 主板 10cm


def fake_hist_klines(code, d):
    return [{"close": PREV_CLOSE}, {"close": BASE_PX}]


def make_backtest(mcap):
    bt = object.__new__(eng.Backtest)
    bt.slippage = C.SLIPPAGE
    bt.mcap_of = {CODE: mcap}
    bt.names = {CODE: "测试票"}
    bt._hist_klines = fake_hist_klines
    return bt


def make_trader():
    return object.__new__(tr.TradingEngine)


def ref_backtest(code, base_px, qty, bar):
    """回测参考：engine._sell_price（生产代码直调）。"""
    return make_backtest(bar["mcap"])._sell_price(code, base_px, qty, bar)


def ref_live(code, base_px, qty, q, use_new):
    """实盘参考：trader._buy_px(side='sell')（生产代码直调；use_new 切新口径）。"""
    old = getattr(C, "BUY_PX_USE_NEW", False)
    try:
        C.BUY_PX_USE_NEW = use_new
        return make_trader()._buy_px(q, base_px, side="sell", qty=qty,
                                     day_amount=q.get("amount"))
    finally:
        C.BUY_PX_USE_NEW = old


def unif(base_px, qty, ctx):
    return ep.exit_price(base_px, qty, CODE, "sell", ctx)


def main():
    lu, ld = eng.limit_prices(CODE, PREV_CLOSE, "测试票")
    print("涨跌停: lu=%.2f ld=%.2f（prev_close=%.2f）" % (lu, ld, PREV_CLOSE))
    print("=" * 96)
    print("%-14s %8s %10s %12s | %-12s %-12s %-12s | 统一vs回测 统一vs实盘新 实盘旧vs回测" %
          ("市值档", "qty", "day_amount", "base_px", "回测参考", "实盘新参考", "统一函数"))
    print("-" * 96)

    n_case = 0
    max_dev_ab = 0.0   # 统一 vs 回测（A）
    max_dev_bc = 0.0   # 统一 vs 实盘新（B）
    diffs_old_vs_bt = []  # 实盘旧 vs 回测（C）pp
    diffs_old_vs_unif = []  # 实盘旧 vs 统一 pp

    for tag, mcap, _slip in MCAP_TIERS:
        for qty in QTYS:
            for da in DAY_AMOUNTS:
                for base_px in (BASE_PX, round(lu * 0.995, 2), round(ld * 1.005, 2)):
                    bar = {"date": "2026-09-01", "mcap": mcap, "amount": da}
                    q = {"code": CODE, "float_mktcap": mcap, "amount": da,
                         "limit_up": lu, "limit_down": ld, "name": "测试票"}
                    bt_ref = ref_backtest(CODE, base_px, qty, bar)
                    live_old = ref_live(CODE, base_px, qty, q, use_new=False)
                    live_new = ref_live(CODE, base_px, qty, q, use_new=True)
                    # 统一 A：回测 ctx（pre_slip=C.SLIPPAGE，impact=True，历史涨跌停）
                    ctx_bt = {"mcap": mcap, "day_amount": da, "pre_slip": C.SLIPPAGE,
                              "impact": True, "limit_up": lu, "limit_down": ld}
                    u_bt = unif(base_px, qty, ctx_bt)
                    # 统一 B：实盘新 ctx（pre_slip=0，impact=True，quote 涨跌停）
                    ctx_new = {"mcap": mcap, "day_amount": da, "pre_slip": 0.0,
                               "impact": True, "limit_up": lu, "limit_down": ld}
                    u_new = unif(base_px, qty, ctx_new)

                    dev_ab = abs(u_bt - bt_ref)
                    dev_bc = abs(u_new - live_new)
                    max_dev_ab = max(max_dev_ab, dev_ab)
                    max_dev_bc = max(max_dev_bc, dev_bc)
                    if dev_ab > 1e-9 or dev_bc > 1e-9:
                        print("  ✗ %-10s qty=%d da=%.0f base=%.2f | bt=%.6f u_bt=%.6f "
                              "dev=%.2e | live_new=%.6f u_new=%.6f dev=%.2e"
                              % (tag, qty, da, base_px, bt_ref, u_bt, dev_ab,
                                 live_new, u_new, dev_bc))
                    # C：实盘旧 vs 回测 vs 统一（pp）
                    diff_old_bt = (live_old - bt_ref) / base_px * 100
                    diff_old_u = (live_old - u_bt) / base_px * 100
                    diffs_old_vs_bt.append(diff_old_bt)
                    diffs_old_vs_unif.append(diff_old_u)
                    n_case += 1

    print("=" * 96)
    print("用例数: %d（6 市值档 × 4 qty × 3 day_amount × 3 base_px）" % n_case)
    print("A) 统一(回测ctx) vs 回测路径 : 最大偏差 %.2e  → %s"
          % (max_dev_ab, "逐位相同 ✓" if max_dev_ab <= 1e-9 else "存在偏差 ✗"))
    print("B) 统一(实盘新ctx) vs 实盘新路径: 最大偏差 %.2e  → %s"
          % (max_dev_bc, "逐位相同 ✓" if max_dev_bc <= 1e-9 else "存在偏差 ✗"))
    print("-" * 96)
    # C 汇总：实盘旧 vs 回测/统一 的差异分布
    d1 = [abs(x) for x in diffs_old_vs_bt]
    d2 = [abs(x) for x in diffs_old_vs_unif]
    print("C) 实盘旧口径 vs 回测路径（现状不对称，pp）："
          "p50=%.4f p95=%.4f max=%.4f" %
          (sorted(d1)[len(d1) // 2], sorted(d1)[int(len(d1) * 0.95)], max(d1)))
    print("   实盘旧口径 vs 统一函数（pp）：p50=%.4f p95=%.4f max=%.4f" %
          (sorted(d2)[len(d2) // 2], sorted(d2)[int(len(d2) * 0.95)], max(d2)))
    print("   说明：实盘旧口径=固定1000股假设+impact=False（trader.py:893-896）；"
          "回测=固定滑点+分档滑点+impact=True（engine.py:932-938）")
    # 收益影响量级估算（单笔卖出按 1e6 元计）
    print("   收益影响估算：每笔卖出 100 万元计，p50 差 %.2fpp = %.0f 元/笔"
          % (sorted(d1)[len(d1) // 2], sorted(d1)[len(d1) // 2] / 100 * 1e6))

    ok = max_dev_ab <= 1e-9 and max_dev_bc <= 1e-9
    print("=" * 96)
    print("结论: %s" % ("对称性验证通过（统一函数与两侧新口径逐位一致）"
                        if ok else "存在偏差，见上方 ✗ 行"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
