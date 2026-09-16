# -*- coding: utf-8 -*-
"""H1 数值等价性护栏：新 numpy 实现 vs _legacy 旧实现
- 30 只股票 × 全历史（主板/创业板/科创板/短史/长史/除权样本）
- 对每个改造函数对比 max(abs(diff)) < 1e-9
- 边界用例：空/长度1/长度<period/全等值/含0/单调
用法：python tools/test_h1_equivalence.py
"""
import os
import sqlite3
import sys

sys.path.insert(0, r"C:\Users\26838\A股模拟盘")
os.chdir(r"C:\Users\26838\A股模拟盘")

import numpy as np
from app import indicators as ind

TOL = 1e-9
PRICE_EPS = 1e-9  # 输出为价格/比值，绝对容差同 TOL

# ---------------- 数据加载 ----------------
def load_klines(codes):
    c = sqlite3.connect('file:data/market.db?mode=ro&immutable=1', uri=True)
    out = {}
    for code in codes:
        rows = c.execute(
            "SELECT date,open,high,low,close,volume,amount FROM kline "
            "WHERE code=? AND period='day' ORDER BY date", (code,)).fetchall()
        out[code] = [{"date": r[0], "open": r[1], "high": r[2], "low": r[3],
                      "close": r[4], "volume": r[5], "amount": r[6]} for r in rows]
    c.close()
    return out


def pick_codes(n=30):
    c = sqlite3.connect('file:data/market.db?mode=ro&immutable=1', uri=True)
    rows = c.execute(
        "SELECT code, COUNT(*) FROM kline WHERE period='day' AND code NOT LIKE 'sh%' "
        "AND code NOT LIKE 'sz%' AND code != '000001' AND code NOT LIKE 'bj%' "
        "GROUP BY code HAVING COUNT(*) >= 30").fetchall()
    c.close()
    import random
    random.seed(42)
    # 分桶覆盖：科创板 688、创业板 30、主板 60/00、短史(<100)、长史(>1000)
    codes = [r[0] for r in rows]
    buckets = {"688": [], "30": [], "60": [], "00": [], "short": [], "long": []}
    for code in codes:
        cnt = dict(rows)[code]
        if cnt > 1000:
            buckets["long"].append(code)
        elif cnt < 100:
            buckets["short"].append(code)
        for pre in ("688", "30", "60", "00"):
            if code.startswith(pre) and code not in buckets[pre] and cnt >= 100:
                buckets[pre].append(code)
    picked, seen = [], set()
    # 每桶至少 4 只，其余随机补齐
    order = ["688", "30", "60", "00", "short", "long"]
    for b in order:
        random.shuffle(buckets[b])
        for code in buckets[b][:5]:
            if code not in seen:
                picked.append(code)
                seen.add(code)
    rest = [x for x in codes if x not in seen]
    random.shuffle(rest)
    for code in rest:
        if len(picked) >= n:
            break
        picked.append(code)
        seen.add(code)
    return picked[:n]


# ---------------- 对比工具 ----------------
def _walk(a, b, path):
    """递归对比 list/tuple/dict，返回 (max_diff, n_items)"""
    if isinstance(a, (list, tuple)):
        if len(a) != len(b):
            return (float("inf"), 0, "len %d vs %d @ %s" % (len(a), len(b), path))
        md, n, msg = 0.0, 0, None
        for i, (x, y) in enumerate(zip(a, b)):
            d, nn, m = _walk(x, y, "%s[%d]" % (path, i))
            if m:
                return (float("inf"), 0, m)
            md = max(md, d)
            n += nn
        return md, n, None
    if isinstance(a, dict):
        if set(a) != set(b):
            return (float("inf"), 0, "dict keys diff @ %s" % path)
        md, n, msg = 0.0, 0, None
        for k in a:
            d, nn, m = _walk(a[k], b[k], "%s.%s" % (path, k))
            if m:
                return (float("inf"), 0, m)
            md = max(md, d)
            n += nn
        return md, n, None
    # 标量
    if a is None and b is None:
        return 0.0, 1, None
    if a is None or b is None:
        return (float("inf"), 1, "None mismatch @ %s (%r vs %r)" % (path, a, b))
    try:
        d = abs(float(a) - float(b))
    except (TypeError, ValueError):
        return (float("inf"), 1, "type mismatch @ %s (%r vs %r)" % (path, a, b))
    return d, 1, None


def compare(fn_new, fn_legacy, *args, label=""):
    r_new = fn_new(*args)
    r_old = fn_legacy(*args)
    md, n, msg = _walk(r_new, r_old, "$")
    ok = md < TOL and msg is None
    if not ok:
        print("  [FAIL] %s max_diff=%.3e items=%d %s" % (label, md, n, msg or ""))
    return ok, md, n


# ---------------- 构造各函数输入 ----------------
def inputs(kl):
    closes = [k["close"] for k in kl]
    highs = [k["high"] for k in kl]
    lows = [k["low"] for k in kl]
    vols = [k.get("volume", 0) or 0 for k in kl]
    return closes, highs, lows, vols


# ---------------- 边界用例 ----------------
def edge_cases():
    """返回 [(函数名, args列表), ...] 的边界输入（含空/短/全等/0/None/单调）"""
    edge = []
    # 空列表
    edge.append(("sma", ([], 5)))
    edge.append(("ema", ([], 5)))
    edge.append(("rsi", ([], 14)))
    edge.append(("rolling_max", ([], 20)))
    edge.append(("rolling_min", ([], 20)))
    edge.append(("volume_ma", ([], 5)))
    edge.append(("volume_slope", ([], 5)))
    edge.append(("price_volume_corr", ([], [], 10)))
    edge.append(("price_vol_divergence", ([], [], 10, 10)))
    edge.append(("obv", ([])))
    edge.append(("obv_slope", ([])))
    edge.append(("vwap", ([])))
    edge.append(("boll", ([], 20)))
    edge.append(("macd", ([], 12, 26, 9)))
    # 长度 1
    edge.append(("sma", ([1.0], 5)))
    edge.append(("rsi", ([10.0], 14)))
    edge.append(("rsrs", ([{"high": 1, "low": 1, "close": 1}], 18, 600)))
    edge.append(("kdj", ([{"high": 1, "low": 1, "close": 1}], 9)))
    edge.append(("atr", ([{"high": 1, "low": 1, "close": 1}], 14)))
    edge.append(("chip_profile", ([{"high": 1, "low": 1, "close": 1, "volume": 0}], 120, 0.965)))
    # 长度 < period
    edge.append(("sma", ([1, 2, 3], 5)))
    edge.append(("rsi", ([1, 2, 3, 4], 14)))
    edge.append(("rsrs", ([{"high": i + 1, "low": i, "close": i + 0.5} for i in range(10)], 18, 600)))
    # 全等值序列
    edge.append(("sma", ([5.0] * 50, 5)))
    edge.append(("rsi", ([5.0] * 50, 14)))
    edge.append(("rolling_max", ([3.3] * 50, 20)))
    edge.append(("boll", ([2.0] * 50, 20)))
    edge.append(("volume_slope", ([100.0] * 50, 5)))
    edge.append(("price_volume_corr", ([10.0] * 50, [100.0] * 50, 10)))
    edge.append(("rsrs", ([{"high": 5.0, "low": 4.0, "close": 4.5} for _ in range(50)], 18, 600)))
    edge.append(("chip_profile", ([{"high": 5.0, "low": 4.0, "close": 4.5, "volume": 1000} for _ in range(50)], 120, 0.965)))
    # 含 0（停牌 volume=0）
    kl0 = [{"high": 5.0, "low": 4.0, "close": 4.5, "volume": 0} for _ in range(30)]
    kl0[10]["volume"] = 0
    edge.append(("obv", (kl0,)))
    edge.append(("vwap", (kl0,)))
    edge.append(("atr", (kl0,)))
    edge.append(("chip_profile", (kl0, 120, 0.965)))
    # 含 0 价格/量
    edge.append(("sma", ([0, 1, 2, 3, 4, 5, 6], 3)))
    edge.append(("price_volume_corr", ([0, 1, 2, 3, 4, 5], [0, 100, 200, 300, 400, 500], 3)))
    # 单调序列
    edge.append(("sma", (list(range(60)), 5)))
    edge.append(("rsi", (list(range(60)), 14)))
    edge.append(("rsrs", ([{"high": i + 1.0, "low": i * 0.9, "close": i} for i in range(80)], 18, 600)))
    edge.append(("kdj", ([{"high": i + 1.0, "low": i * 0.9, "close": i} for i in range(40)], 9)))
    edge.append(("atr", ([{"high": i + 1.0, "low": i * 0.9, "close": i} for i in range(40)], 14)))
    edge.append(("obv", ([{"high": i + 1.0, "low": i * 0.9, "close": i, "volume": 100} for i in range(40)],)))
    edge.append(("chip_profile", ([{"high": i + 1.0, "low": i * 0.9, "close": i, "volume": 1000} for i in range(40)], 120, 0.965)))
    edge.append(("vwap", ([{"high": i + 1.0, "low": i * 0.9, "close": i, "volume": 100} for i in range(40)],)))
    return edge


# 改造函数清单（新实现与 _legacy 的对照）
FUNCS = ["sma", "ema", "macd", "rsi", "rolling_max", "rolling_min", "boll", "rsrs",
         "kdj", "atr", "obv", "obv_slope", "volume_ma", "vwap", "volume_slope",
         "price_volume_corr", "price_vol_divergence", "chip_profile",
         "volume_surge"]


def invoke(name, *args):
    """按函数名调用新/旧实现"""
    fnew = getattr(ind, name)
    fleg = getattr(ind, name + "_legacy", None)
    if fleg is None:
        return None, None
    return fnew(*args), fleg(*args)


def main():
    codes = pick_codes(30)
    data = load_klines(codes)
    print("测试股票 %d 只: %s" % (len(codes), ",".join(codes[:8]) + "..."))
    total_fn, total_ok = 0, 0
    fails = []
    # 主测试：30 只 × 全历史 × 每函数
    for code, kl in data.items():
        closes, highs, lows, vols = inputs(kl)
        cases = [
            ("sma", (closes, 5)), ("sma", (closes, 20)),
            ("ema", (closes, 12)), ("ema", (closes, 26)),
            ("macd", (closes, 12, 26, 9)),
            ("rsi", (closes, 14)), ("rsi", (closes, 6)),
            ("rolling_max", (highs, 20)), ("rolling_min", (lows, 20)),
            ("boll", (closes, 20, 2.0)),
            ("rsrs", (kl, 18, 600)),
            ("kdj", (kl, 9, 3, 3)),
            ("atr", (kl, 14)),
            ("obv", (kl,)),
            ("obv_slope", (kl, 10)),
            ("volume_ma", (vols, 5)),
            ("vwap", (kl,)),
            ("volume_slope", (vols, 5)),
            ("price_volume_corr", (closes, vols, 10)),
            ("price_vol_divergence", (closes, vols, 10, 10)),
            ("chip_profile", (kl, 120, 0.965)),
            ("volume_surge", (vols, 5)),
        ]
        for name, args in cases:
            total_fn += 1
            try:
                rn, ro = invoke(name, *args)
            except Exception as e:
                print("  [EXC] %s %s: %r" % (code, name, e))
                fails.append((code, name, "exc %r" % e))
                continue
            md, n, msg = _walk(rn, ro, "$")
            ok = md < TOL and msg is None
            total_ok += 1 if ok else 0
            if not ok:
                fails.append((code, name, "max_diff=%.3e %s" % (md, msg or "")))
                print("  [FAIL] %s %s max_diff=%.3e %s" % (code, name, md, msg or ""))
    # 边界用例
    print("\n=== 边界用例 ===")
    edge_ok, edge_fail = 0, 0
    for name, args in edge_cases():
        try:
            rn, ro = invoke(name, *args)
            md, n, msg = _walk(rn, ro, "$")
            ok = md < TOL and msg is None
        except Exception as e:
            # 新旧都应抛同样异常（或都不抛）
            rn_e = ro_e = None
            try:
                getattr(ind, name)(*args)
            except Exception as e1:
                rn_e = e1
            try:
                getattr(ind, name + "_legacy")(*args)
            except Exception as e2:
                ro_e = e2
            ok = (rn_e is None) == (ro_e is None)
            msg = "exc new=%r old=%r" % (rn_e, ro_e) if not ok else ""
        edge_ok += 1 if ok else 0
        edge_fail += 0 if ok else 1
        if not ok:
            fails.append(("edge", name, msg))
            print("  [FAIL] edge %s %s" % (name, msg))
    print("边界: %d 通过 / %d 失败" % (edge_ok, edge_fail))

    print("\n=== 汇总 ===")
    print("主测试: %d/%d 通过" % (total_ok, total_fn))
    print("失败清单: %s" % (fails[:10] if fails else "无"))
    return 0 if total_ok == total_fn and edge_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
