import sys
sys.path.insert(0, r"C:\Users\26838\A股模拟盘")
from app import datafeed as df
# Verify the bug: requesting more than cached returns truncated
df._mem_kline.clear()
# First fetch 250, then 270 without clearing -> should be bug
kl250 = df.fetch_kline('600519', 'day', 250)
print(f"250 fetch: {len(kl250)}, cache len {len(df._mem_kline[('600519','day')][1])}")
kl270 = df.fetch_kline('600519', 'day', 270)
print(f"270 fetch (cached 250): got {len(kl270)} - BUG? expected 270 got {len(kl270)}")
# Clear and fetch 270 first
df._mem_kline.clear()
kl270b = df.fetch_kline('600519', 'day', 270)
print(f"270 fetch fresh: got {len(kl270b)}")
# Then fetch 250 from that cache
kl250b = df.fetch_kline('600519', 'day', 250)
print(f"250 fetch from 270 cache: got {len(kl250b)} (should slice to 250: {len(kl250b)==250})")
# The bug is that _fetch_daily returns k[-days:] but only if cached hit; if cached has fewer than requested, it slices but doesn't fetch extra
# Let's check _fetch_daily logic
import inspect
print(inspect.getsource(df._fetch_daily))
