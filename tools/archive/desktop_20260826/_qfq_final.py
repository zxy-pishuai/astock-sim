import json, sqlite3, os, collections
from datetime import datetime
db=r"C:\Users\26838\A股模拟盘\data\market.db"
qr=r"C:\Users\26838\A股模拟盘\data\quality_report.json"
d=json.loads(open(qr,encoding='utf-8').read())
cands=d['qfq_jump_candidates']['candidates']
# We need to decide triage categories: suspension_seam / true_qfq_pollution / listing_new_or_ipo / already_excluded + maybe false_positive
# For remaining 248 true_qfq_pollution, let's see if some are actually real market gaps that exceed limit due to being ST->non-ST or limit change not captured?
# Sentinel uses limit_pct per code type, but if code was ST then limit 5%, but jump 11% vs 5% limit would be high, but actually code may have changed ST status?
# Let's check ST status via name containing ST at jump date: we don't have historic name, just current name (qfq candidates name field is current)
# For now triage: 248 includes many small jumps 11-14% vs 10% limit with no gap. Need to verify if these are real ex-div or just limit rule mismatch.
# Let's look at <20 bucket candidates: check if name contains ST?
# Also check if code is创业板 (30xxxx) with limit 20% but sentinel shows 10%? That would cause false high for 11% jump.
# But sentinel's limit_pct for 300058 is 0.2 correct (20%), so not that.

# For <20 bucket, barely over limit 24 cases could be market noise: e.g., 10.6% vs 10% limit with no gap, maybe due to qfq scaling of 0.5% error accumulated?
# Let's examine those barely cases
barely=[c for c in cands if not c['already_excluded'] and abs(c['jump_pct'])-c['limit_pct']*100 < 2 and abs(c['jump_pct'])-c['limit_pct']*100 >0.5]
print("barely", len(barely))
for c in barely[:10]:
    print(c)

# Also need to check if any candidate coincides with known xdxr ex-div dates via external source? Without xdxr table we use alternative: check qfq_repair logic
# Let's try to fetch mootdx xdxr for a few codes to validate
try:
    from mootdx.affair import Affair
    af=Affair()
    for code in ['002768','002335','600262','300058'][:2]:
        try:
            df=af.fetch_xdxr(code)
            print("\nxdxr", code, df.head(10).to_string() if hasattr(df,'head') else df[:10])
        except Exception as e:
            print("xdxr fetch err", code, e)
except Exception as e:
    import traceback; traceback.print_exc()
