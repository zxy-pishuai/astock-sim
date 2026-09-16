# -*- coding: utf-8 -*-
import sqlite3, sys, urllib.request, json
sys.stdout.reconfigure(encoding='utf-8')
UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}
conn = sqlite3.connect('file:C:/Users/26838/A股模拟盘/data/market.db?mode=ro', uri=True)
for code, dd, w0, w1 in [('300750', '2022-09-29', '2022-09-01', '2022-10-31'),
                         ('002837', '2020-12-31', '2020-12-01', '2021-01-31')]:
    r = conn.execute("SELECT open,close FROM kline WHERE code=? AND period='day' AND date=?",
                     (code, dd)).fetchone()
    pref = 'sz'
    url = (f'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param='
           f'{pref}{code},day,{w0},{w1},100,qfq')
    req = urllib.request.Request(url, headers=UA)
    d = json.loads(urllib.request.urlopen(req, timeout=15).read().decode('utf-8', 'replace'))
    sd = (d.get('data') or {}).get(pref + code) or {}
    tv = None
    for e in sd.get('qfqday') or []:
        if e[0] == dd:
            tv = (float(e[1]), float(e[2]))
            break
    ok = r and tv and abs(r[0] - tv[0]) < 0.05
    print(f'{code} {dd}: 库内={tuple(round(x,2) for x in r) if r else None} '
          f'腾讯窗口={tuple(round(x,2) for x in tv) if tv else None} '
          f'{"✅一致(v3写对)" if ok else "❌不一致"}')
conn.close()