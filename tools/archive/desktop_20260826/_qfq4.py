import json, sqlite3, os, collections
from datetime import datetime
db=r"C:\Users\26838\A股模拟盘\data\market.db"
qr=r"C:\Users\26838\A股模拟盘\data\quality_report.json"
d=json.loads(open(qr,encoding='utf-8').read())
cands=d['qfq_jump_candidates']['candidates']
# Need to check which jumps are actually ex-div vs real large move that just exceeds limit but is suspension seam already handled
# For true_qfq_pollution with |jump| < 15% and severity medium, could be false positive: real market gap up/down that exceeds limit by 0.5% but not 2x -> sentinel rule is |jump|>limit+0.5%
# Real market cannot exceed limit for qfq-adjusted? Actually limit is based on prev_close, so max jump is limit. So any candidate exceeding limit+0.5% is indeed anomalous, but small excess (e.g., 10.6% vs 10% limit) could be rounding error in limit_pct due to ST/price limit rule variation?
# But for  <20 bucket (199 cases) with jump 11-14% vs limit 10%, need to check if it's actually suspension seam missed due to emp incomplete for 2026 dates?
# Let's examine: for <20 bucket, are they all recently 2025-2026 with emp missing?

con=sqlite3.connect(f"file:{db}?mode=ro", uri=True)
emp=set(r[0] for r in con.execute("SELECT DISTINCT date FROM kline WHERE period='day'").fetchall())
emp_sorted=sorted(emp)
first_dates=dict(con.execute("SELECT code, MIN(date) FROM kline WHERE period='day' GROUP BY code").fetchall())
kline_cache={}
def get_dates(code):
    if code not in kline_cache:
        kline_cache[code]=set(r[0] for r in con.execute("SELECT date FROM kline WHERE code=? AND period='day'",(code,)).fetchall())
    return kline_cache[code]
def has_gap(code,pd,dt):
    dates=get_dates(code)
    between=[x for x in emp_sorted if x>pd and x<dt]
    missing=sum(1 for x in between if x not in dates)
    return missing>0, missing

# analyze <20 bucket
true_cands=[c for c in cands if not c['already_excluded']]
# classify again but keep details
for c in true_cands[:20]:
    code=c['code']; pd=c['prev_date']; dt=c['date']
    # check actual limit: sentinel uses limit_pct from rule, but actual limit depends on code/name (ST,创业板 etc)
    # For 30% board etc, limit 30, but sentinel may use 10? Let's verify
    # Our sentinel rule uses limit_pct from config? Might be simplified
    # Let's compute real limit via engine
    import sys
    sys.path.insert(0, r"C:\Users\26838\A股模拟盘")
    from app import engine as eng
    # need name
    name=c.get('name','')
    # For now just print
    is_gap, miss = has_gap(code,pd,dt)
    print(f"{code} {pd}->{dt} jump {c['jump_pct']:.2f}% limit {c['limit_pct']:.1%} sev {c['severity']} gap {is_gap} miss {miss} prev {c['prev_close']:.2f} open {c['open']:.2f}")

# Check how many true_qfq <20 have jump just barely over limit+0.5%
barely=sum(1 for c in true_cands if abs(c['jump_pct'])-c['limit_pct']*100 < 2)
print("\n barely over limit (<2pp):", barely)
# Check 2025-2026 concentration
from collections import Counter
cnt=Counter()
for c in true_cands:
    if abs(c['jump_pct'])<20:
        cnt[c['date'][:7]]+=1
print("\n <20 by month", sorted(cnt.items())[-20:])

con.close()
