# -*- coding: utf-8 -*-
"""测 mootdx 批量/全市场快照"""
import time
from mootdx.quotes import Quotes

client = Quotes.factory(market="std")

# 1) 批量 100 只行情（模拟扫描候选池）
codes = []
for i in range(100):
    # 生成 100 个合法代码（600xxx 部分可能不存在，但测批量性能）
    codes.append("60%04d" % (i + 1000) if i < 50 else "00%04d" % (i + 1000))
t0 = time.time()
try:
    q = client.quotes(symbol=codes)
    dt = time.time() - t0
    print("批量 %d 只: %.3fs" % (len(codes), dt))
except Exception as e:
    print("批量失败:", str(e)[:80])

# 2) 用真实存在的代码测批量（沪深各 50 只真实股）
real_codes = ["600519", "600036", "600000", "601318", "600030", "601398", "600028", "600050",
              "600690", "600887", "601888", "600309", "600585", "600031", "601166", "600104",
              "600276", "600900", "601857", "600089"] + \
             ["000858", "000001", "000002", "000333", "000651", "000725", "000063", "000100",
              "000568", "000776", "002415", "002594", "002475", "002230", "002352", "300750",
              "300059", "300015", "002714", "300124"]
t0 = time.time()
q2 = client.quotes(symbol=real_codes)
dt = time.time() - t0
print("批量 %d 只(真实股): %.3fs, %d 行" % (len(real_codes), dt, len(q2)))

# 3) 分钟K（打板用 5 分钟）
t0 = time.time()
m5 = client.bars(symbol="600519", frequency=0, offset=48)  # 0=5分钟
dt = time.time() - t0
print("5分钟K(48根): %.3fs" % dt)

client.close()
