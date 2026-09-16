# -*- coding: utf-8 -*-
"""
tactic_lianban_backtest.py — V1 连板梯队历史回测（pack13）
纯只读快照库；零落地。输出 data/bt_tactic_lianban.json 并打印关键表。

数据源:
  - tmp/v1/kline_day.pkl  (3,866,561 行, 60/00/30/68, 2018-01-02~2026-09-02)
  - tmp/v1/events.pkl     (全量 kline + is_zt/lbc/next_lbc/特征/情绪 join)
正确性门(§0.5): 干净窗口 08-14~08-21 对 limit_pool 一致率 97.83% (540/552) 已单独验证。

口径(预注册, 见 docs/reports/tactic_lianban_backtest.md §0):
  - 涨停: 10%板 chg>=0.097, 20%板 chg>=0.194; 除权粗剔除 |chg|>band; 新股 seq<5 豁免; lbc=连续涨停计数
  - 成本: 佣金0.025%双边 + 印花0.05%卖 + 滑点0.2%/笔; A1打板 +0.1%排队惩罚 (拍脑袋, 披露)
  - 三打法: A1 打板(收盘涨停视为成交, 一字剔除), A2 次日开盘买(剔开盘一字), A3 断板低吸(开盘或-3%挂单近似)
  - 卖点: {next_open, next_close, break_next_open}; A3 用 {b+1 open, b+1 close, b+2 open}
  - 胜率B=晋级率(事件级), 胜率A=打法级扣费后>0; 样本<100 标"不可信"
  - 主窗口 2019-01-01~2026-08-31; IS=2019~2023-12, OOS=2024-01~2026-08
"""
import json, sys
import numpy as np
import pandas as pd

pd.set_option('display.width', 200)
OUT = 'data/bt_tactic_lianban.json'
MIN_N = 100  # 样本<100 标不可信

# ---------- 成本 ----------
SLIP = 0.002       # 滑点 0.2%/笔(买卖各一)
COMM = 0.00025     # 佣金 0.025% 单边
STAMP = 0.0005     # 印花税 0.05% 卖
QUEUE = 0.001      # A1 打板排队惩罚
BUY_FEE_A1 = 1 + SLIP + QUEUE + COMM
BUY_FEE_A2A3 = 1 + SLIP + COMM
SELL_FEE = 1 - SLIP - COMM - STAMP

def ret(buy_price, sell_price, buy_fee):
    if buy_price is None or buy_price <= 0 or sell_price is None or sell_price <= 0:
        return np.nan
    return sell_price * SELL_FEE / (buy_price * buy_fee) - 1.0

def stat(s):
    """s: 每笔收益序列(numpy)"""
    s = np.asarray(s, dtype=float)
    s = s[~np.isnan(s)]
    n = len(s)
    out = {'n': int(n), 'winA': None, 'mean_ret': None, 'median_ret': None,
           'pf': None, 'mae': None}
    if n == 0:
        return out
    out['winA'] = float((s > 0).mean())
    out['mean_ret'] = float(s.mean())
    out['median_ret'] = float(np.median(s))
    pos = s[s > 0].sum(); neg = -s[s < 0].sum()
    out['pf'] = float(pos / neg) if neg > 0 else (np.inf if pos > 0 else np.nan)
    return out

def main():
    print('load pkl ...')
    E = pd.read_pickle('tmp/v1/events.pkl')
    K = pd.read_pickle('tmp/v1/kline_day.pkl')
    K['code'] = K['code'].astype(str)
    K['date'] = K['date'].astype(str)
    E = E.merge(K[['code', 'date', 'amount', 'volume']], on=['code', 'date'], how='left')
    E = E.sort_values(['code', 'date']).reset_index(drop=True)
    E['year'] = E['date'].str[:4]
    # 窗口标记
    E['in_win'] = (E['date'] >= '2019-01-01') & (E['date'] <= '2026-08-31')
    E['is_is'] = (E['date'] <= '2023-12-31')
    E['is_oos'] = (E['date'] >= '2024-01-01')
    g = E.groupby('code')

    print('build next-day cols ...')
    E['next_open'] = g['open'].shift(-1)
    E['next_close'] = g['close'].shift(-1)
    E['next_low'] = g['low'].shift(-1)
    E['next2_open'] = g['open'].shift(-2)
    E['next2_close'] = g['close'].shift(-2)
    E['next2_low'] = g['low'].shift(-2)
    E['next3_open'] = g['open'].shift(-3)
    E['next_is_zt'] = g['is_zt'].shift(-1).fillna(False).astype(bool)
    E['next_board_type'] = g['board_type'].shift(-1)
    # 连板 run: 断板次日开盘 & run 窗口最低价
    isz = E['is_zt'].values
    nxt_isz = E['next_is_zt'].values
    run_end = isz & (~nxt_isz)
    brk_open = g['open'].shift(-2)  # run_end 行 -> e+2 开盘
    E['_brk'] = np.where(run_end, brk_open, np.nan)
    E['_brk_low'] = np.where(run_end, g['low'].shift(-1), np.nan)   # 断板日低点
    E['_brk_open'] = np.where(run_end, g['open'].shift(-1), np.nan) # 断板日开盘
    E['_brk1_open'] = np.where(run_end, brk_open, np.nan)           # e+2 开盘 (断板次日)
    E['_brk1_close'] = np.where(run_end, g['close'].shift(-2), np.nan)
    # run 分组 id (仅 zt 行有效)
    prev_isz = g['is_zt'].shift(1).fillna(False).astype(bool).values
    zt_start = isz & (~prev_isz)
    E['_zg'] = np.where(isz, zt_start.cumsum(), np.nan)
    # 组内回填: 每个 zt run 的所有行拿到 run_end 的 _brk/_brk_low/...
    for col in ['_brk', '_brk_low', '_brk_open', '_brk1_open', '_brk1_close']:
        E[col] = E.groupby('_zg')[col].bfill()
    # run 窗口最低价 (run_start..e+2)
    E['_run_min_low'] = E.groupby('_zg')['low'].transform('min')
    # 但要覆盖到 e+2: 用 groupby 的 min 只到 run 末尾; e+2 的低点未含. 简化: 用 run min 与 e+2 low 取 min
    E['_run_min_low2'] = E['_run_min_low']
    mask = run_end & E['_brk1_open'].notna()
    E.loc[mask, '_run_min_low2'] = E.loc[mask, ['_run_min_low', '_brk1_close']].min(axis=1)

    zt = E[E['is_zt'] & E['in_win']].copy()

    # ================= §1 阶梯表 =================
    print('\n===== §1 阶梯表 P(n->n+1) =====')
    def ladder(df):
        out = {}
        for n in [1, 2, 3]:
            sub = df[df['lbc'] == n]
            p = sub['next_lbc'].eq(n + 1).mean()
            out[f'{n}->{n+1}'] = {'n': int(len(sub)), 'P': float(p) if not np.isnan(p) else None}
        sub = df[df['lbc'] >= 4]
        p = sub['next_lbc'].eq(sub['lbc'] + 1).mean()
        out['4+->+1'] = {'n': int(len(sub)), 'P': float(p) if not np.isnan(p) else None}
        return out
    ladder_full = ladder(zt)
    ladder_is = ladder(zt[zt['is_is']])
    ladder_oos = ladder(zt[zt['is_oos']])
    # 按年
    ladder_year = {}
    for y, sub in zt.groupby('year'):
        if y < '2019' or y > '2026':
            continue
        ladder_year[y] = ladder(sub)
    print('full:', json.dumps(ladder_full, ensure_ascii=False))
    print('IS  :', json.dumps(ladder_is, ensure_ascii=False))
    print('OOS :', json.dumps(ladder_oos, ensure_ascii=False))

    # ================= §2 特征 x 晋级率 (一进二, lbc==1) =================
    print('\n===== §2 特征 x P(1->2) =====')
    one = zt[zt['lbc'] == 1].copy()
    one['promo'] = one['next_lbc'].ge(2)
    # amount 分位阈值 (全样本 zt amount)
    amt_p = one['amount'].quantile([0.25, 0.5, 0.75])
    amt_bin = pd.cut(one['amount'], [-np.inf, amt_p[0.25], amt_p[0.5], amt_p[0.75], np.inf],
                     labels=['<p25', 'p25-50', 'p50-75', '>p75'])
    one['amt_bucket'] = amt_bin
    one['ar_bucket'] = pd.cut(one['amount_ratio'], [-np.inf, 1, 2, 5, np.inf],
                              labels=['<1', '1-2', '2-5', '>5'])
    zc_p = one['zt_count'].quantile([1 / 3, 2 / 3])
    pp_p = one['prev_zt_premium'].quantile([1 / 3, 2 / 3])
    one['zc_terc'] = pd.cut(one['zt_count'], [-np.inf, zc_p.iloc[0], zc_p.iloc[1], np.inf],
                            labels=['zc_low', 'zc_mid', 'zc_high'])
    one['pp_terc'] = pd.cut(one['prev_zt_premium'], [-np.inf, pp_p.iloc[0], pp_p.iloc[1], np.inf],
                            labels=['pp_low', 'pp_mid', 'pp_high'])

    def feat_slice(col):
        out = {}
        for v, sub in one.groupby(col, dropna=False):
            key = str(v)
            rec = {'n': int(len(sub)), 'P': float(sub['promo'].mean())}
            sub_is = sub[sub['is_is']]; sub_os = sub[sub['is_oos']]
            rec['P_IS'] = float(sub_is['promo'].mean()) if len(sub_is) else None
            rec['P_OOS'] = float(sub_os['promo'].mean()) if len(sub_os) else None
            out[key] = rec
        return out

    features = {}
    for col in ['board_type', 'amt_bucket', 'ar_bucket', 'trend_vol', 'new20d_high',
                'phase', 'zc_terc', 'pp_terc']:
        features[col] = feat_slice(col)
        print(f'-- {col}:', json.dumps(features[col], ensure_ascii=False))

    # ================= §3/§4 三打法回测 =================
    print('\n===== §3/§4 三打法网格 =====')
    # 打法事件: 用 2019-01-01 ~ 2026-08-21 (保证 t+1/t+2 数据完整)
    trade_win = (zt['date'] >= '2019-01-01') & (zt['date'] <= '2026-08-21')
    T = zt[trade_win].copy()
    T['is_is'] = T['date'] <= '2023-12-31'
    T['is_oos'] = T['date'] >= '2024-01-01'

    # A1 打板: 收盘买 (一字剔除)
    a1 = T[T['board_type'] != '一字'].copy()
    a1_buy = a1['close']
    # A2 开盘追: t+1 开盘买 (剔 t+1 一字)
    a2 = T[T['next_open'].notna() & (T['next_board_type'] != '一字')].copy()
    a2_buy = a2['next_open']
    # A3 断板低吸: 断板日开盘或 -3%
    a3 = T[T['_brk_open'].notna()].copy()
    dip = a3['_brk_low'] <= a3['_brk_open'] * 0.97
    a3_buy = np.where(dip, a3['_brk_open'] * 0.97, a3['_brk_open'])

    # 统一 sell 列 (按 buy 日 T=buy day 的语义):
    # A1: buy day=t, sell: t+1 open/close, break_next_open
    # A2: buy day=t+1, sell: t+2 open/close, break_next_open
    # A3: buy day=b,  sell: b+1 open/close, b+2 open
    sells = {}
    sells['A1'] = {
        'next_open':   a1['next_open'],
        'next_close':  a1['next_close'],
        'break_next':  a1['_brk1_open'],
        'mae_next':    (a1['next_low'] - a1_buy) / a1_buy,
        'mae_break':   (a1['_run_min_low2'] - a1_buy) / a1_buy,
        'buy':         a1_buy,
        'buy_fee':     BUY_FEE_A1,
        'mae_next2':   None,
    }
    sells['A2'] = {
        'next_open':   a2['next2_open'],
        'next_close':  a2['next2_close'],
        'break_next':  a2['_brk1_open'],
        'mae_next':    (np.minimum(a2['next_low'].fillna(np.inf), a2['next2_low'].fillna(np.inf)) - a2_buy) / a2_buy,
        'mae_break':   (a2['_run_min_low2'] - a2_buy) / a2_buy,
        'buy':         a2_buy,
        'buy_fee':     BUY_FEE_A2A3,
        'mae_next2':   None,
    }
    sells['A3'] = {
        'next_open':   a3['_brk1_open'],
        'next_close':  a3['_brk1_close'],
        'break_next':  a3['next2_open'],
        'mae_next':    (a3['_brk_low'] - a3_buy) / a3_buy,
        'mae_break':   (a3['_run_min_low2'] - a3_buy) / a3_buy,
        'buy':         pd.Series(a3_buy, index=a3.index),
        'buy_fee':     BUY_FEE_A2A3,
        'mae_next2':   None,
    }
    frames = {'A1': a1, 'A2': a2, 'A3': a3}
    buys = {'A1': a1_buy, 'A2': a2_buy, 'A3': pd.Series(a3_buy, index=a3.index)}
    buy_fees = {'A1': BUY_FEE_A1, 'A2': BUY_FEE_A2A3, 'A3': BUY_FEE_A2A3}

    grid = []
    for pb in ['A1', 'A2', 'A3']:
        df = frames[pb]
        buy = buys[pb].values
        bf = buy_fees[pb]
        for sp in ['next_open', 'next_close', 'break_next']:
            sp_col = sells[pb][sp]
            for mask, lab in [(df['is_is'].values, 'IS'), (df['is_oos'].values, 'OOS'), (np.ones(len(df), bool), 'ALL')]:
                idx = mask & sp_col.notna().values
                r = np.array([ret(buy[i], sp_col.values[i], bf) for i in np.where(idx)[0]])
                st = stat(r)
                mae_key = 'mae_next' if sp in ('next_open', 'next_close') else 'mae_break'
                mae_arr = sells[pb][mae_key].values[idx]
                mae = float(np.nanmean(mae_arr)) if len(mae_arr) else None
                st['mae'] = mae
                st['period'] = lab
                st['playback'] = pb
                st['sell'] = sp
                st['n_board_focus'] = '1进2'  # 主分析在 lbc==1
                grid.append(st)
    # 只取 lbc==1 的一进二为主 (网格), 同时输出全 n 版本
    grid_1to2 = [g for g in grid]  # 已按 lbc==1? 否 —— 上面用了全部 T
    # 重新按 lbc==1 计算网格
    grid = []
    for pb in ['A1', 'A2', 'A3']:
        df = frames[pb]
        df1 = df[df['lbc'] == 1]
        buy = buys[pb].loc[df1.index].values
        bf = buy_fees[pb]
        for sp in ['next_open', 'next_close', 'break_next']:
            sp_col = sells[pb][sp].loc[df1.index]
            for mask, lab in [(df1['is_is'].values, 'IS'), (df1['is_oos'].values, 'OOS'), (np.ones(len(df1), bool), 'ALL')]:
                idx = mask & sp_col.notna().values
                r = np.array([ret(buy[i], sp_col.values[i], bf) for i in np.where(idx)[0]])
                st = stat(r)
                mae_key = 'mae_next' if sp in ('next_open', 'next_close') else 'mae_break'
                mae_arr = sells[pb][mae_key].loc[df1.index].values[idx]
                st['mae'] = float(np.nanmean(mae_arr)) if len(mae_arr) else None
                st['period'] = lab
                st['playback'] = pb
                st['sell'] = sp
                grid.append(st)

    print('\n--- 一进二网格 (IS) ---')
    for g in grid:
        if g['period'] == 'IS':
            print(f"{g['playback']}/{g['sell']:11s} n={g['n']:6d} winA={g['winA'] if g['winA'] is None else round(g['winA'],4)} mean={None if g['mean_ret'] is None else round(g['mean_ret']*100,3)}% pf={None if g['pf'] is None else round(g['pf'],2)}")
    print('\n--- 一进二网格 (OOS) ---')
    for g in grid:
        if g['period'] == 'OOS':
            print(f"{g['playback']}/{g['sell']:11s} n={g['n']:6d} winA={g['winA'] if g['winA'] is None else round(g['winA'],4)} mean={None if g['mean_ret'] is None else round(g['mean_ret']*100,3)}% pf={None if g['pf'] is None else round(g['pf'],2)}")
    print('\n--- 一进二网格 (ALL) ---')
    for g in grid:
        if g['period'] == 'ALL':
            print(f"{g['playback']}/{g['sell']:11s} n={g['n']:6d} winA={g['winA'] if g['winA'] is None else round(g['winA'],4)} mean={None if g['mean_ret'] is None else round(g['mean_ret']*100,3)}% pf={None if g['pf'] is None else round(g['pf'],2)}")

    # 按年分布 (A2/next_close 为例)
    year_dist = {}
    for pb in ['A1', 'A2', 'A3']:
        df = frames[pb][frames[pb]['lbc'] == 1]
        buy = buys[pb].loc[df.index]
        sp = sells[pb]['next_close'].loc[df.index]
        m = sp.notna()
        dfy = df[m]
        r = np.array([ret(buy.values[i], sp.values[i], buy_fees[pb]) for i in range(len(dfy))])
        year_dist[pb] = {y: {'n': int(n), 'mean_ret': float(np.nanmean(r[dfy['year'].values == y])) if n else None}
                         for y, n in dfy.groupby('year').size().items()}

    # ================= 全 n 阶梯打法概览 (非一进二也看一眼) =================
    grid_alln = {}
    for pb in ['A1', 'A2', 'A3']:
        df = frames[pb]
        buy = buys[pb].loc[df.index]
        sp = sells[pb]['next_close'].loc[df.index]
        m = sp.notna()
        dfm = df[m]
        r = np.array([ret(buy.values[i], sp.values[i], buy_fees[pb]) for i in range(len(dfm))])
        d = pd.DataFrame({'lbc': dfm['lbc'].values, 'ret': r})
        grid_alln[pb] = {int(n): {'n': int(c), 'mean': float(g['ret'].mean()), 'winA': float((g['ret'] > 0).mean())}
                         for n, g in d.groupby('lbc') if int(c := len(g)) >= 30}

    # ================= §6 情绪闸门专题 =================
    print('\n===== §6 情绪闸门 =====')
    senti = {}
    for ph in ['冰点', '发酵', '高潮', '退潮']:
        sub = one[one['phase'] == ph]
        rec = {'n_evt': int(len(sub)), 'P12': float(sub['promo'].mean()) if len(sub) else None}
        # A2/next_close 在 phase 上的收益
        a2ph = a2[a2['phase'] == ph]
        a2ph1 = a2ph[a2ph['lbc'] == 1]
        sp = sells['A2']['next_close'].loc[a2ph1.index]
        m = sp.notna()
        if m.sum() > 0:
            r = np.array([ret(buys['A2'].loc[a2ph1.index].values[i], sp.values[i], BUY_FEE_A2A3) for i in np.where(m.values)[0]])
            rec['A2_nc_n'] = int(len(r))
            rec['A2_nc_mean'] = float(np.nanmean(r))
            rec['A2_nc_winA'] = float((r > 0).mean())
        senti[ph] = rec
        print(ph, json.dumps(rec, ensure_ascii=False))
    # zt_count 三分位与 prev_premium 三分位下的 P12
    senti['zt_count_terc'] = feat_slice('zc_terc')
    senti['prev_premium_terc'] = feat_slice('pp_terc')

    # ================= 三周真值因子体检 =================
    print('\n===== 三周真值因子体检 (limit_pool 窗口) =====')
    truth = truth_factor_check()
    print(json.dumps(truth, ensure_ascii=False, default=float))

    # ================= 组装 JSON =================
    result = {
        'meta': {
            'task': 'V1 连板梯队历史回测 (pack13)',
            'restored_at': pd.Timestamp.now().strftime('%Y-%m-%d %H:%M:%S'),
            'method': 'read-only snapshot market.db (2026-09-02), kline_day.pkl + events.pkl',
            'window': '2019-01-01~2026-08-31', 'IS': '2019~2023-12', 'OOS': '2024-01~2026-08',
            'cost': {'commission': '0.025% x2', 'stamp': '0.05% sell', 'slippage': '0.2%/side',
                     'A1_queue_penalty': '0.1%', 'note': '拍脑袋参数, 披露'},
            'gate': {'gate_verdict': 'PASS (clean window 97.83% >= 95%)',
                     'detail': '见 docs/reports/tactic_lianban_backtest.md §0.5'},
            'V0': 'absent, 判据未经外部对表'
        },
        'ladder': {'full': ladder_full, 'is': ladder_is, 'oos': ladder_oos, 'by_year': ladder_year},
        'features': features,
        'grid_1to2': grid,
        'year_dist': year_dist,
        'grid_alln': grid_alln,
        'sentiment': senti,
        'truth_factor': truth,
    }
    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    print(f'\nwrote {OUT}')

def truth_factor_check():
    """三周真值因子: limit_pool 窗口 fund/ltsz, zbc, fbt, hs -> 次日晋级率方向"""
    import sqlite3
    DB = 'file:C:/Users/26838/A股模拟盘/data/snapshots/2026-09-02/market.db?mode=ro&immutable=1'
    conn = sqlite3.connect(DB, uri=True)
    lp = pd.read_sql_query("SELECT date, code, payload FROM limit_pool WHERE kind='zt'", conn)
    conn.close()
    lp['date'] = pd.to_datetime(lp['date'], format='%Y%m%d')
    lp['code'] = lp['code'].astype(str)
    # 对齐表 pool.date -> kline 交易日
    align = {
        '2026-08-15': '2026-08-14', '2026-08-16': '2026-08-14', '2026-08-17': '2026-08-17',
        '2026-08-18': '2026-08-17', '2026-08-19': '2026-08-18', '2026-08-20': '2026-08-19',
        '2026-08-21': '2026-08-21', '2026-08-22': '2026-08-21', '2026-08-23': '2026-08-21',
        '2026-08-24': '2026-08-24', '2026-08-25': '2026-08-25', '2026-08-26': '2026-08-25',
        '2026-08-27': '2026-08-26', '2026-08-28': '2026-08-28', '2026-08-29': '2026-08-28',
        '2026-08-30': '2026-08-28', '2026-08-31': '2026-08-28', '2026-09-01': '2026-08-31',
        '2026-09-02': '2026-09-01',
    }
    lp['kdate'] = lp['date'].dt.strftime('%Y-%m-%d').map(align)
    E = pd.read_pickle('tmp/v1/events.pkl')
    E['code'] = E['code'].astype(str)
    E['date'] = pd.to_datetime(E['date'])
    ev = E[E['is_zt']][['code', 'date', 'lbc', 'next_lbc']].copy()
    ev['date'] = ev['date'].dt.strftime('%Y-%m-%d')
    ev = ev.rename(columns={'date': 'kdate'})
    m = lp.merge(ev, on=['code', 'kdate'], how='left')
    m = m[~m['code'].str.startswith('92')]
    m['promo'] = m['next_lbc'].ge(m['lbc'] + 1)
    def pload(x):
        try:
            return json.loads(x)
        except Exception:
            return {}
    m['pl'] = m['payload'].map(pload)
    m['fund_ltsz'] = m['pl'].map(lambda p: p.get('fund', 0) / p['ltsz'] if p.get('ltsz') else np.nan)
    m['zbc'] = m['pl'].map(lambda p: p.get('zbc'))
    m['fbt'] = m['pl'].map(lambda p: p.get('fbt'))
    m['hs'] = m['pl'].map(lambda p: p.get('hs'))
    out = {}
    for col, buckets in [('fund_ltsz', [0.01, 0.03, 0.10]),
                         ('zbc', [1, 2, 3]),
                         ('fbt', [93000, 100000, 103000, 110000]),
                         ('hs', [5, 10, 20])]:
        b = pd.cut(m[col], [-np.inf] + buckets + [np.inf], labels=[f'<{buckets[0]}'] + [f'{buckets[i]}-{buckets[i+1]}' for i in range(len(buckets)-1)] + [f'>{buckets[-1]}'])
        tmp = pd.DataFrame({'b': b, 'promo': m['promo'], 'n': 1})
        rec = {}
        for k, sub in tmp.groupby('b', dropna=False):
            rec[str(k)] = {'n': int(len(sub)), 'promo_rate': float(sub['promo'].mean()) if sub['promo'].notna().any() else None}
        out[col] = rec
    return out

def _load_events(board_split):
    """E3：只读复用 tmp/v1 缓存，按板块过滤（all=全量 / 10cm=60/00 / 20cm=30/68）。
    返回 merge amount/volume 后、按 code/date 排序、带 year 的 E。"""
    E = pd.read_pickle('tmp/v1/events.pkl')
    K = pd.read_pickle('tmp/v1/kline_day.pkl')
    K['code'] = K['code'].astype(str)
    K['date'] = K['date'].astype(str)
    E = E.merge(K[['code', 'date', 'amount', 'volume']], on=['code', 'date'], how='left')
    E = E.sort_values(['code', 'date']).reset_index(drop=True)
    E['year'] = E['date'].str[:4]
    if board_split == '10cm':
        E = E[E['code'].str[:2].isin(['60', '00'])].copy()
    elif board_split == '20cm':
        E = E[E['code'].str[:2].isin(['30', '68'])].copy()
    elif board_split != 'all':
        raise ValueError('board_split must be in all/10cm/20cm, got %r' % board_split)
    return E


def _build_trades(E):
    """E3：复刻 main() 的 next-day 列 + 三打法交易构造（含 run 断板窗口）。
    返回 (T, frames, buys, buy_fees, sells) —— T 为 trade_win 内 zt 事件。"""
    E['in_win'] = (E['date'] >= '2019-01-01') & (E['date'] <= '2026-08-31')
    E['is_is'] = (E['date'] <= '2023-12-31')
    E['is_oos'] = (E['date'] >= '2024-01-01')
    g = E.groupby('code')
    E['next_open'] = g['open'].shift(-1)
    E['next_close'] = g['close'].shift(-1)
    E['next_low'] = g['low'].shift(-1)
    E['next2_open'] = g['open'].shift(-2)
    E['next2_close'] = g['close'].shift(-2)
    E['next2_low'] = g['low'].shift(-2)
    E['next3_open'] = g['open'].shift(-3)
    E['next_is_zt'] = g['is_zt'].shift(-1).fillna(False).astype(bool)
    E['next_board_type'] = g['board_type'].shift(-1)
    isz = E['is_zt'].values
    nxt_isz = E['next_is_zt'].values
    run_end = isz & (~nxt_isz)
    brk_open = g['open'].shift(-2)
    E['_brk'] = np.where(run_end, brk_open, np.nan)
    E['_brk_low'] = np.where(run_end, g['low'].shift(-1), np.nan)
    E['_brk_open'] = np.where(run_end, g['open'].shift(-1), np.nan)
    E['_brk1_open'] = np.where(run_end, brk_open, np.nan)
    E['_brk1_close'] = np.where(run_end, g['close'].shift(-2), np.nan)
    prev_isz = g['is_zt'].shift(1).fillna(False).astype(bool).values
    zt_start = isz & (~prev_isz)
    E['_zg'] = np.where(isz, zt_start.cumsum(), np.nan)
    for col in ['_brk', '_brk_low', '_brk_open', '_brk1_open', '_brk1_close']:
        E[col] = E.groupby('_zg')[col].bfill()
    E['_run_min_low'] = E.groupby('_zg')['low'].transform('min')
    E['_run_min_low2'] = E['_run_min_low']
    mask = run_end & E['_brk1_open'].notna()
    E.loc[mask, '_run_min_low2'] = E.loc[mask, ['_run_min_low', '_brk1_close']].min(axis=1)
    zt = E[E['is_zt'] & E['in_win']].copy()
    trade_win = (zt['date'] >= '2019-01-01') & (zt['date'] <= '2026-08-21')
    T = zt[trade_win].copy()
    T['is_is'] = T['date'] <= '2023-12-31'
    T['is_oos'] = T['date'] >= '2024-01-01'
    a1 = T[T['board_type'] != '一字'].copy()
    a1_buy = a1['close']
    a2 = T[T['next_open'].notna() & (T['next_board_type'] != '一字')].copy()
    a2_buy = a2['next_open']
    a3 = T[T['_brk_open'].notna()].copy()
    dip = a3['_brk_low'] <= a3['_brk_open'] * 0.97
    a3_buy = np.where(dip, a3['_brk_open'] * 0.97, a3['_brk_open'])
    sells = {}
    sells['A1'] = {'next_open': a1['next_open'], 'next_close': a1['next_close'],
                   'break_next': a1['_brk1_open'],
                   'mae_next': (a1['next_low'] - a1_buy) / a1_buy,
                   'mae_break': (a1['_run_min_low2'] - a1_buy) / a1_buy,
                   'buy': a1_buy, 'buy_fee': BUY_FEE_A1}
    sells['A2'] = {'next_open': a2['next2_open'], 'next_close': a2['next2_close'],
                   'break_next': a2['_brk1_open'],
                   'mae_next': (np.minimum(a2['next_low'].fillna(np.inf), a2['next2_low'].fillna(np.inf)) - a2_buy) / a2_buy,
                   'mae_break': (a2['_run_min_low2'] - a2_buy) / a2_buy,
                   'buy': a2_buy, 'buy_fee': BUY_FEE_A2A3}
    sells['A3'] = {'next_open': a3['_brk1_open'], 'next_close': a3['_brk1_close'],
                   'break_next': a3['next2_open'],
                   'mae_next': (a3['_brk_low'] - a3_buy) / a3_buy,
                   'mae_break': (a3['_run_min_low2'] - a3_buy) / a3_buy,
                   'buy': pd.Series(a3_buy, index=a3.index), 'buy_fee': BUY_FEE_A2A3}
    frames = {'A1': a1, 'A2': a2, 'A3': a3}
    buys = {'A1': a1_buy, 'A2': a2_buy, 'A3': pd.Series(a3_buy, index=a3.index)}
    buy_fees = {'A1': BUY_FEE_A1, 'A2': BUY_FEE_A2A3, 'A3': BUY_FEE_A2A3}
    return T, frames, buys, buy_fees, sells


def split_main(board_split, out=None):
    """E3：按板块拆分（all/10cm/20cm）重跑三打法网格 + 最优组合 IS/OOS。
    只读复用 tmp/v1 缓存；all 用于正确性门对照（复现 data/bt_tactic_lianban.json 原值）。
    out 必须指定（写入 tmp/e3/，禁覆盖 data/bt_tactic_lianban.json）。"""
    print('load pkl (split=%s) ...' % board_split)
    E = _load_events(board_split)
    T, frames, buys, buy_fees, sells = _build_trades(E)
    # 三打法 × 三卖点网格（一进二，lbc==1）
    grid = []
    for pb in ['A1', 'A2', 'A3']:
        df = frames[pb]
        df1 = df[df['lbc'] == 1]
        buy = buys[pb].loc[df1.index].values
        bf = buy_fees[pb]
        for sp in ['next_open', 'next_close', 'break_next']:
            sp_col = sells[pb][sp].loc[df1.index]
            for mask, lab in [(df1['is_is'].values, 'IS'), (df1['is_oos'].values, 'OOS'), (np.ones(len(df1), bool), 'ALL')]:
                idx = mask & sp_col.notna().values
                r = np.array([ret(buy[i], sp_col.values[i], bf) for i in np.where(idx)[0]])
                st = stat(r)
                mae_key = 'mae_next' if sp in ('next_open', 'next_close') else 'mae_break'
                mae_arr = sells[pb][mae_key].loc[df1.index].values[idx]
                st['mae'] = float(np.nanmean(mae_arr)) if len(mae_arr) else None
                st['period'] = lab
                st['playback'] = pb
                st['sell'] = sp
                grid.append(st)
    # 最优组合: A1 + 缩量(量比<1) + 非一字 + 卖次日开盘（V1 §5 推荐卡）
    c1 = frames['A1'][(frames['A1']['lbc'] == 1) & (frames['A1']['amount_ratio'] < 1.0)]
    c_buy = c1['close'].values
    c_sell = sells['A1']['next_open'].loc[c1.index]
    combo = {}
    for mask, lab in [(c1['is_is'].values, 'IS'), (c1['is_oos'].values, 'OOS'), (np.ones(len(c1), bool), 'ALL')]:
        idx = mask & c_sell.notna().values
        r = np.array([ret(c_buy[i], c_sell.values[i], BUY_FEE_A1) for i in np.where(idx)[0]])
        st = stat(r)
        mae_arr = sells['A1']['mae_next'].loc[c1.index].values[idx]
        st['mae'] = float(np.nanmean(mae_arr)) if len(mae_arr) else None
        combo[lab] = st
    # 按年（A1 + 缩量 + next_open）
    c_buy_all = c1['close'].values
    c_sell_all = c_sell.values
    combo_year = {}
    for y, sub in c1.groupby(c1['year']):
        pos = c1.index.get_indexer(sub.index)   # sub 行在 c1 中的位置（E3 修正：避免子组内 0..n-1 与全量数组错位）
        buy_sub = c_buy_all[pos]
        sell_sub = c_sell_all[pos]
        m = ~np.isnan(sell_sub)
        if not m.any():
            continue
        r = np.array([ret(buy_sub[i], sell_sub[i], BUY_FEE_A1) for i in np.where(m)[0]])
        combo_year[str(y)] = {'n': int(len(r)), 'mean_ret': float(np.nanmean(r)),
                              'winA': float((r > 0).mean())}
    result = {
        'meta': {'task': 'E3 20cm 拆分回测', 'board_split': board_split,
                 'method': 'read-only tmp/v1 events.pkl + kline_day.pkl (复用缓存, 禁重建)',
                 'window': '2019-01-01~2026-08-21 (trade_win)', 'cost': '同 V1 (佣金0.025%x2+印花0.05%卖+滑点0.2%/笔+A1排队0.1%)'},
        'grid': grid,
        'combo': combo,
        'combo_year': combo_year,
    }
    if out:
        with open(out, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=1)
        print('wrote %s' % out)
    print('\n--- split=%s 网格 (一进二, 扣费后) ---' % board_split)
    for g in grid:
        n = g['n']
        tag = '不可信' if n < MIN_N else ''
        print("%s/%-11s %s n=%-7d winA=%s mean=%s%% %s" % (
            g['playback'], g['sell'], g['period'], n,
            '-' if g['winA'] is None else round(g['winA'], 4),
            '-' if g['mean_ret'] is None else round(g['mean_ret'] * 100, 3), tag))
    print('--- 最优组合 (A1+缩量+非一字+next_open) ---')
    for lab in ['IS', 'OOS', 'ALL']:
        st = combo.get(lab, {})
        print('%s n=%s winA=%s mean=%.4f%% pf=%s' % (lab, st.get('n'), st.get('winA'),
              (st.get('mean_ret') or 0) * 100, st.get('pf')))
    return result


if __name__ == '__main__':
    if '--board-split' in sys.argv:
        i = sys.argv.index('--board-split')
        bs = sys.argv[i + 1] if i + 1 < len(sys.argv) else 'all'
        out = None
        if '--out' in sys.argv:
            j = sys.argv.index('--out')
            out = sys.argv[j + 1]
        split_main(bs, out)
    else:
        main()
