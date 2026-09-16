# -*- coding: utf-8 -*-
"""★ Phase25: 汇总 8 配置×4 窗口 → data/bt_ml_weight.json + 判定表"""
import sys, os, json
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
ROOT = r'C:/Users/26838/A股模拟盘'
sys.path.insert(0, ROOT)
from app import config as C

PARTS = os.path.join(C.DATA_DIR, 'bt_ml_weight_parts')
BASE = json.load(open(os.path.join(C.DATA_DIR, 'bt_clean_results.json'), encoding='utf-8'))['multi_window']
WINS = ['2019-20', '2021-22', '2023-24', '2025-08~2026-08(牛市)']

out = {'meta': {
    'pool': 'bt_pool.json top500 (2026-08-21 成交额降序; engine 内滤 DATA_EXCLUDE_CODES)',
    'params': {'score': {'buy_threshold': 25, 'max_positions': 3, 'position_pct': 0.3, 'slippage': 0.001},
               'board': {'buy_threshold': 40, 'max_positions': 2, 'position_pct': 0.25, 'slippage': 0.001}},
    'capital': 100000, 'factor': 'ml_pred score 加权: bonus=(score-0.5)*2*weight*K, K=8 (config.ML_RANK_WEIGHT)',
    'alignment': 'weight=0 与 data/bt_clean_results.json 基线精确复现(fetch_quotes 置空+worker参数)',
}, 'runs': {}, 'judgement': {}}

for strat in ['score', 'board']:
    base_w = BASE[strat]
    for w in [0.0, 0.5, 1.0, 1.5]:
        key = '%s_w%.1f' % (strat, w)
        p = os.path.join(PARTS, key + '.json')
        if not os.path.exists(p):
            print('MISSING', key); continue
        d = json.load(open(p, encoding='utf-8'))['windows']
        rows = []
        for i in range(4):
            g = d[str(i)]
            if 'error' in g:
                rows.append({'window': WINS[i], 'error': g['error']}); continue
            b = base_w[i]
            rows.append({
                'window': WINS[i], 'total_return': round(g['total_return'], 4),
                'max_drawdown': round(g['max_drawdown'], 4), 'trade_count': g['trade_count'],
                'win_rate': g.get('win_rate'), 'sharpe': g.get('sharpe'),
                'baseline_return': b['total_return'], 'baseline_trades': b['trade_count'],
                'delta_pp': round((g['total_return'] - b['total_return']) * 100, 2),
                'trade_change_pct': round((g['trade_count'] - b['trade_count']) / max(b['trade_count'], 1) * 100, 1),
                'elapsed_s': g.get('elapsed'),
            })
        out['runs'][key] = {'strategy': strat, 'weight': w, 'windows': rows}

# 判定（事先写死规则）：≥3/4 窗口 Δ≥-1pp，且牛市窗口 Δ≥-2pp，且两策略交易数变化 <15%
for strat in ['score', 'board']:
    for w in [0.5, 1.0, 1.5]:
        key = '%s_w%.1f' % (strat, w)
        rows = out['runs'].get(key, {}).get('windows', [])
        if len(rows) != 4 or any('error' in r for r in rows):
            continue
        ok3 = sum(1 for r in rows if r['delta_pp'] >= -1.0) >= 3
        bull = rows[3]['delta_pp']
        okbull = bull >= -2.0
        # 两策略交易结构：本策略 vs 各自基线（另一策略在同 weight 下若未受因子影响则为 0 变化）
        tc_ok = abs(rows[3 - 1]['trade_change_pct']) < 15 and all(abs(r['trade_change_pct']) < 15 for r in rows)
        passed = ok3 and okbull and tc_ok
        out['judgement'][key] = {
            'improved_or_flat_windows': sum(1 for r in rows if r['delta_pp'] >= -1.0),
            'cond_3of4_deltageq-1pp': ok3, 'bull_delta_pp': bull, 'cond_bull_ge-2pp': okbull,
            'cond_trades_lt15pct': tc_ok, 'pass': passed}

tmp = os.path.join(C.DATA_DIR, 'bt_ml_weight.json.tmp')
with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
    json.dump(out, f, ensure_ascii=False, indent=1)
os.replace(tmp, os.path.join(C.DATA_DIR, 'bt_ml_weight.json'))
print('written bt_ml_weight.json')
for k, j in out['judgement'].items():
    print(k, j)
