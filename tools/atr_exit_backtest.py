# -*- coding: utf-8 -*-
"""C4 ATR 跟踪止损 vs 固定阈值，退出链对照回测（预注册判据见 tmp/pack19/C4_atr_exit.md §0/§0a）

基线 = V1 A1 卡（收盘打缩量非一字首板 → 次日开盘卖），IS +2.562%/OOS +2.279%。
T 臂 = 买入端逐位 identical，卖出端改 ATR 跟踪（静态 ATR14，k=2.5/3.0/3.5）：
  - 激活前：close <= buy*(1-7%) 止损（§0a.1）
  - 激活（close >= buy*1.04）后：trail = max(peak, close) - k*ATR14_static，close 跌破即卖（§0a.2 静态）
  - 第 20 交易日收盘强卖兜底（执行口径：防无限持有，与 W1R H 网格上限一致）
  - 封板日（涨停/跌停收盘）卖单顺延 → 首个非封板日开盘卖（engine 顺延语义）
扣费：ret = sell*SF/(buy*BF_A1)-1，SLIP=0.002 COMM=0.00025 STAMP=0.0005 QUEUE=0.001
IS=<=2023-12-31，OOS=2024-01-01~2026-08-21（与基线宇宙一致）
"""
import sys, json, pickle
import numpy as np
import pandas as pd
sys.stdout.reconfigure(encoding='utf-8')

SLIP = 0.002; COMM = 0.00025; STAMP = 0.0005; QUEUE = 0.001
BF_A1 = 1 + SLIP + QUEUE + COMM   # 打板买入端（含排队）
SF = 1 - SLIP - COMM - STAMP      # 卖出端
THR10, THR20 = 0.097, 0.194
ACTIVATE = 0.04; STOP = 0.07; H_MAX = 20; SEAL_MAX = 5

def r(buy, sell):
    return sell * SF / (buy * BF_A1) - 1.0

# ---------------- 1. 数据与基线宇宙（finalize.py 同款） ----------------
E = pd.read_pickle('tmp/v1/events.pkl')
E['code'] = E['code'].astype(str)
E['is10'] = E['code'].str.startswith(('60', '00'))
g = E.groupby('code')
E['next_open'] = g['open'].shift(-1)
E['next_low'] = g['low'].shift(-1)
E['seq'] = g.cumcount()
# 涨停/跌停封板判定（收盘封板）
thr_b = np.where(E['is10'], THR10, THR20)
E['is_sealed'] = E['is_zt'] | (E['chg'] <= -thr_b)

one = E[(E['is_zt']) & (E['lbc'] == 1) & (E['date'] >= '2019-01-01') & (E['date'] <= '2026-08-21')]
A = one[(one['board_type'] != '一字') & (one['amount_ratio'] < 1) & one['next_open'].notna()].copy()
A['is_is'] = A['date'] <= '2023-12-31'
A['year'] = A['date'].str[:4]
print('基线宇宙 ALL=%d IS=%d OOS=%d' % (len(A), int(A['is_is'].sum()), int((~A['is_is']).sum())))

# ---------------- 2. Wilder ATR14（全表，按票；静态值=事件日值） ----------------
def wilder_atr(df, period=14):
    codes = df['code'].values
    h, l, c, pc = df['high'].values, df['low'].values, df['close'].values, df['prev_close'].values
    tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
    out = np.full(len(df), np.nan)
    chg_idx = np.where(codes[1:] != codes[:-1])[0] + 1
    bounds = np.concatenate([[0], chg_idx, [len(df)]])
    for i in range(len(bounds) - 1):
        s0, s1 = bounds[i], bounds[i + 1]
        seg = tr[s0:s1].copy()
        if np.isnan(seg[0]):
            seg[0] = h[s0] - l[s0]   # 每票首行 prev_close NaN → TR 用 h-l
        n = len(seg)
        if n < period:
            continue
        a = seg[:period].mean()
        out[s0 + period - 1] = a
        for j in range(period, n):
            a = (a * (period - 1) + seg[j]) / period
            out[s0 + j] = a
    return out

E['atr14'] = wilder_atr(E)
A = A.drop(columns=['seq'], errors='ignore').merge(
    E[['code', 'date', 'atr14', 'seq', 'is_sealed']], on=['code', 'date'], how='left')

# ---------------- 3. 持有路径模拟（T 臂） ----------------
# 预取每票的日K子序列（供持有期切片）；只取事件票
ev_codes = sorted(A['code'].unique())
E_by_code = {cd: E[E['code'] == cd].reset_index(drop=True) for cd in ev_codes}

def simulate(in_code_seq, code, buy_close, atr_static, k):
    """返回 (sell_price, sold_flag)。sold_flag: 1=已卖出, 0=数据不足未成交（剔除）
    in_code_seq = 事件日在该票内的位置（E['seq']，0-based）。"""
    seq_df = E_by_code[code]
    rows = seq_df.iloc[in_code_seq + 1: in_code_seq + 1 + H_MAX + SEAL_MAX + 2]
    if len(rows) == 0:
        return None, 0
    peak = buy_close
    activated = False
    for j, (_, rr) in enumerate(rows.iterrows(), start=1):
        c = rr['close']
        if c >= buy_close * (1 + ACTIVATE):
            activated = True
        if activated:
            peak = max(peak, c)
            trail = peak - k * atr_static
            signal = c < trail
        else:
            signal = c <= buy_close * (1 - STOP)
        if j >= H_MAX and not signal:
            signal = True   # 第20日强卖
        if signal:
            if rr['is_sealed']:
                # 封板顺延：首个非封板日开盘卖（最多 SEAL_MAX 日）
                for u in range(j + 1, min(j + SEAL_MAX + 1, len(rows) + 1)):
                    ru = rows.iloc[u - 1]
                    if not ru['is_sealed']:
                        return ru['open'], 1
                return None, 0   # 顺延超限/数据不足 → 剔除
            return c, 1
    return None, 0   # 持有至数据末尾未卖出 → 剔除

def run_arm(k):
    recs, valid = [], []
    for _, row in A.iterrows():
        atr = row['atr14']
        if pd.isna(atr) or row['seq'] < 20:
            valid.append(False); recs.append(None); continue
        sell, ok = simulate(row['seq'], row['code'], row['close'], atr, k)
        if not ok:
            valid.append(False); recs.append(None); continue
        valid.append(True); recs.append(r(row['close'], sell))
    return np.array(recs, dtype=float), np.array(valid, dtype=bool)

def stats(ret, mae=None):
    s = np.asarray(ret)
    if len(s) == 0:
        return None
    return {'n': int(len(s)), 'winA': float((s > 0).mean()), 'mean_ret': float(s.mean()),
            'median_ret': float(np.median(s)), 'pf': float(s[s > 0].sum() / abs(s[s < 0].sum())) if (s < 0).any() else None,
            'mae_mean': float(np.nanmean(mae)) if mae is not None else None}

def arm_metrics(ret_all, is_is, years):
    out = {}
    for seg, mask in (('ALL', np.ones(len(ret_all), bool)), ('IS', is_is), ('OOS', ~is_is)):
        s = ret_all[mask]
        out[seg] = stats(s)
    out['by_year'] = {y: stats(ret_all[years == y]) for y in sorted(set(years))}
    return out

# 基线
A['ret_base'] = [r(c, o) for c, o in zip(A['close'], A['next_open'])]
A['mae_base'] = (A['next_low'] - A['close']) / A['close']
base_metrics = arm_metrics(A['ret_base'].values, A['is_is'].values, A['year'].values)
# 基线按年 MAE 不逐项给，ALL/IS/OOS 给
for seg in ('ALL', 'IS', 'OOS'):
    m = A['is_is'].values if seg == 'IS' else (~A['is_is'].values if seg == 'OOS' else np.ones(len(A), bool))
    base_metrics[seg]['mae_mean'] = float(np.nanmean(A['mae_base'].values[m]))
print('基线 IS mean=%.4f n=%d | OOS mean=%.4f n=%d' % (
    base_metrics['IS']['mean_ret'], base_metrics['IS']['n'],
    base_metrics['OOS']['mean_ret'], base_metrics['OOS']['n']))

arms = {}
arm_results = {}
arm_valid = {}
arm_ret_full = {}
for name, k in (('T1_k25', 2.5), ('T2_k30', 3.0), ('T3_k35', 3.5)):
    ret, valid = run_arm(k)
    ret_f = np.array([np.nan if x is None else float(x) for x in ret])
    idx_v = np.where(valid)[0]
    ret_v = ret_f[valid]
    is_is_v = A['is_is'].values[idx_v]
    years_v = A['year'].values[idx_v]
    arm_valid[name] = valid
    arm_ret_full[name] = ret_f
    arms[name] = {'k': k, 'valid_n': int(valid.sum()), 'drop_n': int((~valid).sum()),
                  'drop_atr': int((~valid & (pd.isna(A['atr14'].values) | (A['seq'].values < 20))).sum()),
                  'drop_data': int((~valid & ~(pd.isna(A['atr14'].values) | (A['seq'].values < 20))).sum())}
    arm_results[name] = arm_metrics(ret_v, is_is_v, years_v)
    print('%s: valid=%d drop=%d (ATR不足=%d 数据不足=%d) | IS mean=%.4f n=%d | OOS mean=%.4f n=%d' % (
        name, arms[name]['valid_n'], arms[name]['drop_n'], arms[name]['drop_atr'], arms[name]['drop_data'],
        arm_results[name]['IS']['mean_ret'], arm_results[name]['IS']['n'],
        arm_results[name]['OOS']['mean_ret'], arm_results[name]['OOS']['n']))

# ---------------- 4. 波动率分层（IS 段三分位切点映射 OOS，只解释） ----------------
atr_v = A['atr14'].values
is_is = A['is_is'].values
q33, q66 = np.nanpercentile(atr_v[is_is], [33.33, 66.67])
band = np.where(atr_v <= q33, '低波', np.where(atr_v <= q66, '中波', '高波'))
strata = {}
for b in ('低波', '中波', '高波'):
    m = (band == b) & ~pd.isna(atr_v)
    m_t2 = m & arm_valid['T2_k30']
    strata[b] = {'n': int(m.sum()), 'n_t2': int(m_t2.sum()),
                 'base_IS': float(np.nanmean(A['ret_base'].values[m & is_is])),
                 'base_OOS': float(np.nanmean(A['ret_base'].values[m & ~is_is])),
                 't2_IS': float(np.nanmean(arm_ret_full['T2_k30'][m_t2 & is_is])) if m_t2.any() else None,
                 't2_OOS': float(np.nanmean(arm_ret_full['T2_k30'][m_t2 & ~is_is])) if m_t2.any() else None,
                 'med_atr': float(np.nanmedian(atr_v[m]))}
print('分层切点 q33=%.4f q66=%.4f' % (q33, q66))

# ---------------- 5. 判过判定（预注册判据） ----------------
judge = {}
for name in arms:
    m = arm_results[name]
    is_ok = m['IS']['mean_ret'] >= base_metrics['IS']['mean_ret'] - 0.001
    oos_ok = m['OOS']['mean_ret'] >= base_metrics['OOS']['mean_ret'] - 0.001
    boost = ((m['IS']['mean_ret'] >= base_metrics['IS']['mean_ret'] + 0.005 and m['IS']['n'] >= 300) or
             (m['OOS']['mean_ret'] >= base_metrics['OOS']['mean_ret'] + 0.005 and m['OOS']['n'] >= 300) or
             (m['IS']['winA'] >= base_metrics['IS']['winA'] + 0.02 and m['IS']['n'] >= 300) or
             (m['OOS']['winA'] >= base_metrics['OOS']['winA'] + 0.02 and m['OOS']['n'] >= 300))
    years_ok = 0
    for y in ('2024', '2025', '2026'):
        if m['by_year'].get(y) and base_metrics['by_year'].get(y):
            if (m['by_year'][y]['mean_ret'] > 0) == (base_metrics['by_year'][y]['mean_ret'] > 0):
                years_ok += 1
    judge[name] = {'is_not_inferior': bool(is_ok), 'oos_not_inferior': bool(oos_ok), 'boost': bool(boost),
                   'oos_years_same_dir': years_ok, 'pass': bool(is_ok and oos_ok and boost and years_ok >= 2)}
    print('%s 判过=%s (IS≥基-0.1pp:%s OOS≥基-0.1pp:%s 提升:%s OOS分年同向:%d/3)' % (
        name, judge[name]['pass'], is_ok, oos_ok, boost, years_ok))

# ---------------- 6. 落盘 ----------------
out = {
    'judgment': 'PASS' if any(j['pass'] for j in judge.values()) else 'NULL',
    'judge_detail': judge,
    'baseline': base_metrics,
    'arms': arm_results,
    'arm_drops': arms,
    'strata': strata,
    'cost': {'slip': SLIP, 'comm': COMM, 'stamp': STAMP, 'queue': QUEUE, 'bf_a1': BF_A1, 'sf': SF},
    'meta': {'universe': 'A1收盘打缩量非一字首板(2019-01-01~2026-08-21)', 'is': '<=2023-12-31',
             'oos': '2024-01-01~2026-08-21', 'atr': 'Wilder ATR14 事件日静态值',
             'exit': '激活4%后trail=peak-k*ATR / 激活前-7%止损 / H20强卖 / 封板顺延次日开盘',
             'data': 'tmp/v1/events.pkl (2026-09-02快照派生)'}}
with open('data/bt_atr_exit.json', 'w', encoding='utf-8') as f:
    json.dump(out, f, ensure_ascii=False, indent=1, default=float)
print('saved data/bt_atr_exit.json')
