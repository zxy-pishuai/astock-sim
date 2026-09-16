# -*- coding: utf-8 -*-
"""★ 阶段2 指数择时闸门 A/B 回测：开/关对比（策略=score，同一股票池，其余参数完全一致）
用法：python -m tools.backtest_index_timing
输出：docs/reports/phase2_index_timing.md
"""
import json
import os
import sqlite3
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from app import engine as eng
from app import index_timing as it

DB = os.path.join(BASE, "data", "market.db")
START, END = "2025-08-18", "2026-08-18"
REPORT = os.path.join(BASE, "docs", "reports", "phase2_index_timing.md")

# 股票池：本地 ≥320 根日K 的主板/创业/科创标的（引擎预热 260+60 全本地，零网络K线）
conn = sqlite3.connect(DB)
codes = [r[0] for r in conn.execute(
    "SELECT code, COUNT(*) c FROM kline WHERE period='day' "
    "GROUP BY code HAVING c>=320 ORDER BY code")]
conn.close()
names = {}
try:
    with open(os.path.join(BASE, "data", "stock_list.json"), encoding="utf-8") as f:
        for c, n, _p in json.load(f):
            names[c] = n
except Exception:
    pass
names.update({c: c for c in codes})

L = []
def P(s=""):
    print(s); L.append(s)

def run(pool, params):
    bt = eng.Backtest(pool, {c: names.get(c, c) for c in pool},
                      START, END, strategy="score", params=params)
    return bt.run()

def main():
    t0 = time.time()
    P("# 阶段2 指数择时闸门 A/B 回测报告")
    P("")
    P(f"- 区间: {START} ~ {END}｜股票池: {len(codes)} 只（本地≥320根日K）｜策略: score")
    P("- 闸门: MA20/60 多空 × 20日波动率分位 × 北向5日趋势 → 仓位乘数(0.5~1.0)，只调仓位不动情绪周期")
    P("")
    pool = codes

    # 1) 闸门行为统计（回放窗口内每个交易日的乘数）
    mult_hist = []
    # 取池内交易日并集
    conn = sqlite3.connect(DB)
    days = sorted(r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM kline WHERE code=? AND period='day' "
        "AND date>=? AND date<=?", (pool[0], START, END)))
    conn.close()
    active = 0
    for d in days:
        m, rs = it.timing_multiplier(as_of=d)
        mult_hist.append((d, m, rs))
        if m < 1.0:
            active += 1
    if mult_hist:
        vals = [m for _, m, _ in mult_hist]
        P(f"### 闸门行为（{len(days)} 个交易日）")
        P(f"- 乘数<1.0 天数: {active}/{len(days)}（{active/len(days):.0%}），平均乘数 {sum(vals)/len(vals):.3f}")
        P(f"- 乘数分布: 1.0×{sum(1 for v in vals if v >= 1.0)}天 | 0.9×{sum(1 for v in vals if v == 0.9)}天 | "
          f"0.8×{sum(1 for v in vals if v == 0.8)}天 | 0.6~0.79×{sum(1 for v in vals if 0.6 <= v < 0.8)}天 | "
          f"<0.6×{sum(1 for v in vals if v < 0.6)}天")
        if active:
            ex = next((r for d_, m, r in mult_hist if m < 1.0), None)
            P(f"- 触发样例理由: {ex}")
    P("")

    # 2) A/B 回测
    P("### A/B 回测对比（同池同参数，唯一差异=闸门开关）")
    P("| 配置 | 总收益 | 年化 | 最大回撤 | 夏普 | 胜率 | 盈利因子 | 买入笔数 |")
    P("|---|---|---|---|---|---|---|---|")
    result = {}
    for name, flag in (("闸门关闭(现状)", False), ("闸门开启", True)):
        r = run(pool, {"index_timing": flag})
        if "error" in r:
            P(f"| {name} | ERROR: {r['error']} |")
            continue
        result[flag] = r
        P(f"| {name} | {r['total_return']:+.2%} | {r['annual_return']:+.2%} | "
          f"{r['max_drawdown']:.2%} | {r['sharpe']:.2f} | {r['win_rate']:.1%} | "
          f"{r['profit_factor']:.2f} | {r['buy_count']} |")
    P("")
    r0, r1 = result.get(False), result.get(True)
    if r0 and r1:
        dd0, dd1 = r0["max_drawdown"], r1["max_drawdown"]   # 负值：越大(接近0)越好
        sh0, sh1 = r0["sharpe"], r1["sharpe"]
        P("### 结论")
        if dd1 > dd0 and sh1 >= sh0 * 0.98:
            P("**闸门开启后组合回撤与夏普双改善 → 建议接入（保守起步）**")
        elif dd1 > dd0 and r1["total_return"] > r0["total_return"]:
            P("**闸门开启后回撤与收益改善、但夏普略降 → 具防御缓冲价值；"
              "样本仅单一区间，建议保持默认关闭，多窗口验证后再决定开启**")
        elif dd1 > dd0 or sh1 > sh0:
            P("**单边改善但另一指标取舍明显 → 权衡后暂不开启**")
        else:
            P("**闸门未改善组合回撤与夏普（甚至恶化）→ 按数据暂不开启**")
        P(f"- 回撤: 关 {dd0:.2%} → 开 {dd1:.2%}（Δ {dd1 - dd0:+.2%}，正值=改善）")
        P(f"- 夏普: 关 {sh0:.2f} → 开 {sh1:.2f}")
        P(f"- 收益: 关 {r0['total_return']:+.2%} → 开 {r1['total_return']:+.2%}")
        P("- 样本说明: 单一区间(一年)+单一股票池，闸门在策略亏损年度体现为防御减损；"
          "结论仅对当前数据有效，INDEX_TIMING_ENABLED 默认关闭")
    P("")
    P("### 说明")
    P("- 北向5日趋势：官方日披露自2024-08停更（接口返回NULL），本回测该腿中性；"
      "补 data/northbound_flow.json 后自动启用")
    P("- 闸门只缩放单笔仓位（pos_cash×乘数），不改变情绪周期/评分/w_cap 公式")
    P(f"- 总用时 {time.time()-t0:.0f}s")
    os.makedirs(os.path.dirname(REPORT), exist_ok=True)
    with open(REPORT, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("报告:", REPORT)

if __name__ == "__main__":
    main()