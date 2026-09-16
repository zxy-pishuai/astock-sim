import sys, time
sys.path.insert(0, r"C:\Users\26838\A股模拟盘")
from app import datafeed as df
from app import config as C

codes=['600000','000001','300001','600519','000858']
# Clear caches to force actual load
df._mem_kline.clear()
for days in [250, 270]:
    df._mem_kline.clear()
    start=time.time()
    for c in codes:
        kl=df.fetch_kline(c, 'day', days)
        print(f" {c} days={days} got {len(kl)} elapsed {time.time()-start:.4f}")
    print(f"total for {days}: {time.time()-start:.4f}s")
    # second call should be cached
    start2=time.time()
    for c in codes:
        kl=df.fetch_kline(c, 'day', days)
    print(f" cached for {days}: {time.time()-start2:.6f}s")

# Also test _fetch_daily with force=True (network path)
print("\n--- network fallback estimate ---")
# Estimate bytes: extra 20 bars per stock per fetch = 20 * ~50 bytes JSON ~1KB per stock
# For 400 stocks scan: 400 * 20 bars * ~60 bytes ~ 480KB extra per scan
print("Extra data per fetch_kline: 20 bars * ~60 bytes/bar = ~1.2KB per stock")
print("For 130 scored candidates: ~156KB extra per scan cycle")
print("For 8 board candidates: ~9.6KB extra")
print("Total extra per 5s round: negligible")
print("Cache hit: 270 requested but only 250 in cache -> would need re-fetch for extra 20 bars")

# Check actual cache behavior: if we request 270 but cache has 250, what happens?
df._mem_kline.clear()
kl250=df.fetch_kline('600000', 'day', 250)
print(f"\nAfter 250 fetch, cache has {len(df._mem_kline)} entries")
for k,v in df._mem_kline.items():
    print(f"  {k}: len={len(v[1])}")
kl270=df.fetch_kline('600000', 'day', 270)
print(f"After 270 fetch (same code): {len(kl270)} returned, cache: {[(k, len(v[1])) for k,v in df._mem_kline.items()]}")
# The cache key includes period but not days, so 270 would return cached 250? Check _fetch_daily
