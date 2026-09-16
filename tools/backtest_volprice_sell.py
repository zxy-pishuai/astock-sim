# -*- coding: utf-8 -*-
"""★ 4.6 量价择时卖出 双信号回测（纯本地 market.db，不走网络）
信号：①缩量新高 ②放量滞涨（intraday 判定放宽见函数）
对卖出信号：有效 = 触发后股价不涨/下跌（均收益低、继续涨占比低）。
入选：3/5日均收益 ≤ 全市场基线(0.30%/0.58%) 且 胜率<50%。
"""
import sqlite3, sys
sys.path.insert(0, r'C:\Users\26838\A股模拟盘')
sys.stdout.reconfigure(encoding='utf-8')

from app import config as C
from app import volprice_sell as vps

# ★ 回测需信号生效：临时强制开启量价卖出开关（不影响主系统 config）
_old_enabled = C.VOLP_SELL_ENABLED
_old_shrink = C.VOLP_SELL_SHRINK_NEWHIGH
_old_surge = C.VOLP_SELL_SURGE_STALL
C.VOLP_SELL_ENABLED = True
C.VOLP_SELL_SHRINK_NEWHIGH = True
C.VOLP_SELL_SURGE_STALL = True

# 直接在 market.db 按 (code,date) 读日K，规避 fetch_kline 走网络
DB = r'C:\Users\26838\A股模拟盘\data\market.db'
conn = sqlite3.connect(DB)
cur = conn.cursor()

# 候选：一年窗口(2025-08-19起)内有≥240根日K的股票
codes = [r[0] for r in cur.execute(
    "SELECT code FROM kline WHERE period='day' AND date>='2025-08-19' "
    "GROUP BY code HAVING COUNT(*)>=240").fetchall()]
print('候选池（一年≥240根）:', len(codes), '只')

# 全服基线：窗口内全体 3/5 日均收益
base3 = base5 = 0.0
n3 = n5 = 0
base_w3 = base_w5 = 0
for code in codes:
    rows = cur.execute(
        "SELECT date,open,high,low,close,volume FROM kline WHERE code=? AND period='day' "
        "AND date>='2025-08-19' AND date<='2026-08-18' ORDER BY date",
        (code,)).fetchall()
    for j in range(len(rows) - 5):
        c = rows[j][4]
        if c <= 0:
            continue
        base3 += rows[j + 3][4] / c - 1
        base5 += rows[j + 5][4] / c - 1
        n3 += 1; n5 += 1
        base_w3 += 1 if rows[j + 3][4] > c else 0
        base_w5 += 1 if rows[j + 5][4] > c else 0
print(f'市场基线: 3日均 {base3/max(n3,1)*100:+.2f}%(胜率{base_w3/max(n3,1)*100:.1f}%)  '
      f'5日均 {base5/max(n5,1)*100:+.2f}%(胜率{base_w5/max(n5,1)*100:.1f}%)')

# 逐股扫描信号
sig = {}   # key -> [n, s3, w3, s5, w5]
def rec(key, f3, f5):
    e = sig.setdefault(key, [0, 0.0, 0, 0.0, 0])
    e[0] += 1
    if f3 is not None:
        e[1] += f3; e[2] += 1 if f3 > 0 else 0
    if f5 is not None:
        e[3] += f5; e[4] += 1 if f5 > 0 else 0

for code in codes:
    rows = cur.execute(
        "SELECT date,open,high,low,close,volume FROM kline WHERE code=? AND period='day' "
        "AND date>='2025-08-19' AND date<='2026-08-18' ORDER BY date",
        (code,)).fetchall()
    # 转 dict 供 volp_sell_signal 使用（需要 20 日预热，从533根往前补）
    hist = [{"open": r[1], "high": r[2], "low": r[3], "close": r[4], "volume": r[5],
             "date": r[0]} for r in rows]
    for i in range(6, len(hist) - 5):
        key, ratio, _reason = vps.volp_sell_signal(hist, idx=i)
        if not key:
            continue
        c = hist[i]["close"]
        f3 = hist[i + 3]["close"] / c - 1 if c > 0 else None
        f5 = hist[i + 5]["close"] / c - 1 if c > 0 else None
        rec(key, f3, f5)
conn.close()

print('\n===== 量价卖出双信号 回测（2025-08-19~2026-08-18）=====')
print('对卖出信号：3/5日均收益越低、且胜率(继续涨)越低 → 卖出越有效')
print(f'参考市场基线: 3日均 {base3/max(n3,1)*100:+.2f}%(胜率{base_w3/max(n3,1)*100:.1f}%) '
      f'| 5日均 {base5/max(n5,1)*100:+.2f}%(胜率{base_w5/max(n5,1)*100:.1f}%)\n')
b3 = base3 / max(n3, 1)
b5 = base5 / max(n5, 1)
wr3b = base_w3 / max(n3, 1)
wr5b = base_w5 / max(n5, 1)
for key, (n, s3, w3, s5, w5) in sorted(sig.items()):
    m3 = s3 / n if n else 0; m5 = s5 / n if n else 0
    wr3 = w3 / n if n else 0; wr5 = w5 / n if n else 0
    print(f'[{key}] n={n}')
    print(f'   3日: 均收益{m3*100:+.2f}% (基线{b3*100:+.2f}%) 胜率{wr3*100:.1f}% (基线{wr3b*100:.1f}%)')
    print(f'   5日: 均收益{m5*100:+.2f}% (基线{b5*100:+.2f}%) 胜率{wr5*100:.1f}% (基线{wr5b*100:.1f}%)')
    ok3 = (m3 < b3) and (wr3 < wr3b)
    ok5 = (m5 < b5) and (wr5 < wr5b)
    verdict = "卖出有效" if (ok3 and ok5) else ("部分有效" if (ok3 or ok5) else "卖出无效(反而涨)")
    print(f'   判定: 3日{"✅" if ok3 else "❌"} 5日{"✅" if ok5 else "❌"} → {verdict}\n')

# 恢复 config
C.VOLP_SELL_ENABLED = _old_enabled
C.VOLP_SELL_SHRINK_NEWHIGH = _old_shrink
C.VOLP_SELL_SURGE_STALL = _old_surge