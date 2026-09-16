# -*- coding: utf-8 -*-
"""测 tdx 加速层"""
import sys, time
sys.path.insert(0, r"C:\Users\26838\A股模拟盘")
from app import tdx

ok = True
def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        ok = False

print("mootdx 可用:", tdx.available())

# 1) 批量行情
t0 = time.time()
q = tdx.fetch_quotes_fast(["600519", "000858", "601318", "600036", "000333"])
dt = time.time() - t0
print("批量5只: %.3fs, %d 条" % (dt, len(q or {})))
if q:
    s = q.get("600519") or {}
    print("  600519: 价=%.2f 涨跌=%.2f%% 买一=%.2f 卖一=%.2f 外盘=%.0f 内盘=%.0f" % (
        s.get("price"), s.get("pct_chg"), s.get("bid1_price"),
        s.get("ask1_price"), s.get("outer_vol"), s.get("inner_vol")))
    check("盘口字段齐全", s.get("bid1_price") and s.get("ask1_price"))

# 2) 日K
t0 = time.time()
k = tdx.fetch_kline_fast("600519", "day", 60)
dt = time.time() - t0
print("日K60根: %.3fs, %d 根" % (dt, len(k or [])))
if k:
    print("  首末:", k[0]["date"], "~", k[-1]["date"])
    check("日K格式正确", "close" in k[0] and k[0]["close"] > 0)

# 3) 5分钟K
t0 = time.time()
m5 = tdx.fetch_kline_fast("600519", "min5", 48)
dt = time.time() - t0
print("5分钟K48根: %.3fs, %d 根" % (dt, len(m5 or [])))
check("5分钟K可用", (m5 or []) and "close" in m5[0])

# 4) 稳定性：连续调用（连接复用）
t0 = time.time()
for _ in range(5):
    tdx.fetch_quotes_fast(["600519"])
print("连续5次批量: %.3fs (连接复用)" % (time.time() - t0))
check("连接复用", time.time() - t0 < 2)

print("\n" + ("ALL PASS" if ok else "SOME FAILED"))
sys.exit(0 if ok else 1)
