import sys
sys.path.insert(0, r"C:\Users\26838\A股模拟盘")
from app import volprice, indicators as ind

# Simulate volprice with 250 vs 270 klines
# The bug: detect_pullback_ma250 needs i>=250, but _score_one gives only 250 klines, so i=249, never triggers
# With 270, i=269, would trigger for stocks with >=251 bars

# Check scoring path
from app import scoring as sc
# Mock: create 270 klines with MA250 pullback pattern
import random
random.seed(42)
# Create closes that have pullback pattern
closes = [10 + i*0.02 + random.uniform(-0.1,0.1) for i in range(270)]
volumes = [1000000 + random.uniform(-200000,200000) for _ in range(270)]
lows = [c*0.99 for c in closes]
klines = [{"date": f"2020-01-{i:02d}", "open": c, "high": c*1.01, "low": l, "close": c, "volume": v, "amount": v*c} for i,(c,l,v) in enumerate(zip(closes, lows, volumes))]
# Make last bar pullback to MA250
# Compute MA250 for last bar
ma250_series = ind.sma(closes, 250)
print(f"MA250 last: {ma250_series[-1]:.4f}, close last: {closes[-1]:.4f}, dev: {(closes[-1]-ma250_series[-1])/ma250_series[-1]:.4f}")

result = volprice.detect_pullback_ma250(closes, lows, volumes, 269)
print(f"detect with 270 klines (i=269): {result}")
result250 = volprice.detect_pullback_ma250(closes[:250], lows[:250], volumes[:250], 249)
print(f"detect with 250 klines (i=249): {result250} - should be False due to i<250")

# Now test via score_stock path with 250 vs 270
score250, sigs250 = sc.score_stock(klines[:250])
print(f"\nscore_stock with 250 klines: {score250}, signals: {sigs250}")
score270, sigs270 = sc.score_stock(klines)
print(f"score_stock with 270 klines: {score270}, signals: {sigs270}")
print(f"Delta: {score270-score250}, new signals: {set(sigs270)-set(sigs250)}")

# Check if volprice signal ever fires with current 250 limit
# The comment says "实盘不触发" - confirm
print("\n--- Volprice signal frequency estimate ---")
# Sample from real data
from app import datafeed as df
codes=['600000','600519','000001','300001','002594']
for code in codes:
    kl=df.fetch_kline(code, 'day', 270)
    if len(kl) < 270:
        print(f"{code}: only {len(kl)} klines, skip")
        continue
    closes_r=[k['close'] for k in kl]
    volumes_r=[k['volume'] for k in kl]
    lows_r=[k['low'] for k in kl]
    hit250 = volprice.detect_pullback_ma250(closes_r[:250], lows_r[:250], volumes_r[:250], 249)
    hit270 = volprice.detect_pullback_ma250(closes_r, lows_r, volumes_r, 269)
    print(f"{code}: 250-> {hit250}, 270-> {hit270}")
