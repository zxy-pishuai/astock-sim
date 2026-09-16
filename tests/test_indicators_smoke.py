# -*- coding: utf-8 -*-
"""J4（2026-09-13）：indicators 冒烟——全部顶层指标函数 × 边界输入。
边界：空 / 长度1 / 短于period / 含None / 正常120根。
两类输入：数值序列（closes/series/volumes）与 kline dict 列表（klines）。
函数内部不抛异常即过；缺参数（测试组装问题）标 SKIP，函数内部异常记 FAIL。

已知边界缺陷（生产代码不改，显式 xfail 记录，见报告 §4.3）：
  - legacy 变体与 amt_change 对含 None 序列无防护（实盘 kline 无 None，低优先级）
  - obv/obv_slope/chip_profile 对空序列无防护
新增同类缺陷（不在 KNOWN 集合内）会被测试抓住。
"""
import inspect
import os
import sys
from datetime import date, timedelta

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import pytest

from app import indicators as ind

FNS = sorted(n for n, _ in inspect.getmembers(ind, inspect.isfunction)
             if not n.startswith("_"))

NUM_CASES = {
    "empty": [],
    "one": [10.0],
    "short": [10.0, 11.0, 10.5],
    "with_none": [10.0, None, 11.0, 10.6, 10.2, 10.8, 10.3, 10.9],
    "normal": [10.0 + i * 0.1 for i in range(120)],
}


def _to_klines(vals):
    d0 = date(2025, 1, 1)
    out = []
    for i, v in enumerate(vals):
        c = v if v is not None else 10.0
        out.append({"date": (d0 + timedelta(days=i)).isoformat(),
                    "open": c * 0.998, "high": c * 1.01, "low": c * 0.99,
                    "close": c, "volume": 2_000_000.0, "amount": 2e7})
    return out


KL_CASES = {k: _to_klines(v) for k, v in NUM_CASES.items()}

# 数值/序列类参数名 → 喂数值列表；klines 单独处理
_NUM_HINTS = ("period", "n", "window", "fast", "slow", "signal", "k", "d",
              "pct", "ma", "thr", "rate", "ratio", "limit", "span",
              "zscore_window", "k_period", "d_period", "bins", "decay",
              "price_period", "vol_period", "ma_period", "threshold",
              "today_vol")
_SER_HINTS = ("data", "arr", "series", "x", "prices", "closes", "values",
              "price", "vol", "volumes", "amount", "y", "base", "ref",
              "vwap_vals")


def _fill_kwargs(fn, num_data, kl_data):
    """按参数名喂数据：klines→kline dict 列表；closes/volumes→数值列表。"""
    kw = {}
    for pname, par in inspect.signature(fn).parameters.items():
        if par.default is not inspect.Parameter.empty:
            continue  # 有默认值 → 用默认
        if pname == "klines":
            kw[pname] = kl_data
        elif pname in _SER_HINTS:
            kw[pname] = num_data
        elif pname in _NUM_HINTS:
            kw[pname] = 5
        else:
            return {}, "未知必填参数 %s" % pname
    return kw, None


# 已知边界缺陷（2026-09-13 实测，生产代码不改——红线；见报告 §4.3）
KNOWN_XFAIL = {
    ("obv", "empty"),
    ("obv_slope", "empty"),
    ("chip_profile", "empty"),
    ("chip_profile_legacy", "empty"),
    ("amt_change", "with_none"),
    ("sma_legacy", "with_none"),
    ("volume_ma_legacy", "with_none"),
    ("volume_slope_legacy", "with_none"),
    ("volume_surge_legacy", "with_none"),
}


@pytest.mark.parametrize("name", FNS)
@pytest.mark.parametrize("label", list(NUM_CASES))
def test_indicator_boundary_no_crash(name, label):
    fn = getattr(ind, name)
    kw, skip = _fill_kwargs(fn, NUM_CASES[label], KL_CASES[label])
    if skip:
        pytest.skip(skip)
    if (name, label) in KNOWN_XFAIL:
        pytest.xfail("已知边界缺陷（生产无此输入，见报告 §4.3）")
    fn(**kw)


def test_known_core_indicators_normal():
    """核心指标在正常序列上返回非 None（防 NaN/None 泄漏）。"""
    for name in ("sma", "ema", "macd", "rsi", "kdj", "boll", "atr", "rsrs",
                 "momentum", "vwap", "volume_ratio", "obv"):
        fn = getattr(ind, name)
        kw, skip = _fill_kwargs(fn, NUM_CASES["normal"], KL_CASES["normal"])
        if skip:
            continue
        out = fn(**kw)
        assert out is not None, "%s 返回 None" % name
