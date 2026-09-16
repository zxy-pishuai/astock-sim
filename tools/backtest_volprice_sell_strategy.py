# -*- coding: utf-8 -*-
"""★ 4.6 量价择时卖出 策略级 A/B：阶梯止盈常开(True) 前提下，量价卖出 开/关
本池：本地一年日K（≥320根 82 只），score 策略，与阶段2 A/B 同口径。
输出：总收益/回撤/胜率/买入/卖出/阶梯/量价卖出笔数
"""
import sys, time, sqlite3
sys.path.insert(0, r'C:\Users\26838\A股模拟盘')
sys.stdout.reconfigure(encoding='utf-8')

from app import config as C
from app import engine as eng

def run(volp_enable):
    old_l = C.LADDER_TP_ENABLED
    old_v = C.VOLP_SELL_ENABLED
    C.LADDER_TP_ENABLED = True       # 阶梯常开（用户要求）
    C.VOLP_SELL_ENABLED = volp_enable
    try:
        e = eng.Backtest(
            codes=CODES, names={},
            start="2025-08-18", end="2026-08-18",
            initial_capital=100000, strategy="score",
            params={"buy_threshold": 30, "max_positions": 3, "position_pct": 0.3,
                    "slippage": 0.001, "board_enabled": False})
        res = e.run()
    finally:
        C.LADDER_TP_ENABLED = old_l
        C.VOLP_SELL_ENABLED = old_v
    return e, res

conn = sqlite3.connect(r'C:\Users\26838\A股模拟盘\data\market.db')
rows = conn.execute(
    "SELECT code, COUNT(*) c FROM kline WHERE period='day' "
    "GROUP BY code HAVING c>=320 ORDER BY c DESC LIMIT 82").fetchall()
conn.close()
CODES = [r[0] for r in rows]
print('股票池:', len(CODES), '只')
print('阶梯止盈: 常开(True) | 量价卖出: A/B\n')

for lbl, v in [('阶梯+无增量量价', False), ('阶梯+量价卖出', True)]:
    t0 = time.time()
    e, res = run(v)
    sells = [t for t in e.trades if t.get('side') == 'sell']
    ladder_sells = [t for t in sells if '阶梯止盈' in (t.get('reason') or '')]
    volp_sells = [t for t in sells if '缩量新高' in (t.get('reason') or '') or '放量滞涨' in (t.get('reason') or '')]
    buys = [t for t in e.trades if t.get('side') == 'buy']
    eq = e.equity
    n_days = max(1, len(eq))
    ann = (1 + res.get('total_return', 0)) ** (252 / n_days) - 1 if abs(res.get('total_return', 0)) < 1 else -1
    print(f'===== {lbl} =====')
    print(f'  总收益: {res.get("total_return",0)*100:.2f}%  回撤: {res.get("max_drawdown",0)*100:.2f}%  胜率: {res.get("win_rate",0)*100:.1f}%')
    print(f'  买入{len(buys)} | 卖出{len(sells)} | 阶梯{len(ladder_sells)} | 量价卖出{len(volp_sells)}')
    print(f'  用时 {time.time()-t0:.1f}s\n')