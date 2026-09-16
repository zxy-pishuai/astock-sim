import json, sqlite3, os, re
from datetime import datetime, timedelta
db=r"C:\Users\26838\A股模拟盘\data\market.db"
qr=r"C:\Users\26838\A股模拟盘\data\quality_report.json"
d=json.loads(open(qr,encoding='utf-8').read())
cands=d['qfq_jump_candidates']['candidates']
print("cands", len(cands))
# load listing dates from stock_list.json if exists
sl=r"C:\Users\26838\A股模拟盘\data\stock_list.json"
if os.path.exists(sl):
    j=json.loads(open(sl,encoding='utf-8').read())
    # guess format
    print("stock_list type", type(j), len(j) if isinstance(j,(list,dict)) else "?")
    if isinstance(j, dict):
        print(list(j.keys())[:10])
        if 'data' in j:
            print(j['data'][:2] if isinstance(j['data'], list) else str(j['data'])[:500])
    elif isinstance(j, list):
        print(j[:2])
# check trading_calendar
import sys
sys.path.insert(0, r"C:\Users\26838\A股模拟盘")
from app import trading_calendar as tcal
# check gaps for a few codes: need to know if jump date is after long gap (suspension)
con=sqlite3.connect(f"file:{db}?mode=ro", uri=True)
# build empirical trading days set
rows=con.execute("SELECT DISTINCT date FROM kline WHERE period='day' ORDER BY date").fetchall()
emp=set(r[0] for r in rows)
emp_sorted=sorted(emp)
print("emp days", len(emp_sorted), emp_sorted[:3], emp_sorted[-3:])
# check for each candidate: gap between prev_date and date
# Use kline existence for that code
def get_kline_dates(code):
    r=con.execute("SELECT date FROM kline WHERE code=? AND period='day' ORDER BY date", (code,)).fetchall()
    return [x[0] for x in r]

# sample 10 high not excluded
high_new=[c for c in cands if c['severity']=='high' and not c['already_excluded']][:10]
for c in high_new:
    code=c['code']; pd=c['prev_date']; dt=c['date']
    dates=get_kline_dates(code)
    # find gap
    # find index
    try:
        i1=dates.index(pd) if pd in dates else -1
        i2=dates.index(dt) if dt in dates else -1
        gap = i2 - i1 -1 if i1>=0 and i2>=0 else "N/A"
        # also check overall empirical gap
        # find empirical gap between pd and dt (number of trading days missing)
        # empirical trading days between pd+1 .. dt-1 that are in emp but not in code's series
        # simpler: count emp days >pd and <dt
        emp_between=sum(1 for x in emp_sorted if x>pd and x<dt)
        print(f"{code} {pd}->{dt} gap_code={gap} emp_between={emp_between} jump={c['jump_pct']:.1f}% limit={c['limit_pct']}")
        # check kline detail
        r=con.execute("SELECT date,open,high,low,close,volume FROM kline WHERE code=? AND date IN (?,?) ORDER BY date", (code, pd, dt)).fetchall()
        print(" ", r)
    except Exception as e:
        print("err", code, e)

# also check qfq_repair_failed list
qfq_failed=r"C:\Users\26838\A股模拟盘\data\qfq_repair_failed.json"
if os.path.exists(qfq_failed):
    print("\nqfq_failed", open(qfq_failed,encoding='utf-8').read()[:3000])

# check DATA_EXCLUDE_CODES
sys.path.insert(0, r"C:\Users\26838\A股模拟盘\app")
import config as C
print("\nEXCLUDE", C.DATA_EXCLUDE_CODES[:10], len(C.DATA_EXCLUDE_CODES))

# check for listing date: try to get from tdx or stock_list; fallback use first kline date per code
# Build first dates
first_dates={}
for r in con.execute("SELECT code, MIN(date) as d FROM kline WHERE period='day' GROUP BY code").fetchall():
    first_dates[r[0]]=r[1]
# sample codes: check listing freshness for jump dates
print("\n--- listing freshness for high_new ---")
for c in high_new[:5]:
    code=c['code']; dt=c['date']
    first=first_dates.get(code,"?")
    print(code, "first", first, "jump", dt, "days since first", (datetime.strptime(dt,"%Y-%m-%d")-datetime.strptime(first,"%Y-%m-%d")).days if first!="?" else "?")

con.close()
