# -*- coding: utf-8 -*-
"""「潜力挖掘」策略复现回测（本地等效管线）
依据：用户下发交接说明书 §3/§7 + tmp/qlw/pre_registered_judgment.md（2026-09-10 预注册）
数据：tmp/qlw/kline_day.pkl（生产库只读导出）+ tmp/qlw/universe_meta.json（新浪名单）
输出：data/bt_qlw.json（全量结果）
"""
import json
import sys
import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding='utf-8')

ROOT = r'C:/Users/26838/A股模拟盘'
T0 = '2025-09-15'          # 信号日区间起点
T1 = '2026-09-09'          # 信号日区间终点（说明书 239 交易日）
COST = 0.0029              # 双边合计成本 0.29%
BUY_COST_F = 1.0           # 净收益 = 毛收益 - COST（减法口径）

# ---------- 载入 ----------
# 数据源：腾讯 fqkline 全量重拉（说明书 §4 指定源；本地库 2026-08-26 前仅 43% 覆盖不足复现）
# 字段 code,date,open,close,high,low,volume（volume 单位=手，已实测 600519 对账成立）
df = pd.read_pickle(ROOT + '/tmp/qlw/kline_tencent.pkl')
uni = json.load(open(ROOT + '/tmp/qlw/universe_meta.json', encoding='utf-8'))

# 统一 code 为 6 位数字字符串
df['code'] = df['code'].astype(str)
uni_df = pd.DataFrame(uni)
uni_df['code'] = uni_df['code'].astype(str)

# ---------- 宇宙：剔除北交所 / ST（当前名单口径） ----------
def board_of(c):
    if c.startswith(('60', '00')):
        return 'main'
    if c.startswith(('30', '68')):
        return 'gem'
    return 'other'

uni_df['board'] = uni_df['code'].map(board_of)
uni_df['is_st'] = uni_df['name'].str.upper().str.contains('ST', na=False)
uni_df = uni_df[uni_df['board'] != 'other']          # 剔北交所/其他
uni_df = uni_df[~uni_df['is_st']]                    # 剔 ST
uni_map = uni_df.set_index('code')[['board', 'nmc', 'trade']].to_dict('index')

# kline 内所有票（含新浪名单外）
df = df[df['code'].isin(uni_map)]                    # 只保留宇宙内（可算换手/识 ST）

# ---------- 特征构建（按票向量化） ----------
df = df.sort_values(['code', 'date']).reset_index(drop=True)
g = df.groupby('code', sort=False)

df['prev_close'] = g['close'].shift(1)
df['chg'] = df['close'] / df['prev_close'] - 1.0
df['board'] = df['code'].map(lambda c: uni_map[c]['board'])
band = np.where(df['board'] == 'gem', 0.194, 0.097)
df['seam'] = df['chg'].abs() > (band + 0.02)         # 除权断层日（|chg|>带宽+2pp）
df['is_zt'] = (df['chg'] >= band) & (~df['seam'])    # 收盘涨停（比值口径，方言安全）

# 涨停基因：含 T 的 10 日窗口内涨停次数 >= 1
df['zt10'] = g['is_zt'].transform(lambda s: s.rolling(10, min_periods=1).sum())

# 量比 V[T]/mean(V[T-5..T-1])
df['vol_ma5'] = g['volume'].transform(lambda s: s.rolling(5).mean().shift(1))
df['vr'] = df['volume'] / df['vol_ma5']

# 换手率 = volume(股) / 流通股本(股)；流通股本 = 流通市值(万元)*1e4 / 现价(trade)（说明书 §4 同款近似）
# volume 单位=手 → 股 = volume*100
df['nmc'] = df['code'].map(lambda c: uni_map[c]['nmc'])
df['trade'] = df['code'].map(lambda c: uni_map[c]['trade'])
df['turn'] = df['volume'] * 100.0 * df['trade'] / (df['nmc'] * 1e4)

# 非一字：O[T] < C[T-1]*1.097
df['not_yizi'] = df['open'] < df['prev_close'] * 1.097

# 价格阈值（方言价近似）
df['ge3'] = df['close'] >= 3.0

# 停牌：相邻 K 间隔 > 15 自然日 → 复牌首日标记
df['ddate'] = pd.to_datetime(df['date'])
df['prev_date'] = g['ddate'].shift(1)
df['gap_days'] = (df['ddate'] - df['prev_date']).dt.days
df['reopen'] = df['gap_days'] > 15                  # 复牌首日

# T+1 / T+2 数据（执行与卖点）
df['n_open'] = g['open'].shift(-1)                  # T+1 开盘（执行买入价）
df['n_close'] = g['close'].shift(-1)                # T+1 收盘
df['c2'] = g['close'].shift(-2)                     # T+2 收盘（E1 卖）
df['c4'] = g['close'].shift(-4)                     # T+4 收盘（E2 卖）
df['g_exec'] = df['n_open'] / df['close'] - 1.0     # 执行日开盘缺口
df['next_row'] = g['date'].shift(-1)
df['n2_low'] = g['low'].shift(-1)                   # T+1 最低
df['n2_high'] = g['high'].shift(-1)                 # T+1 最高
df['n3_low'] = g['low'].shift(-2)
df['n3_high'] = g['high'].shift(-2)
df['n4_low'] = g['low'].shift(-3)
df['n4_high'] = g['high'].shift(-3)
df['n5_low'] = g['low'].shift(-4)
df['n5_high'] = g['high'].shift(-4)
df['n6_close'] = g['close'].shift(-5)               # 买日后第 5 根收盘（E3 兜底）

# 信号窗口
sig = (df['date'] >= T0) & (df['date'] <= T1) & (~df['reopen']) & df['n_open'].notna()

# ---------- 卖点路径 ----------
def e1_path(r):
    """E1: T+2 收盘卖。买入价 = T+1 开盘"""
    if pd.isna(r['c2']):
        return None, None
    buy = r['n_open']
    gross = r['c2'] / buy - 1.0
    return gross - COST, 'E1'

def e2_path(r):
    if pd.isna(r['c4']):
        return None, None
    buy = r['n_open']
    gross = r['c4'] / buy - 1.0
    return gross - COST, 'E2'

def e3_path(r):
    """E3: 止损 -5% / 止盈 +10% / 最长 5 交易日。买入 T+1，T+2 起逐日（T+1 不可卖）。
    同日先触止损计。无触发 → 第 5 根 K 收盘强卖（n6_close）。"""
    buy = r['n_open']
    stop = buy * 0.95
    tgt = buy * 1.10
    lows = [r['n2_low'], r['n3_low'], r['n4_low'], r['n5_low']]
    highs = [r['n2_high'], r['n3_high'], r['n4_high'], r['n5_high']]
    for i in range(4):
        if pd.isna(lows[i]):
            break
        if lows[i] <= stop:
            return (stop / buy - 1.0) - COST, 'E3_stop'
        if highs[i] >= tgt:
            return (tgt / buy - 1.0) - COST, 'E3_tp'
    if pd.isna(r['n6_close']):
        return None, None
    return (r['n6_close'] / buy - 1.0) - COST, 'E3_time'

def ga_path(r, sell='F1'):
    """版本 B：O[T] 买入。F1=T+1 收盘卖；F2=T+2 收盘卖；F3=止损止盈（T+1 起）。"""
    buy = r['open']
    if sell == 'F1':
        if pd.isna(r['n_close']):
            return None, None
        return (r['n_close'] / buy - 1.0) - COST, 'F1'
    if sell == 'F2':
        if pd.isna(r['c2']):
            return None, None
        return (r['c2'] / buy - 1.0) - COST, 'F2'
    stop = buy * 0.95
    tgt = buy * 1.10
    if not pd.isna(r['n2_low']) and r['n2_low'] <= stop:
        return (stop / buy - 1.0) - COST, 'F3_stop'
    if not pd.isna(r['n2_high']) and r['n2_high'] >= tgt:
        return (tgt / buy - 1.0) - COST, 'F3_tp'
    if pd.isna(r['c2']):
        return None, None
    return (r['c2'] / buy - 1.0) - COST, 'F3_time'

# ---------- 汇总 ----------
def summarize(recs, label):
    """recs: list of (net_ret, style)"""
    if not recs:
        return {'label': label, 'n': 0}
    rets = np.array([r[0] for r in recs])
    n = len(rets)
    wins = rets[rets > 0]
    losses = rets[rets <= 0]
    pf = wins.sum() / abs(losses.sum()) if losses.sum() != 0 else float('inf')
    return {
        'label': label,
        'n': int(n),
        'win_rate': float((rets > 0).mean()),
        'avg_net': float(rets.mean()),
        'pf': float(pf),
        'sum_net': float(rets.sum()),
    }

# ---------- 各配置 ----------
res = {}
sig_df = df[sig].copy()

# --- P0：全市场（主+创+科）涨停基因池，无每日上限，E1 ---
p0_mask = sig_df['zt10'] >= 1
p0_sub = sig_df[p0_mask]
recs = []
for _, r in p0_sub.iterrows():
    net, st = e1_path(r)
    if net is not None:
        recs.append((net, st))
res['P0'] = summarize(recs, 'P0 涨停基因池 E1')

# --- P8：主板 + 涨停基因 + 量比≥2 + 换手8-35% + 非一字 + 缺口<8.5% + 价格≥3，E1 ---
main_df = sig_df[sig_df['board'] == 'main']
p8_mask = (main_df['zt10'] >= 1) & (main_df['vr'] >= 2.0) & \
          (main_df['turn'] >= 0.08) & (main_df['turn'] <= 0.35) & \
          main_df['not_yizi'] & (main_df['g_exec'] < 0.085) & main_df['ge3']
p8_sub = main_df[p8_mask]
recs = []
for _, r in p8_sub.iterrows():
    net, st = e1_path(r)
    if net is not None:
        recs.append((net, st))
res['P8'] = summarize(recs, 'P8 全池 E1')

# --- P8 每日量比前1 + E3 / 前3 + E1 ---
p8_ranked = p8_sub.sort_values(['date', 'vr'], ascending=[True, False])
p8_top1 = p8_ranked.groupby('date').head(1)
p8_top3 = p8_ranked.groupby('date').head(3)
recs = []
for _, r in p8_top1.iterrows():
    net, st = e3_path(r)
    if net is not None:
        recs.append((net, st))
res['P8_top1_E3'] = summarize(recs, 'P8 每日前1 E3')
recs = []
for _, r in p8_top3.iterrows():
    net, st = e1_path(r)
    if net is not None:
        recs.append((net, st))
res['P8_top3_E1'] = summarize(recs, 'P8 每日前3 E1')

# --- 全市场基线：每日量比前 30，E1 ---
base_ranked = sig_df.sort_values(['date', 'vr'], ascending=[True, False])
base_top30 = base_ranked.groupby('date').head(30)
recs = []
for _, r in base_top30.iterrows():
    net, st = e1_path(r)
    if net is not None:
        recs.append((net, st))
res['base_cap30'] = summarize(recs, '全市场基线 cap30 E1')

# --- GA 系（版本 B，主板）：缺口 g = O[T]/C[T-1]-1 ∈ 区间，O[T] 买，F1=T+1 收盘卖 ---
main_g = sig_df[sig_df['board'] == 'main'].copy()
main_g['prev_zt'] = main_g['is_zt']          # T-1 涨停（昨收涨停→今高开信号；is_zt 是 T 日属性…需 T-1）
# 昨日涨停 = T-1 日 is_zt → shift(1)
main_g['prev_is_zt'] = g['is_zt'].shift(1).reindex(main_g.index)
main_g['zt10_prev'] = g['zt10'].shift(1).reindex(main_g.index)  # T-10..T-1 窗口
main_g['g'] = main_g['open'] / main_g['prev_close'] - 1.0
main_g['ge3_prev'] = main_g['prev_close'] >= 3.0

# GA2：涨停基因 + 缺口[-3,+2]%
ga2 = main_g[(main_g['zt10_prev'] >= 1) & (main_g['g'] >= -0.03) & (main_g['g'] <= 0.02) & main_g['ge3_prev']]
recs = []
for _, r in ga2.iterrows():
    net, st = ga_path(r, 'F1')
    if net is not None:
        recs.append((net, st))
res['GA2'] = summarize(recs, 'GA2 涨停基因+缺口[-3,+2]% F1')

# GA10：昨日涨停 + 高开[+1,+6]%
ga10 = main_g[(main_g['prev_is_zt'] == 1) & (main_g['g'] >= 0.01) & (main_g['g'] <= 0.06) & main_g['ge3_prev']]
recs = []
for _, r in ga10.iterrows():
    net, st = ga_path(r, 'F1')
    if net is not None:
        recs.append((net, st))
res['GA10'] = summarize(recs, 'GA10 昨日涨停+高开[+1,+6]% F1')

# GA9：无涨停基因要求 + 缺口[+2,+7]%
ga9 = main_g[(main_g['g'] >= 0.02) & (main_g['g'] <= 0.07) & main_g['ge3_prev']]
recs = []
for _, r in ga9.iterrows():
    net, st = ga_path(r, 'F1')
    if net is not None:
        recs.append((net, st))
res['GA9'] = summarize(recs, 'GA9 缺口[+2,+7]% F1')

# --- P8 月度表（E1 全池，按信号日月份） ---
p8_sub = p8_sub.copy()
p8_sub['month'] = p8_sub['date'].str[:7]
month_rows = []
for m, grp in p8_sub.groupby('month'):
    rets = []
    for _, r in grp.iterrows():
        net, st = e1_path(r)
        if net is not None:
            rets.append(net)
    if rets:
        month_rows.append({'month': m, 'n': len(rets), 'avg_net_pct': float(np.mean(rets)) * 100})
res['P8_monthly'] = month_rows

# --- 漏斗计数（P8 各过滤级） ---
main_sig = sig_df[sig_df['board'] == 'main']
funnel = {
    'sig_total_main': int(len(main_sig)),
    'ge3': int(main_sig['ge3'].sum()),
    'zt10': int(main_sig['zt10'].sum()),
    'vr2': int((main_sig['vr'] >= 2.0).sum()),
    'turn_8_35': int(((main_sig['turn'] >= 0.08) & (main_sig['turn'] <= 0.35)).sum()),
    'not_yizi': int(main_sig['not_yizi'].sum()),
    'gap_lt_085': int((main_sig['g_exec'] < 0.085).sum()),
    'p8_full': int(len(p8_sub)),
    'p8_executed': int(len([r for _, r in p8_sub.iterrows() if e1_path(r)[0] is not None])),
}
res['funnel'] = funnel
res['meta'] = {
    'T0': T0, 'T1': T1, 'cost': COST,
    'kline_codes': int(df['code'].nunique()),
    'data_source': 'tencent_fqkline',
    'universe_after_filter': int(df['code'].nunique()),
    'excluded_st': int(len(uni_df[uni_df['is_st']])),
    'note': '腾讯 fqkline 全量重拉(2025-08-01~09-10, 标准qfq) + 新浪名单(09-10 时点)，判据见 tmp/qlw/pre_registered_judgment.md §0b',
}

with open(ROOT + '/data/bt_qlw.json', 'w', encoding='utf-8') as f:
    json.dump(res, f, ensure_ascii=False, indent=1)

# 打印
print('=' * 78)
for k in ['P0', 'P8', 'P8_top1_E3', 'P8_top3_E1', 'GA2', 'GA10', 'GA9', 'base_cap30']:
    s = res[k]
    if s['n']:
        print('%-12s n=%6d 胜率=%5.1f%% 单笔净=%+.4f%% PF=%.2f' % (
            k, s['n'], s['win_rate'] * 100, s['avg_net'] * 100, s['pf']))
    else:
        print('%-12s n=0' % k)
print('-' * 78)
print('P8 月度：')
for m in res['P8_monthly']:
    print('  %s  n=%4d  均净=%+.3f%%' % (m['month'], m['n'], m['avg_net_pct']))
print('漏斗：', json.dumps(res['funnel'], ensure_ascii=False))
print('saved data/bt_qlw.json')
