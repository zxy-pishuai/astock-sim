import json, os
base=r"C:\Users\26838\A股模拟盘\data\snapshot_baseline_parts"
for fn in ['snapshot_score.json','snapshot_board.json','live_score.json','live_board.json']:
    p=os.path.join(base,fn)
    try:
        d=json.loads(open(p,encoding='utf-8').read())
        tag=d.get('tag'); strat=d.get('strategy'); wins=d.get('windows',{})
        print(fn, tag, strat, 'windows', len(wins), 'keys', sorted(wins.keys(), key=lambda x: int(x)))
        for k in sorted(wins.keys(), key=lambda x: int(x)):
            v=wins[k]
            print(f"  win{k} {v['window']} ret {v['total_return']} dd {v['max_drawdown']} trades {v['trade_count']} curve {len(v.get('equity_curve',[]))}")
    except Exception as e:
        import traceback; print(fn, 'ERR', e); traceback.print_exc()

p2=r"C:\Users\26838\A股模拟盘\docs\operations.md"
try:
    print("\n--- ops head ---")
    print(open(p2,encoding='utf-8').read()[:3000])
except Exception as e:
    print("ops err", e)
