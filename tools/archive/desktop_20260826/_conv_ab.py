import json
b=json.load(open(r'C:\Users\26838\A股模拟盘\data\bt_conv_before.json',encoding='utf-8'))
a=json.load(open(r'C:\Users\26838\A股模拟盘\data\bt_conv_after.json',encoding='utf-8'))
# Compute per-strategy averages
for strat in ['score','board']:
    br=[r for r in b['runs'] if r['strategy']==strat]
    ar=[r for r in a['runs'] if r['strategy']==strat]
    avg_b=sum(r['total_return'] for r in br)/len(br)
    avg_a=sum(r['total_return'] for r in ar)/len(ar)
    print(f"{strat} avg ret before {avg_b:+.4f} after {avg_a:+.4f} delta {avg_a-avg_b:+.4f}")
    print(f"  maxdd before {[r['max_drawdown'] for r in br]}")
    print(f"  maxdd after  {[r['max_drawdown'] for r in ar]}")
# isolated effect: need ab files if exist
import os
for f in os.listdir(r"C:\Users\26838\A股模拟盘\data"):
    if "conv" in f or "ab" in f.lower():
        print(f)
