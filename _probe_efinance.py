# -*- coding: utf-8 -*-
"""实测 efinance 的速度与可用性"""
import time
try:
    import efinance as ef
    print("efinance 版本:", getattr(ef, "__version__", "?"))
except Exception as e:
    print("导入失败:", e)
    raise SystemExit

# 1) 全市场实时行情（核心需求：替代 fetch_quotes）
try:
    t0 = time.time()
    df = ef.stock.get_realtime_quotes()
    dt = time.time() - t0
    print("全市场实时行情: %.2fs, %d 行" % (dt, len(df)))
    if len(df):
        print("列:", list(df.columns)[:12])
        print("样例:", df.iloc[0].to_dict())
except Exception as e:
    print("实时行情失败:", str(e)[:120])

# 2) 单只日K
try:
    t0 = time.time()
    k = ef.stock.get_quote_history("600519")
    dt = time.time() - t0
    print("600519 日K: %.2fs, %d 行" % (dt, len(k)))
    if len(k):
        print("列:", list(k.columns))
except Exception as e:
    print("日K失败:", str(e)[:120])
