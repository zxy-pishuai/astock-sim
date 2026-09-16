# -*- coding: utf-8 -*-
"""C3（2026-09-13）：A1 打板入场端过滤研究（只读研究脚本，不改生产代码）

背景：atr_exit.md §8 结论——救"延长持有"的方向在入场端而非 ATR 参数；
C4 三臂 IS 归零已判 null（退出端封盘）。本脚本在 A1 基线（收盘打缩量非一字
首板 → 次日开盘卖）上逐个单因子测试入场端过滤。

判据预注册（先抄后跑，跑完未改）：
  双段期望 ≥ 基线+0.1pp，且至少一段 +0.5pp 或胜率 +2pp（该段 n≥300）；
  OOS 分年（2024/2025/2026）至少 2/3 同向。

数据：tmp/v1/events.pkl（2026-09-02 快照派生，含 2018-01-02~2026-09-02 日K）。
扣费与 atr_exit_backtest.py 同款：ret = sell*SF/(buy*BF_A1)-1。
本脚本只读，不写 market.db。
"""
import sys, json
import numpy as np
import pandas as pd
sys.stdout.reconfigure(encoding='utf-8')

SLIP = 0.002; COMM = 0.00025; STAMP = 0.0005; QUEUE = 0.001
BF_A1 = 1 + SLIP + QUEUE + COMM
SF = 1 - SLIP - COMM - STAMP
THR10, THR20 = 0.097, 0.194
SEAL_MAX = 5

def r(buy, sell):
    return sell * SF / (buy * BF_A1) - 1.0

# ---------------- 1. 数据与基线宇宙（finalize.py / atr_exit 同款） ----------------
E = pd.read_pickle('tmp/v1/events.pkl')
E['code'] = E['code'].astype(str)
E['is10'] = E['code'].str.startswith(('60', '00'))
g = E.groupby('code')
E['next_open'] = g['open'].shift(-1)
E['next_low'] = g['low'].shift(-1)
E['next_chg'] = g['chg'].shift(-1)
E['seq'] = g.cumcount()
thr_b = np.where(E['is10'], THR10, THR20)
E['is_sealed'] = E['is_zt'] | (E['chg'] <= -thr_b)

one = E[(E['is_zt']) & (E['lbc'] == 1) & (E['date'] >= '2019-01-01') & (E['date'] <= '2026-08-21')]
A = one[(one['board_type'] != '一字') & (one['amount_ratio'] < 1) & one['next_open'].notna()].copy()
A['is_is'] = A['date'] <= '2023-12-31'
A['year'] = A['date'].str[:4]
A['ret_base'] = [r(c, o) for c, o in zip(A['close'], A['next_open'])]
A['mae_base'] = (A['next_low'] - A['close']) / A['close']
print('基线宇宙 ALL=%d IS=%d OOS=%d' % (len(A), int(A['is_is'].sum()), int((~A['is_is']).sum())))

# 当日组（量比排名臂用）
A['rank_in_day'] = A.groupby('date')['amount_ratio'].rank(ascending=False, method='first')

E_by_code = {cd: E[E['code'] == cd].reset_index(drop=True) for cd in sorted(A['code'].unique())}

def hold_sell(seq, code, H):
    """持有 H 交易日强卖（封板日顺延至首个非封板日开盘卖，最多 SEAL_MAX 日）。
    返回 (sell_price, ok)。与 atr_exit 的 simulate 同款语义（无 ATR 分支）。"""
    rows = E_by_code[code].iloc[seq + 1: seq + 1 + H + SEAL_MAX + 2]
    if len(rows) == 0:
        return None, 0
    for j, (_, rr) in enumerate(rows.iterrows(), start=1):
        if j >= H:
            if rr['is_sealed']:
                for u in range(j + 1, min(j + SEAL_MAX + 1, len(rows) + 1)):
                    ru = rows.iloc[u - 1]
                    if not ru['is_sealed']:
                        return ru['open'], 1
                return None, 0
            return rr['close'], 1
        if rr['is_sealed']:
            continue
        # 未到 H：顺延逻辑只在卖出触发时用；这里继续持有（封板日顺延已由最终卖出处理）
    return None, 0

def stats(s):
    s = np.asarray(s, dtype=float)
    if len(s) == 0:
        return None
    return {'n': int(len(s)), 'winA': float((s > 0).mean()), 'mean_ret': float(s.mean()),
            'pf': float(s[s > 0].sum() / abs(s[s < 0].sum())) if (s < 0).any() else None,
            'median_ret': float(np.median(s))}

def arm_metrics(ret_all, is_is, years, mae_all=None):
    out = {}
    for seg, mask in (('ALL', np.ones(len(ret_all), bool)), ('IS', is_is), ('OOS', ~is_is)):
        s = ret_all[mask]
        st = stats(s)
        if st is not None and mae_all is not None:
            st['mae_mean'] = float(np.nanmean(mae_all[mask]))
        out[seg] = st
    out['by_year'] = {y: stats(ret_all[years == y]) for y in sorted(set(years))}
    return out

# ---------------- 2. 基线（T+1 开盘卖） ----------------
base_metrics = arm_metrics(A['ret_base'].values, A['is_is'].values, A['year'].values, A['mae_base'].values)
for seg in ('ALL', 'IS', 'OOS'):
    base_metrics[seg]['mae_mean'] = float(np.nanmean(A['mae_base'].values[
        A['is_is'].values if seg == 'IS' else (~A['is_is'].values if seg == 'OOS' else np.ones(len(A), bool))]))
print('基线 IS mean=%.4f n=%d win=%.4f | OOS mean=%.4f n=%d win=%.4f' % (
    base_metrics['IS']['mean_ret'], base_metrics['IS']['n'], base_metrics['IS']['winA'],
    base_metrics['OOS']['mean_ret'], base_metrics['OOS']['n'], base_metrics['OOS']['winA']))

# ---------------- 3. 前置诊断：延长持有分年（H=1/2/5/10/20） ----------------
print('\n=== 前置诊断：延长持有分年期望（%），基线=H1（次日开盘卖） ===')
diag = {}
for H in (1, 2, 5, 10, 20):
    rets, ok_mask = [], []
    if H == 1:
        rets = A['ret_base'].values
        ok_mask = np.ones(len(A), bool)
    else:
        rl = []
        for _, row in A.iterrows():
            sell, ok = hold_sell(row['seq'], row['code'], H)
            rl.append((None if not ok else r(row['close'], sell), ok))
        rets = np.array([x[0] for x in rl], dtype=float)
        ok_mask = np.array([x[1] for x in rl], bool)
    diag[H] = {}
    years = A['year'].values
    for y in sorted(set(years)):
        m = ok_mask & (years == y)
        diag[H][y] = {'n': int(m.sum()),
                      'mean': float(np.nanmean(rets[m])) if m.any() else None}
    print('H=%2d | %s' % (H, ' '.join('%s:%.3f%%(%d)' % (y, (diag[H][y]['mean'] or 0) * 100, diag[H][y]['n'])
                                       for y in ('2019', '2020', '2021', '2022', '2023'))))
    print('        %s' % (' '.join('%s:%.3f%%(%d)' % (y, (diag[H][y]['mean'] or 0) * 100, diag[H][y]['n'])
                                    for y in ('2024', '2025', '2026'))))

# 隔夜溢价 & 次日负收益占比（炸板代理）分年
print('\n=== 隔夜溢价与次日回落分年（基线宇宙） ===')
overnight = {}
A['gap'] = A['next_open'] / A['close'] - 1.0
A['gap_neg'] = A['gap'] < 0
for y in ('2019', '2020', '2021', '2022', '2023', '2024', '2025', '2026'):
    m = A['year'] == y
    overnight[y] = {'n': int(m.sum()),
                    'gap_mean': float(A['gap'][m].mean()),
                    'gap_neg_ratio': float(A['gap_neg'][m].mean()),
                    'ret_base': float(A['ret_base'][m].mean())}
    print('%s 隔夜溢价=%+.3f%% 负隔夜占比=%.1f%% 基线收益=%+.3f%% n=%d' % (
        y, overnight[y]['gap_mean'] * 100, overnight[y]['gap_neg_ratio'] * 100,
        overnight[y]['ret_base'] * 100, overnight[y]['n']))

# ---------------- 4. 六臂单因子过滤（纯入场端，卖出=T+1 开盘卖） ----------------
print('\n=== 各臂（基线宇宙子集，T+1 卖） ===')
arms = {}

# B1 量比排名（当日缩量首板内量比前 N）
for N in (1, 2, 3):
    m = A['rank_in_day'] <= N
    arms['B1_量比前%d' % N] = {'mask': m.values, 'desc': '当日缩量首板内量比排名前%d' % N}

# B3 二板预期/连板生态位（事件日可知信息，无未来函数）
arms['B3a_昨溢>0'] = {'mask': (A['prev_zt_premium'] > 0).values, 'desc': '昨日涨停溢价为正（隔日溢价环境好）'}
arms['B3b_连板高度>=3'] = {'mask': (A['max_days'] >= 3).values, 'desc': '当日市场最高连板>=3（有梯队）'}
arms['B3c_昨溢>0且高度>=3'] = {'mask': ((A['prev_zt_premium'] > 0) & (A['max_days'] >= 3)).values,
                             'desc': 'B3a∩B3b'}

# B4 情绪阶段闸门
for ph in ('发酵', '高潮', '冰点'):
    arms['B4_%s' % ph] = {'mask': (A['phase'] == ph).values, 'desc': '情绪阶段=%s' % ph}

# B6 首板类型（基线已排除一字）
arms['B6_T字回封'] = {'mask': (A['board_type'] == 'T字回封').values, 'desc': '首板类型=T字回封'}
arms['B6_换手板'] = {'mask': (A['board_type'] == 'none').values, 'desc': '首板类型=换手板（全天封死）'}

results = {}
for name, arm in arms.items():
    m = arm['mask']
    rets = A['ret_base'].values[m]
    is_is = A['is_is'].values[m]
    years = A['year'].values[m]
    mae = A['mae_base'].values[m]
    res = arm_metrics(rets, is_is, years, mae)
    results[name] = res
    is_ok = res['IS']['mean_ret'] >= base_metrics['IS']['mean_ret'] + 0.001
    oos_ok = res['OOS']['mean_ret'] >= base_metrics['OOS']['mean_ret'] + 0.001
    boost = ((res['IS']['mean_ret'] >= base_metrics['IS']['mean_ret'] + 0.005 and res['IS']['n'] >= 300) or
             (res['OOS']['mean_ret'] >= base_metrics['OOS']['mean_ret'] + 0.005 and res['OOS']['n'] >= 300) or
             (res['IS']['winA'] >= base_metrics['IS']['winA'] + 0.02 and res['IS']['n'] >= 300) or
             (res['OOS']['winA'] >= base_metrics['OOS']['winA'] + 0.02 and res['OOS']['n'] >= 300))
    years_ok = 0
    for y in ('2024', '2025', '2026'):
        by = res['by_year'].get(y)
        bb = base_metrics['by_year'].get(y)
        if by and bb and by.get('mean_ret') is not None and bb.get('mean_ret') is not None:
            if (by['mean_ret'] > 0) == (bb['mean_ret'] > 0):
                years_ok += 1
    results[name]['judge'] = {'is_ge_base_p01': bool(is_ok), 'oos_ge_base_p01': bool(oos_ok),
                              'boost': bool(boost), 'oos_years_same_dir': years_ok,
                              'pass': bool(is_ok and oos_ok and boost and years_ok >= 2)}
    print('%s n_IS=%d IS=%+.3f%% win=%.1f%% | n_OOS=%d OOS=%+.3f%% win=%.1f%% | PF=%s MAE=%.2f%% | OOS分年=%d/3 判过=%s' % (
        name, res['IS']['n'], res['IS']['mean_ret'] * 100, res['IS']['winA'] * 100,
        res['OOS']['n'], res['OOS']['mean_ret'] * 100, res['OOS']['winA'] * 100,
        ('%.2f' % res['OOS']['pf']) if res['OOS'].get('pf') else '-',
        (res['OOS'].get('mae_mean') or 0) * 100, years_ok, results[name]['judge']['pass']))

# 数据不可得臂（诚实 null）
arms['B2_封单强度'] = {'mask': None, 'desc': '数据不可得：无历史封单额/封单稳定性字段（events.pkl 无 seal，qg_zt_full 无封单）'}
arms['B5_板块强度'] = {'mask': None, 'desc': '数据不可得：app/sector.py 仅实时接口，无历史板块涨停家数（events.pkl 无 sector 字段）'}
for name in ('B2_封单强度', 'B5_板块强度'):
    results[name] = {'null': True, 'desc': arms[name]['desc'],
                     'reason': '历史数据不可得，不硬凑代理；建议后续在数据层补充封单/板块历史后再测'}

# ---------------- 5. 落盘 ----------------
out = {
    'judgment': 'PASS' if any(v.get('judge', {}).get('pass') for k, v in results.items() if 'judge' in v) else 'NULL',
    'baseline': {k: base_metrics[k] for k in ('ALL', 'IS', 'OOS')},
    'baseline_by_year': base_metrics['by_year'],
    'diagnose_hold_days': diag,
    'overnight_by_year': overnight,
    'arms': {k: {kk: vv for kk, vv in v.items() if kk != 'mask'} for k, v in results.items()},
    'arm_meta': {k: {'desc': v['desc'], 'n_selected': int(v['mask'].sum()) if v['mask'] is not None else None}
                 for k, v in arms.items()},
    'cost': {'slip': SLIP, 'comm': COMM, 'stamp': STAMP, 'queue': QUEUE},
    'meta': {'universe': 'A1收盘打缩量非一字首板(2019-01-01~2026-08-21)', 'is': '<=2023-12-31',
             'oos': '2024-01-01~2026-08-21', 'exit': 'T+1开盘卖（与基线同卖出端，纯入场端过滤）',
             'data': 'tmp/v1/events.pkl (2026-09-02快照派生)',
             'disclosure': ['幸存者偏差：events.pkl 仅含现存股，无退市股，绝对收益乐观（结构相对可比）',
                            '涨停价排队成交理想化：真实排队不一定买得到，额外成本容忍上限约+1%（V1 §7）',
                            'B2/B5 数据不可得，未测', '禁止组合调参：每臂单因子']}}
with open('data/bt_a1_entry.json', 'w', encoding='utf-8') as f:
    json.dump(out, f, ensure_ascii=False, indent=1, default=float)
print('\nsaved data/bt_a1_entry.json | 总体判定:', out['judgment'])
