import json, collections, sqlite3, os
p=r"C:\Users\26838\A股模拟盘\data\quality_report.json"
d=json.loads(open(p,encoding='utf-8').read())
cands=d['qfq_jump_candidates']['candidates']
print("total", len(cands), "high", sum(1 for c in cands if c['severity']=='high'), "already_excl", sum(1 for c in cands if c['already_excluded']))
# show high
print("\n--- HIGH ---")
for c in cands[:15]:
    print(c)
print("\n--- sample low ---")
for c in [x for x in cands if x['severity']!='high'][:10]:
    print(c)
# stats by year
from collections import Counter
cnt=Counter()
for c in cands:
    y=c['date'][:4]
    cnt[y]+=1
print("\nby year", sorted(cnt.items()))
# check xdxr table exists?
db=r"C:\Users\26838\A股模拟盘\data\market.db"
try:
    con=sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows=con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    print("tables", [r[0] for r in rows][:30])
    # check xdxr
    try:
        r=con.execute("SELECT COUNT(*) FROM xdxr").fetchone()
        print("xdxr rows", r)
        print(con.execute("SELECT * FROM xdxr LIMIT 3").fetchall())
    except Exception as e:
        print("xdxr err", e)
    # check kline gaps for 002768
    r=con.execute("SELECT date,open,close,prev_close FROM kline WHERE code='002768' AND period='day' ORDER BY date DESC LIMIT 5").fetchall()
    print("002768 kline", r)
    # check listing date? try to find stock_info table
    try:
        print(con.execute("SELECT sql FROM sqlite_master WHERE name='xdxr'").fetchone())
    except: pass
    con.close()
except Exception as e:
    import traceback; traceback.print_exc()
