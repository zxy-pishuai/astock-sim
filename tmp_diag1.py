import json, sqlite3
from app import datafeed as df
lst = df.get_stock_list()
print('"'"'stock_list length'"'"', len(lst))
print('"'"'first 5'"'"', lst[:5])
conn = sqlite3.connect('"'"'file:data/market.db?mode=ro'"'"', uri=True)
codes_828 = [r[0] for r in conn.execute('"'"'SELECT code FROM kline WHERE period="day" AND date="2026-08-28"'"'"').fetchall()]
print('"'"'8/28 distinct codes'"'"', len(codes_828))
print('"'"'sample 8/28 codes'"'"', codes_828[:20])
first300 = set(c for c,_,_ in lst[:300])
in_first300 = sum(1 for c in codes_828 if c in first300)
print('"'"'of 8/28 codes, in first300'"'"', in_first300, '"'"'outside'"'"', len(codes_828)-in_first300)
missing = [c for c in list(first300)[:10] if c not in codes_828]
print('"'"'missing sample from first300'"'"', missing[:10])
for c in missing[:3]:
    rows = conn.execute('"'"'SELECT date, close FROM kline WHERE code=? AND period="day" ORDER BY date DESC LIMIT 5'"'"', [c]).fetchall()
    print(c, rows)
# Also check 8/27 count
cnt27 = conn.execute('"'"'SELECT COUNT(*) FROM kline WHERE period="day" AND date="2026-08-27"'"'"').fetchone()[0]
cnt28 = conn.execute('"'"'SELECT COUNT(*) FROM kline WHERE period="day" AND date="2026-08-28"'"'"').fetchone()[0]
print('"'"'cnt 8/27'"'"', cnt27, '"'"'cnt 8/28'"'"', cnt28)
# Check if 8/28 codes are maybe TDX succeeded subset?
conn.close()
