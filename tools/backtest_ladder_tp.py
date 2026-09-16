# -*- coding: utf-8 -*-
"""★ 4.6 阶梯止盈回测对比：LADDER_TP_ENABLED 开/关 vs 现状全清
本池：本地一年日K数据（≥320 根），score 策略，与阶段2 A/B 同口径。
输出：总收益/年化/最大回撤/夏普/胜率/买入笔数/卖出笔数/阶梯卖出笔数
"""
import sys, time
sys.path.insert(0, r'C:\Users\26838\A股模拟盘')
sys.stdout.reconfigure(encoding='utf-8')

import sqlite3
from app import config as C
from app import engine as eng
from app import datafeed as df

def run(enable_ladder):
    old = C.LADDER_TP_ENABLED
    C.LADDER_TP_ENABLED = enable_ladder
    try:
        e = eng.Backtest(
            codes=CODES,
            names={},
            start="2025-08-18", end="2026-08-18",
            initial_capital=100000,
            strategy="score",
            params={"buy_threshold": 30, "max_positions": 3, "position_pct": 0.3,
                    "slippage": 0.001, "board_enabled": False},
        )
        res = e.run()
    finally:
        C.LADDER_TP_ENABLED = old
    return e, res

conn = sqlite3.connect(r'C:\Users\26838\A股模拟盘\data\market.db')
rows = conn.execute(
    "SELECT code, COUNT(*) c FROM kline WHERE period='day' "
    "GROUP BY code HAVING c>=320 ORDER BY c DESC LIMIT 82").fetchall()
conn.close()
CODES = [r[0] for r in rows]
print('股票池:', len(CODES), '只')

for lbl, flag in [('现状(全清止盈)', False), ('阶梯止盈(开)', True)]:
    t0 = time.time()
    e, res = run(flag)
    trades = e.trades
    sells = [t for t in trades if t.get('side') == 'sell']
    ladder_sells = [t for t in sells if '阶梯止盈' in (t.get('reason') or '')]
    eq = e.equity
    n_days = max(1, len(eq))
    ann = (1 + res.get('total_return', 0)) ** (252 / n_days) - 1 if res.get('total_return', 0) > -1 else -1
    print(f'\n===== {lbl} =====')
    print(f'  总收益: {res.get("total_return", 0)*100:.2f}%  年化: {ann*100:.2f}%')
    print(f'  最大回撤: {res.get("max_drawdown", 0)*100:.2f}%  胜率: {res.get("win_rate", 0)*100:.1f}%')
    print(f'  买入 {len([t for t in trades if t.get("side")=="buy"])} 笔 | 卖出 {len(sells)} 笔 | 阶梯 {len(ladder_sells)} 笔')
    print(f'  用时 {time.time()-t0:.1f}s')