# -*- coding: utf-8 -*-
"""腾讯 fqkline 全市场拉取（说明书 §4 指定源）
- 区间 2025-08-01 ~ 2026-09-10，320 根 qfq
- 返回 [date, open, close, high, low, volume]（无 amount，F1 已证）
- 断点续跑：tmp/qlw/fetch_done.json 记录已完成 code
- 落盘：tmp/qlw/kline_tencent.pkl（code,date,open,close,high,low,volume）
"""
import json
import os
import sys
import time
import urllib.request

import pandas as pd

sys.stdout.reconfigure(encoding='utf-8')

ROOT = r'C:/Users/26838/A股模拟盘'
START = '2025-08-01'
END = '2026-09-10'
COUNT = 320
INTERVAL = 0.22

# 双源轮换：镜像优先（主源 web.ifzq 已被风控 501），失败自动切换
API_BASES = [
    'https://proxy.finance.qq.com/ifzqgtimg/appstock/app/fqkline/get?param=%s,day,%s,%s,%d,qfq',
    'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=%s,day,%s,%s,%d,qfq',
]

uni = json.load(open(ROOT + '/tmp/qlw/universe_meta.json', encoding='utf-8'))
uni_df = pd.DataFrame(uni)


def symbol_of(code):
    if code.startswith(('60', '68')):
        return 'sh' + code
    if code.startswith(('00', '30')):
        return 'sz' + code
    return None


uni_df['symbol'] = uni_df['code'].map(symbol_of)
# 剔北交所/ST（board 判定）
uni_df['is_st'] = uni_df['name'].str.upper().str.contains('ST', na=False)
keep = uni_df[uni_df['symbol'].notna() & (~uni_df['is_st'])]
codes = keep['code'].tolist()
print('待拉票数:', len(codes))

done_path = ROOT + '/tmp/qlw/fetch_done.json'
done = set()
if os.path.exists(done_path):
    done = set(json.load(open(done_path, encoding='utf-8')))

out_path = ROOT + '/tmp/qlw/kline_tencent.pkl'
records = []
if os.path.exists(out_path):
    prev = pd.read_pickle(out_path)
    records = prev.to_dict('records')

consec_fail = 0
ok = 0
t0 = time.time()
for i, code in enumerate(codes):
    if code in done:
        continue
    sym = keep[keep['code'] == code]['symbol'].iloc[0]
    got = False
    for attempt in range(3):
        # 每轮换源
        base = API_BASES[attempt % len(API_BASES)]
        url = base % (sym, START, END, COUNT)
        try:
            req = urllib.request.Request(url, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
                'Referer': 'https://gu.qq.com/'})
            with urllib.request.urlopen(req, timeout=15) as r:
                txt = r.read().decode('utf-8')
            d = json.loads(txt)
            node = d.get('data', {}).get(sym, {})
            kl = node.get('qfqday') or node.get('day') or []
            if not kl:
                raise ValueError('empty kline')
            for row in kl:
                if len(row) >= 6:
                    records.append({
                        'code': code,
                        'date': row[0],
                        'open': float(row[1]),
                        'close': float(row[2]),
                        'high': float(row[3]),
                        'low': float(row[4]),
                        'volume': float(row[5]),
                    })
            ok += 1
            got = True
            break
        except Exception as e:
            if attempt < 2:
                time.sleep(1 + attempt * 2)
            else:
                consec_fail += 1
                print('[%d/%d] %s fail: %s' % (i + 1, len(codes), code, str(e)[:120]), flush=True)
                if consec_fail >= 5:
                    print('连续失败 5 次，退避 30s', flush=True)
                    time.sleep(30)
                    consec_fail = 0
    if got:
        consec_fail = 0
        done.add(code)
    if (i + 1) % 100 == 0:
        pd.DataFrame(records).to_pickle(out_path)
        json.dump(sorted(done), open(done_path, 'w', encoding='utf-8'))
        el = time.time() - t0
        print('[%d/%d] ok=%d done=%d 用时=%.0fs 预计剩余=%.0fs'
              % (i + 1, len(codes), ok, len(done), el, el / max(i + 1, 1) * (len(codes) - i - 1)), flush=True)
    time.sleep(INTERVAL)

pd.DataFrame(records).to_pickle(out_path)
json.dump(sorted(done), open(done_path, 'w', encoding='utf-8'))
df = pd.DataFrame(records)
print('完成: 行数=%d 票数=%d 区间=%s~%s' % (len(df), df['code'].nunique(), df['date'].min(), df['date'].max()))
