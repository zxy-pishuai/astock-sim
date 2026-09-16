import json
b=json.load(open(r'C:\Users\26838\A股模拟盘\data\bt_conv_before.json',encoding='utf-8'))
a=json.load(open(r'C:\Users\26838\A股模拟盘\data\bt_conv_after.json',encoding='utf-8'))
tags=["2019-20","2021-22","2023-24","近1年"]
for strat in ['score','board']:
    print("="*70)
    print(strat)
    for widx in range(4):
        br=[r for r in b['runs'] if r['strategy']==strat and r['widx']==widx][0]
        ar=[r for r in a['runs'] if r['strategy']==strat and r['widx']==widx][0]
        drett=ar['total_return']-br['total_return']
        ddd=ar['max_drawdown']-br['max_drawdown']
        print(f"  {tags[widx]:6s} ret {br['total_return']:+.4f}->{ar['total_return']:+.4f} Δ{drett:+.4f} ({drett*100:+.1f}pp)  dd {br['max_drawdown']:+.4f}->{ar['max_drawdown']:+.4f} Δ{ddd:+.4f}  sharpe {br['sharpe']:+.3f}->{ar['sharpe']:+.3f}  trades {br['trade_count']}->{ar['trade_count']} Δ{ar['trade_count']-br['trade_count']:+d}")
