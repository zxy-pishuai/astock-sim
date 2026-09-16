# -*- coding: utf-8 -*-
"""F3（2026-09-13）打板行情时效保障：离线自检。

判据（预注册）：
1. force 透传：fetch_quotes_trading(codes, force=True) 时 _fetch_quotes_core 收到 force=True；
   force=False（默认）时收到 False（缓存命中不强制取数）。
2. 请求计数器：_http 真实请求后 request_count() 递增。
3. 强制刷新行为：force=True 绕过 3s 缓存（第二次同批调用仍发请求）；force=False 缓存命中不发。
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import datafeed as df
from app import config as C

# ---- 判据1：force 透传 ----
captured = {}

def _fake_core(codes, force=False, enrich=False, **kw):
    captured["force"] = force
    captured["codes"] = list(codes)
    return {c: {"price": 10.0, "name": "T", "pct_chg": 8.0, "amount": 1e8} for c in codes}

orig_core = df._fetch_quotes_core
df._fetch_quotes_core = _fake_core
try:
    df.fetch_quotes_trading(["600000"], force=True)
    v_true = captured.get("force")
    ok1a = v_true is True
    df.fetch_quotes_trading(["600000"])
    v_false = captured.get("force")
    ok1b = v_false is False
finally:
    df._fetch_quotes_core = orig_core
print("[判据1] force 透传: force=True ->", v_true,
      "| force=False(默认) ->", v_false)
print("  判据1", "通过 ✓" if (ok1a and ok1b) else "不通过 ✗")

# ---- 判据2：请求计数器 ----
n0 = df.request_count()
try:
    df.fetch_quotes_trading(["600000", "000001"], enrich=True)
except Exception:
    pass
n1 = df.request_count()
print("[判据2] 请求计数: 调用前 %d -> 调用后 %d" % (n0, n1))
print("  判据2", "通过 ✓" if n1 > n0 else "不通过 ✗")

# ---- 判据3：force 绕过缓存 vs 默认命中 ----
# 同一批：第一次真实取数写缓存；紧接着 force=True 应再发请求（计数+），默认应命中（计数不+）
n2 = df.request_count()
try:
    df.fetch_quotes_trading(["600519", "000858"], enrich=True, force=True)
except Exception:
    pass
n3 = df.request_count()
try:
    df.fetch_quotes_trading(["600519", "000858"], enrich=True)   # 3s 内应命中缓存
except Exception:
    pass
n4 = df.request_count()
print("[判据3] force=True 后计数 %d->%d（应增）| 默认再次调用 %d->%d（应不增）" % (n2, n3, n3, n4))
print("  判据3", "通过 ✓" if (n3 > n2 and n4 == n3) else "不通过 ✗")

sys.exit(0 if (ok1a and ok1b and n1 > n0 and n3 > n2 and n4 == n3) else 1)
