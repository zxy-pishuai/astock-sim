# -*- coding: utf-8 -*-
"""实测 mootdx 通达信协议：速度 + 可用性"""
import time

try:
    from mootdx.quotes import Quotes
    print("mootdx Quotes 导入 OK")
except Exception as e:
    print("导入失败:", e)
    raise SystemExit

# 1) 连接（默认自动测速选最快服务器）
t0 = time.time()
try:
    client = Quotes.factory(market="std")
    dt = time.time() - t0
    print("连接通达信: %.2fs" % dt)
except Exception as e:
    print("连接失败:", str(e)[:120])
    raise SystemExit

# 2) 单只实时行情
try:
    t0 = time.time()
    q = client.quotes(symbol="600519")
    dt = time.time() - t0
    print("单只实时行情: %.3fs" % dt)
    print("  返回:", q.to_dict() if hasattr(q, "to_dict") else q)
except Exception as e:
    print("实时行情失败:", str(e)[:100])

# 3) 批量行情（多只）
try:
    t0 = time.time()
    qs = client.quotes(symbol=["600519", "000858", "601318"])
    dt = time.time() - t0
    print("批量3只行情: %.3fs" % dt)
except Exception as e:
    print("批量行情失败:", str(e)[:100])

# 4) 日K
try:
    t0 = time.time()
    k = client.bars(symbol="600519", frequency=9, offset=10)  # 9=日线
    dt = time.time() - t0
    print("日K(10根): %.3fs" % dt)
    print("  列:", list(k.columns) if hasattr(k, "columns") else type(k))
except Exception as e:
    print("日K失败:", str(e)[:100])

client.close()
