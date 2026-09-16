import json, sqlite3, os, re
from datetime import datetime
db=r"C:\Users\26838\A股模拟盘\data\market.db"
qr=r"C:\Users\26838\A股模拟盘\data\quality_report.json"
d=json.loads(open(qr,encoding='utf-8').read())
cands=d['qfq_jump_candidates']['candidates']
# We need to understand 2025-08-13->14 jumps: these are not suspensions (gap_code=0), need to check xdxr elsewhere?
# Let's look at actual kline prev_close vs open relation and volume pattern for suspicion
# Let's also check if 2025-08-14 is ex-div date: need to fetch amount/volume and see if it's genuine price jump or qfq artifact
# For qfq artifact, the entire history before ex-div is scaled, so jump at ex-div boundary will be visible as open/prev_close discontinuity that persists
# But our db is qfq already, so we cannot tell without raw hfq or unadjusted. Need heuristic:
# - Suspension seam: gap_code>0 or emp_between>0
# - Listing new: first date within 60 days of jump
# - True qfq pollution: otherwise large jump with no gap and not new listing -> likely ex-div scaling error (or real large move but exceeds limit)
# However real large moves (>limit) are possible via ST? Let's check limit_pct for these: 0.1, but jump 48% >> limit, so not real market move, must be artifact
# The fact gap_code=0 means not suspension seam, so for 2025-08-14 cases it's true qfq pollution

# Let's classify all 300
con=sqlite3.connect(f"file:{db}?mode=ro", uri=True)
first_dates=dict(con.execute("SELECT code, MIN(date) FROM kline WHERE period='day' GROUP BY code").fetchall())
# build suspension check cache
kline_dates_cache={}
def get_dates(code):
    if code not in kline_dates_cache:
        kline_dates_cache[code]=set(r[0] for r in con.execute("SELECT date FROM kline WHERE code=? AND period='day'", (code,)).fetchall())
    return kline_dates_cache[code]

emp=set(r[0] for r in con.execute("SELECT DISTINCT date FROM kline WHERE period='day'").fetchall())
emp_sorted=sorted(emp)

def has_gap(code, pd, dt):
    # check if any empirical trading day between pd and dt missing for this code
    dates=get_dates(code)
    between=[x for x in emp_sorted if x>pd and x<dt]
    if not between:
        return False, 0
    missing=sum(1 for x in between if x not in dates)
    return missing>0, missing

import collections
stats=collections.Counter()
details=[]
for c in cands:
    code=c['code']; pd=c['prev_date']; dt=c['date']; jump=c['jump_pct']
    # gap check
    is_gap, missing = has_gap(code, pd, dt)
    gap_code = False
    # also check large gap in code's own sequence (already via has_gap)
    # listing check
    first=first_dates.get(code,"9999-99-99")
    days_since=(datetime.strptime(dt,"%Y-%m-%d")-datetime.strptime(first,"%Y-%m-%d")).days if first!="9999-99-99" else 9999
    is_new = days_since < 60
    # classify
    if c['already_excluded']:
        cat="already_excluded"
    elif is_new:
        cat="listing_new_or_ipo"
    elif is_gap:
        cat="suspension_seam"
    else:
        # check if it's near ex-div season (April-Aug is dividend season) + not gap
        # We'll call remaining large jumps as true_qfq_pollution (likely mis-split)
        # To separate false positive (real market halt reopening with large jump but we missed gap due to emp incomplete?) keep suspension_seam
        cat="true_qfq_pollution"
    stats[cat]+=1
    details.append((c, cat, missing, days_since))

print(stats)
print("\n--- examples per cat ---")
for cat in stats:
    print("\n==", cat)
    for c, cc, miss, ds in [x for x in details if x[1]==cat][:5]:
        print(c['code'], c['prev_date'], "->", c['date'], f"jump {c['jump_pct']:.1f}%", f"sev {c['severity']}", f"missing {miss}", f"days_since_first {ds}")

# For remaining true_qfq_pollution, let's further split by xdxr: if we had xdxr table we could confirm ex-div, but we don't. Use dividend season heuristic + jump direction
# Let's also check if jump is exactly around -50% or +100% etc (split patterns)
import math
# count by magnitude
mag=collections.Counter()
for c, cat, _, _ in details:
    if cat=="true_qfq_pollution":
        a=abs(c['jump_pct'])
        bucket=">100" if a>100 else "50-100" if a>50 else "30-50" if a>30 else "20-30" if a>20 else "<20"
        mag[bucket]+=1
print("\nmag for true_qfq", mag)

# also check 002768 2025-08-14 vs 2025-08-13: volume not zero, so not suspension
# Let's verify for suspension_seam candidates they actually have gaps
print("\n--- suspension_seam sample verification ---")
for c, cat, miss, ds in [x for x in details if x[1]=="suspension_seam"][:5]:
    print(c, "miss", miss)

con.close()
