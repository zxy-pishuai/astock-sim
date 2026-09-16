# -*- coding: utf-8 -*-
"""J4（2026-09-13）：scoring 冒烟——固定 fixture（3 票 × 250 根）→ score_stock。
返回 (数值, 列表) 且不抛异常，分数在 [0,100]。
"""
import os
import sys
from datetime import date, timedelta

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import numpy as np
import pytest

from app import scoring as sc

CODES = ("sh600000", "sz000001", "sz300750")


def _klines(n=250, seed=1):
    rng = np.random.default_rng(seed)
    close = 10.0 * np.cumprod(1 + rng.normal(0, 0.01, n))
    o = np.roll(close, 1)
    o[0] = close[0]
    h = np.maximum(o, close) * (1 + rng.uniform(0, 0.012, n))
    l = np.minimum(o, close) * (1 - rng.uniform(0, 0.012, n))
    v = rng.integers(1_000_000, 5_000_000, n).astype(float)
    a = v * close
    d0 = date(2025, 1, 1)
    return [{"date": (d0 + timedelta(days=i)).isoformat(),
             "open": float(o[i]), "high": float(h[i]), "low": float(l[i]),
             "close": float(close[i]), "volume": float(v[i]),
             "amount": float(a[i])} for i in range(n)]


@pytest.mark.parametrize("code", CODES)
def test_score_stock_returns_valid(code):
    k = _klines(seed=hash(code) % 1000)
    r = sc.score_stock(k, quote={"code": code}, code=code)
    assert isinstance(r, tuple) and len(r) >= 1, "score_stock 返回结构异常: %r" % (r,)
    score = r[0]
    assert isinstance(score, (int, float)) and not isinstance(score, bool), \
        "评分非数值: %r" % (score,)
    assert 0 <= score <= 100, "评分越界 [0,100]: %r" % score


def test_score_final_breakdown():
    """I2 统一评分入口：返回 (score, signals, breakdown)，breakdown 逐项可审计。"""
    k = _klines(seed=7)
    r = sc.score_final(k, quote={"code": "sh600000"}, code="sh600000",
                       as_of="2026-09-01")
    assert isinstance(r, tuple) and len(r) == 3, "score_final 返回结构: %r" % (r,)
    score, _signals, breakdown = r
    assert isinstance(score, (int, float)) and 0 <= score <= 100
    assert isinstance(breakdown, list) and len(breakdown) >= 1, \
        "breakdown 应为逐项列表: %r" % (breakdown,)
    for item in breakdown:
        assert isinstance(item, dict) and "item" in item and "score" in item, \
            "breakdown 项缺 item/score: %r" % (item,)
