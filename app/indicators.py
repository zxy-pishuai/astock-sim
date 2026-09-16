# -*- coding: utf-8 -*-
"""技术指标库 — 纯函数、无时间依赖（回测安全，避免前视偏差）
输入均为收盘价/序列 list，输出与输入等长的 list（前缀为 None）。

H1（2026-09-13）：numpy 向量化改造。每个被改造函数保留 `_legacy` 版本
（仅供 tools/test_h1_equivalence.py 对照，不参与生产调用）。
签名与返回类型不变；数值等价护栏 max(abs(diff)) < 1e-9。
"""
import math
from itertools import accumulate

import numpy as np


def sma(series, period):
    n = len(series)
    if n < period:
        return [None] * n
    a = np.asarray(series, dtype=float)
    res = [None] * (period - 1)
    s = np.convolve(a, np.ones(period, dtype=float), mode="valid") / period
    res.extend(s.tolist())
    return res


def sma_legacy(series, period):
    n = len(series)
    if n < period:
        return [None] * n
    res = [None] * (period - 1)
    s = sum(series[:period])
    res.append(s / period)
    for i in range(period, n):
        s += series[i] - series[i - period]
        res.append(s / period)
    return res


def ema(series, period):
    """EMA 指数移动平均。
    H1：递推有闭式解 v_i = (1-m)^k·v0 + m·Σ(1-m)^(k-j)·x_j → np.convolve 向量化。
    与旧递推的浮点差 ~1e-16 相对（等价测试覆盖）。
    """
    n = len(series)
    if n < period:
        return [None] * n
    a = np.asarray(series, dtype=float)
    m = 2.0 / (period + 1)
    v0 = a[:period].sum() / period
    rest = a[period:]
    k = n - period
    if k:
        w = m * np.power(1.0 - m, np.arange(k))
        conv = np.convolve(rest, w)[:k]
        decay = np.power(1.0 - m, np.arange(1, k + 1))
        vals = conv + decay * v0
        res = [None] * (period - 1)
        res.append(v0)
        res.extend(vals.tolist())
    else:
        res = [None] * (period - 1)
        res.append(v0)
    return res


def ema_legacy(series, period):
    return ema(series, period)


def macd(closes, fast=12, slow=26, signal=9):
    f = ema(closes, fast)
    s = ema(closes, slow)
    n = len(closes)
    dif = [(fi - si) if (fi is not None and si is not None) else None
           for fi, si in zip(f, s)]
    dea = ema([d if d is not None else 0.0 for d in dif], signal)
    dea = [None if dif[i] is None else dea[i] for i in range(n)]
    hist = [((dif[i] - dea[i]) * 2)
            if (dif[i] is not None and dea[i] is not None) else None
            for i in range(n)]
    return dif, dea, hist


def macd_legacy(closes, fast=12, slow=26, signal=9):
    return macd(closes, fast, slow, signal)


def rsi(closes, period=14):
    n = len(closes)
    if n < period + 1:
        return [None] * n
    a = np.asarray(closes, dtype=float)
    d = np.diff(a)
    gains = np.maximum(d, 0.0)
    losses = np.maximum(-d, 0.0)
    cs_g = np.concatenate(([0.0], np.cumsum(gains)))
    cs_l = np.concatenate(([0.0], np.cumsum(losses)))
    ag = (cs_g[period:] - cs_g[:-period]) / period
    al = (cs_l[period:] - cs_l[:-period]) / period
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.where(al == 0, 100.0, 100 - 100 / (1 + ag / al))
    res = [None] * period
    res.extend(out.tolist())
    return res


def rsi_legacy(closes, period=14):
    n = len(closes)
    if n < period + 1:
        return [None] * n
    res = [None] * period
    gains, losses = [], []
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    for i in range(period, n):
        if i > period:
            d = closes[i] - closes[i - 1]
            gains.pop(0); gains.append(max(d, 0.0))
            losses.pop(0); losses.append(max(-d, 0.0))
        ag = sum(gains) / period
        al = sum(losses) / period
        res.append(100.0 if al == 0 else 100 - 100 / (1 + ag / al))
    return res


def rolling_max(series, period=20):
    n = len(series)
    if n < period:
        return [None] * n
    a = np.asarray(series, dtype=float)
    res = [None] * (period - 1)
    sw = np.lib.stride_tricks.sliding_window_view(a, period).max(axis=-1)
    res.extend(sw.tolist())
    return res


def rolling_max_legacy(series, period=20):
    n = len(series)
    if n < period:
        return [None] * n
    res = [None] * (period - 1)
    win = list(series[:period])
    res.append(max(win))
    for i in range(period, n):
        win.pop(0); win.append(series[i])
        res.append(max(win))
    return res


def rolling_min(series, period=20):
    n = len(series)
    if n < period:
        return [None] * n
    a = np.asarray(series, dtype=float)
    res = [None] * (period - 1)
    sw = np.lib.stride_tricks.sliding_window_view(a, period).min(axis=-1)
    res.extend(sw.tolist())
    return res


def rolling_min_legacy(series, period=20):
    n = len(series)
    if n < period:
        return [None] * n
    res = [None] * (period - 1)
    win = list(series[:period])
    res.append(min(win))
    for i in range(period, n):
        win.pop(0); win.append(series[i])
        res.append(min(win))
    return res


def boll(closes, period=20, k=2.0):
    """布林带：返回 (mid, upper, lower)"""
    mid = sma(closes, period)
    n = len(closes)
    up, low = [None] * n, [None] * n
    if n < period:
        return mid, up, low
    a = np.asarray(closes, dtype=float)
    sw = np.lib.stride_tricks.sliding_window_view(a, period)
    m = np.asarray(mid[period - 1:], dtype=float)
    var = ((sw - m[:, None]) ** 2).sum(axis=1) / period
    sd = np.sqrt(var)
    up[period - 1:] = (m + k * sd).tolist()
    low[period - 1:] = (m - k * sd).tolist()
    return mid, up, low


def boll_legacy(closes, period=20, k=2.0):
    mid = sma_legacy(closes, period)
    n = len(closes)
    up, low = [None] * n, [None] * n
    for i in range(period - 1, n):
        w = closes[i - period + 1:i + 1]
        m = mid[i]
        var = sum((x - m) ** 2 for x in w) / period
        sd = math.sqrt(var)
        up[i] = m + k * sd
        low[i] = m - k * sd
    return mid, up, low


def rsrs(klines, window=18, zscore_window=600):
    """RSRS 右偏标准分（滚动OLS high = a + b*low）
    H1：var_l 阈值分支对浮点敏感（增量 vs 全量求和可能跨 1e-10），
    故四个滑动累加量保留旧实现增量顺序（逐位一致）；仅 var_h 的 O(p)
    内积改 numpy（无阈值分支，1e-16 级差异可接受）。
    """
    n = len(klines)
    if n < window + 2:
        return [None] * n
    highs = np.asarray([k["high"] for k in klines], dtype=float)
    lows = np.asarray([k["low"] for k in klines], dtype=float)
    hl = highs.tolist()
    lo = lows.tolist()
    hw_sq = (np.lib.stride_tricks.sliding_window_view(highs, window) ** 2)
    s_h2 = hw_sq.sum(axis=1)
    inv_w = 1.0 / window
    betas = [None] * n
    r2s = [None] * n
    s_low = sum(lo[:window])
    s_high = sum(hl[:window])
    s_low2 = sum((l * l for l in lo[:window]))
    s_lh = sum((l * h for l, h in zip(lo[:window], hl[:window])))
    # 首窗口（i == window-1，无增量更新）
    ml = s_low * inv_w
    mh = s_high * inv_w
    var_l = s_low2 * inv_w - ml * ml
    if var_l > 1e-10:
        cov = s_lh * inv_w - ml * mh
        beta = cov / var_l
        var_h = s_h2[0] * inv_w - mh * mh
        if var_h > 1e-10:
            r2 = cov * cov / (var_l * var_h)
            if r2 > 1.0:
                r2 = 1.0
            elif r2 < 0.0:
                r2 = 0.0
        else:
            r2 = 0.0
    else:
        beta, r2 = 1.0, 0.0
    betas[window - 1] = beta
    r2s[window - 1] = r2
    for i in range(window, n):
        ol = lo[i - window]
        nl = lo[i]
        oh = hl[i - window]
        nh = hl[i]
        s_low += nl - ol
        s_high += nh - oh
        s_low2 += nl * nl - ol * ol
        s_lh += nl * nh - ol * oh
        ml = s_low * inv_w
        mh = s_high * inv_w
        var_l = s_low2 * inv_w - ml * ml
        if var_l > 1e-10:
            cov = s_lh * inv_w - ml * mh
            beta = cov / var_l
            var_h = s_h2[i - window + 1] * inv_w - mh * mh
            if var_h > 1e-10:
                r2 = cov * cov / (var_l * var_h)
                if r2 > 1.0:
                    r2 = 1.0
                elif r2 < 0.0:
                    r2 = 0.0
            else:
                r2 = 0.0
        else:
            beta, r2 = 1.0, 0.0
        betas[i] = beta
        r2s[i] = r2
    bv = betas[window - 1:]
    if len(bv) < zscore_window + window:
        return [None] * n
    bv2 = [b * b for b in bv]
    zs = [None] * n
    s_b = sum(bv[:zscore_window])
    s_b2 = sum(bv2[:zscore_window])
    inv_z = 1.0 / zscore_window
    for t in range(zscore_window, len(bv)):
        idx = t + window - 1
        mb = s_b * inv_z
        vb = max(1e-10, s_b2 * inv_z - mb * mb)
        sd = math.sqrt(vb)
        z = (bv[t] - mb) / sd
        r2v = r2s[idx] if r2s[idx] is not None else 0.0
        zs[idx] = z * r2v * bv[t]
        s_b += bv[t] - bv[t - zscore_window]
        s_b2 += bv2[t] - bv2[t - zscore_window]
    return zs


def rsrs_legacy(klines, window=18, zscore_window=600):
    n = len(klines)
    if n < window + 2:
        return [None] * n
    highs = [k["high"] for k in klines]
    lows = [k["low"] for k in klines]
    betas = [None] * (window - 1)
    r2s = [None] * (window - 1)
    s_low = sum(lows[:window]); s_high = sum(highs[:window])
    s_low2 = sum(l * l for l in lows[:window])
    s_lh = sum(l * h for l, h in zip(lows[:window], highs[:window]))
    for i in range(window - 1, n):
        if i >= window:
            ol, nl = lows[i - window], lows[i]
            oh, nh = highs[i - window], highs[i]
            s_low += nl - ol; s_high += nh - oh
            s_low2 += nl * nl - ol * ol
            s_lh += nl * nh - ol * oh
        ml = s_low / window
        mh = s_high / window
        var_l = s_low2 / window - ml * ml
        if var_l > 1e-10:
            beta = (s_lh / window - ml * mh) / var_l
            cov = s_lh / window - ml * mh
            seg = highs[i - window + 1:i + 1]
            var_h = sum(h * h for h in seg) / window - mh * mh
            r2 = min(1.0, max(0.0, cov * cov / (var_l * var_h))) if var_h > 1e-10 else 0.0
        else:
            beta, r2 = 1.0, 0.0
        betas.append(beta); r2s.append(r2)
    zs = [None] * len(betas)
    bv = [b for b in betas if b is not None]
    if len(bv) < zscore_window + window:
        return zs
    s_b = sum(bv[:zscore_window])
    s_b2 = sum(b * b for b in bv[:zscore_window])
    for t in range(zscore_window, len(bv)):
        idx = t + (window - 1)
        if idx >= len(betas):
            break
        mb = s_b / zscore_window
        vb = max(1e-10, s_b2 / zscore_window - mb * mb)
        sd = math.sqrt(vb)
        z = (bv[t] - mb) / sd
        r2v = r2s[idx] if r2s[idx] is not None else 0.0
        zs[idx] = z * r2v * bv[t]
        s_b += bv[t] - bv[t - zscore_window]
        s_b2 += bv[t] ** 2 - bv[t - zscore_window] ** 2
    return zs


def volume_ratio(volumes, today_vol):
    """量比 = 今日量 / 前5日均量（排除当日）"""
    if len(volumes) >= 6:
        avg5 = sum(volumes[-6:-1]) / 5
    elif len(volumes) > 1:
        avg5 = sum(volumes[:-1]) / (len(volumes) - 1)
    else:
        avg5 = 0
    return today_vol / avg5 if avg5 > 0 else 1.0


def detect_streak(klines, limit_pct=0.095):
    """连板数：从倒数第二根往前数连续涨停（不含今日）"""
    n = len(klines)
    if n < 3:
        return 0
    streak = 0
    for i in range(n - 2, 0, -1):
        prev = klines[i - 1]["close"]
        if prev > 0 and klines[i]["close"] >= prev * (1 + limit_pct - 0.005):
            streak += 1
        else:
            break
    return streak


# ==================== v3.6：因子库扩展（KDJ/ATR/OBV/动量/波动率） ====================

def kdj(klines, period=9, k_period=3, d_period=3):
    """KDJ 随机指标。返回 (K, D, J) 三序列"""
    n = len(klines)
    if n < period:
        return [None] * n, [None] * n, [None] * n
    highs = np.asarray([k["high"] for k in klines], dtype=float)
    lows = np.asarray([k["low"] for k in klines], dtype=float)
    closes = np.asarray([k["close"] for k in klines], dtype=float)
    hw = np.lib.stride_tricks.sliding_window_view(highs, period).max(axis=1)
    lw = np.lib.stride_tricks.sliding_window_view(lows, period).min(axis=1)
    rsv = np.where(hw > lw, (closes[period - 1:] - lw) / (hw - lw) * 100.0, 50.0)
    k, d = [None] * n, [None] * n
    k_prev, d_prev = 50.0, 50.0
    for i in range(period - 1, n):
        r = rsv[i - period + 1]
        k_cur = (2.0 / 3.0) * k_prev + (1.0 / 3.0) * r
        d_cur = (2.0 / 3.0) * d_prev + (1.0 / 3.0) * k_cur
        k[i] = k_cur
        d[i] = d_cur
        k_prev, d_prev = k_cur, d_cur
    j = [None] * n
    for i in range(period - 1, n):
        j[i] = 3 * k[i] - 2 * d[i]
    return k, d, j


def kdj_legacy(klines, period=9, k_period=3, d_period=3):
    n = len(klines)
    if n < period:
        return [None] * n, [None] * n, [None] * n
    k, d = [None] * n, [None] * n
    k_prev = 50.0
    d_prev = 50.0
    for i in range(period - 1, n):
        win = klines[i - period + 1:i + 1]
        low = min(x["low"] for x in win)
        high = max(x["high"] for x in win)
        rsv = (klines[i]["close"] - low) / (high - low) * 100 if high > low else 50.0
        k_cur = (2.0 / 3.0) * k_prev + (1.0 / 3.0) * rsv
        d_cur = (2.0 / 3.0) * d_prev + (1.0 / 3.0) * k_cur
        k[i] = k_cur
        d[i] = d_cur
        k_prev, d_prev = k_cur, d_cur
    j = [None] * n
    for i in range(n):
        if k[i] is not None and d[i] is not None:
            j[i] = 3 * k[i] - 2 * d[i]
    return k, d, j


def atr(klines, period=14):
    """ATR 平均真实波幅"""
    n = len(klines)
    if n < 2:
        return [None] * n
    highs = np.asarray([k["high"] for k in klines], dtype=float)
    lows = np.asarray([k["low"] for k in klines], dtype=float)
    closes = np.asarray([k["close"] for k in klines], dtype=float)
    trs = np.empty(n)
    trs[0] = 0.0
    trs[1:] = np.maximum.reduce([
        highs[1:] - lows[1:],
        np.abs(highs[1:] - closes[:-1]),
        np.abs(lows[1:] - closes[:-1]),
    ])
    res = [None] * n
    if n < period:
        return res
    s = trs[1:period + 1].sum()
    res[period] = s / period
    for i in range(period + 1, n):
        s = s - s / period + trs[i]
        res[i] = s / period
    return res


def atr_legacy(klines, period=14):
    n = len(klines)
    if n < 2:
        return [None] * n
    trs = [None]
    for i in range(1, n):
        h, l, pc = klines[i]["high"], klines[i]["low"], klines[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    res = [None] * n
    if n < period:
        return res
    s = sum(trs[1:period + 1])
    res[period] = s / period
    for i in range(period + 1, n):
        s = s - s / period + trs[i]
        res[i] = s / period
    return res


def obv(klines):
    """OBV 能量潮（累积量）"""
    n = len(klines)
    res = [None] * n
    closes = np.asarray([k["close"] for k in klines], dtype=float)
    vols = np.asarray([k.get("volume", 0) or 0 for k in klines], dtype=float)
    d = np.diff(closes)
    direction = np.where(d > 0, 1.0, np.where(d < 0, -1.0, 0.0))
    add = direction * vols[1:]
    res[0] = 0.0
    res[1:] = np.cumsum(add).tolist()
    return res


def obv_legacy(klines):
    n = len(klines)
    res = [None] * n
    cum = 0.0
    prev_c = None
    for i in range(n):
        c = klines[i]["close"]
        v = klines[i].get("volume", 0) or 0
        if prev_c is not None:
            if c > prev_c:
                cum += v
            elif c < prev_c:
                cum -= v
        res[i] = cum
        prev_c = c
    return res


def momentum(closes, period=10):
    """动量因子：今日收盘 / N 日前收盘 - 1"""
    n = len(closes)
    res = [None] * n
    for i in range(period, n):
        if closes[i - period] > 0:
            res[i] = closes[i] / closes[i - period] - 1
    return res


def volatility(closes, period=20):
    """滚动波动率（日收益标准差，年化）"""
    n = len(closes)
    res = [None] * n
    for i in range(period, n):
        seg = closes[i - period + 1:i + 1]
        rets = [seg[j] / seg[j - 1] - 1 for j in range(1, len(seg)) if seg[j - 1] > 0]
        if len(rets) >= 2:
            mean = sum(rets) / len(rets)
            var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
            res[i] = math.sqrt(var) * math.sqrt(244)
    return res


def rsv_raw(klines, period=9):
    """RSV（未成熟随机值）——KDJ 的前置因子"""
    n = len(klines)
    res = [None] * n
    for i in range(period - 1, n):
        win = klines[i - period + 1:i + 1]
        low = min(x["low"] for x in win)
        high = max(x["high"] for x in win)
        res[i] = (klines[i]["close"] - low) / (high - low) * 100 if high > low else 50.0
    return res


def rsi_ma(closes, period=14, ma_period=6):
    """RSI 均线（RSI 的 N 日均线，金叉死叉辅助因子）"""
    r = rsi(closes, period)
    n = len(r)
    res = [None] * n
    for i in range(ma_period - 1, n):
        vals = [x for x in r[i - ma_period + 1:i + 1] if x is not None]
        if len(vals) == ma_period:
            res[i] = sum(vals) / ma_period
    return res


def amt_change(closes, period=5):
    """N 日累计涨跌幅（价格动量）"""
    n = len(closes)
    res = [None] * n
    for i in range(period, n):
        if closes[i - period] > 0:
            res[i] = closes[i] / closes[i - period] - 1
    return res


def volume_ma(volumes, period=5):
    """N 日均量"""
    n = len(volumes)
    if n < period:
        return [None] * n
    a = np.asarray(volumes, dtype=float)
    res = [None] * (period - 1)
    s = np.convolve(a, np.ones(period, dtype=float), mode="valid") / period
    res.extend(s.tolist())
    return res


def volume_ma_legacy(volumes, period=5):
    n = len(volumes)
    if n < period:
        return [None] * n
    res = [None] * (period - 1)
    s = sum(volumes[:period])
    res.append(s / period)
    for i in range(period, n):
        s += volumes[i] - volumes[i - period]
        res.append(s / period)
    return res


# ==================== 4.4：量价深度因子库（量价是决定股价的唯二因素） ====================

def vwap(klines, period=None):
    """VWAP 成交量加权均价：Σ(价×量)/Σ量。
    全周期或近 period 日。返回与 klines 等长的 list。
    """
    n = len(klines)
    res = [None] * n
    highs = np.asarray([k["high"] for k in klines], dtype=float)
    lows = np.asarray([k["low"] for k in klines], dtype=float)
    closes = np.asarray([k["close"] for k in klines], dtype=float)
    vols = np.asarray([k.get("volume", 0) or 0 for k in klines], dtype=float)
    prices = (highs + lows + closes) / 3
    cum_amt = np.cumsum(prices * vols)
    cum_vol = np.cumsum(vols)
    idx = np.flatnonzero(cum_vol > 0)
    if idx.size:
        vals = cum_amt[idx] / cum_vol[idx]
        for j, p in enumerate(idx.tolist()):
            res[p] = vals[j]
    return res


def vwap_legacy(klines, period=None):
    n = len(klines)
    res = [None] * n
    cum_amt = 0.0
    cum_vol = 0.0
    for i in range(n):
        price = (klines[i]["high"] + klines[i]["low"] + klines[i]["close"]) / 3
        vol = klines[i].get("volume", 0) or 0
        amt = price * vol
        cum_amt += amt
        cum_vol += vol
        if cum_vol > 0:
            res[i] = cum_amt / cum_vol
    return res


def vwap_deviation(closes, vwap_vals):
    """价格偏离 VWAP：close/vwap - 1（正=价格在均线上方）"""
    n = len(closes)
    return [closes[i] / vwap_vals[i] - 1 if vwap_vals[i] else None for i in range(n)]


def volume_surge(volumes, period=5, threshold=1.5):
    """量能突变：今日量 / 前N日均量（>threshold 为放量突变）。
    返回与 volumes 等长 list（None 前缀）。
    """
    n = len(volumes)
    res = [None] * n
    if n <= period:
        return res
    a = np.asarray(volumes, dtype=float)
    cs = np.concatenate(([0.0], np.cumsum(a)))
    avg = (cs[period:n] - cs[:n - period]) / period
    v = a[period:]
    out = np.divide(v, avg, out=np.ones_like(v), where=avg > 0)
    res[period:] = out.tolist()
    return res


def volume_surge_legacy(volumes, period=5, threshold=1.5):
    n = len(volumes)
    res = [None] * n
    for i in range(period, n):
        avg = sum(volumes[i - period:i]) / period
        res[i] = volumes[i] / avg if avg > 0 else 1.0
    return res


def volume_slope(volumes, period=5):
    """量能斜率：近 N 日量能线性趋势（>0 递增放量，<0 递减缩量）。
    返回与 volumes 等长 list。
    """
    n = len(volumes)
    res = [None] * n
    if n <= period:
        return res
    a = np.asarray(volumes, dtype=float)
    sw = np.lib.stride_tricks.sliding_window_view(a, period)
    x_mean = period / 2.0
    j = np.arange(1, period + 1, dtype=float)
    xd = j - x_mean
    y_mean = sw.sum(axis=1) / period
    num = ((sw - y_mean[:, None]) * xd).sum(axis=1)
    den = float((xd * xd).sum())
    # 旧实现窗口 = volumes[i-period+1..i]（i=period..n-1）→ 对应 sw[1..n-period]
    if den > 0:
        res[period:] = (num[1:] / den).tolist()
    else:
        res[period:] = [0.0] * (n - period)
    return res


def volume_slope_legacy(volumes, period=5):
    n = len(volumes)
    res = [None] * n
    for i in range(period, n):
        seg = volumes[i - period + 1:i + 1]
        x_mean = period / 2.0
        y_mean = sum(seg) / period
        num = sum((j + 1 - x_mean) * (seg[j] - y_mean) for j in range(period))
        den = sum((j + 1 - x_mean) ** 2 for j in range(period))
        res[i] = num / den if den > 0 else 0.0
    return res


def price_volume_corr(closes, volumes, period=10):
    """量价相关性：近 N 日价格与成交量的相关系数。
    正=量价齐升（健康），负=量价背离（警惕）。返回 list。
    """
    n = len(closes)
    res = [None] * n
    if n <= period:
        return res
    c = np.asarray(closes, dtype=float)
    v = np.asarray(volumes, dtype=float)
    cw = np.lib.stride_tricks.sliding_window_view(c, period)
    vw = np.lib.stride_tricks.sliding_window_view(v, period)
    mc = cw.sum(axis=1) / period
    mv = vw.sum(axis=1) / period
    num = ((cw - mc[:, None]) * (vw - mv[:, None])).sum(axis=1)
    dc = np.sqrt(((cw - mc[:, None]) ** 2).sum(axis=1))
    dv = np.sqrt(((vw - mv[:, None]) ** 2).sum(axis=1))
    corr = np.divide(num, dc * dv, out=np.zeros_like(num),
                     where=(dc > 0) & (dv > 0))
    # 旧实现窗口 = closes[i-period+1..i]（i=period..n-1）→ 对应 sw[1..n-period]
    res[period:] = corr[1:].tolist()
    return res


def price_volume_corr_legacy(closes, volumes, period=10):
    n = len(closes)
    res = [None] * n
    for i in range(period, n):
        cs = closes[i - period + 1:i + 1]
        vs = volumes[i - period + 1:i + 1]
        mc, mv = sum(cs) / period, sum(vs) / period
        num = sum((cs[j] - mc) * (vs[j] - mv) for j in range(period))
        dc = math.sqrt(sum((c - mc) ** 2 for c in cs))
        dv = math.sqrt(sum((v - mv) ** 2 for v in vs))
        res[i] = num / (dc * dv) if dc > 0 and dv > 0 else 0.0
    return res


def volume_ma_cross(volumes, fast=5, slow=10):
    """量能均线金叉死叉：快线>慢线=1（放量），<慢线=-1（缩量）。返回 list"""
    f = volume_ma(volumes, fast)
    s = volume_ma(volumes, slow)
    n = len(volumes)
    res = [None] * n
    for i in range(n):
        if f[i] is not None and s[i] is not None:
            res[i] = 1 if f[i] > s[i] else -1
    return res


def obv_slope(klines, period=10):
    """OBV 斜率：能量潮线性趋势（>0 资金持续流入）。
    返回与 klines 等长 list。
    """
    ob = obv(klines)
    n = len(ob)
    res = [None] * n
    if n <= period:
        return res
    a = np.asarray([x if x is not None else 0.0 for x in ob], dtype=float)
    sw = np.lib.stride_tricks.sliding_window_view(a, period)
    x_mean = period / 2.0
    j = np.arange(1, period + 1, dtype=float)
    xd = j - x_mean
    y_mean = sw.sum(axis=1) / period
    num = ((sw - y_mean[:, None]) * xd).sum(axis=1)
    den = float((xd * xd).sum())
    if den > 0:
        res[period:] = (num[1:] / den).tolist()
    else:
        res[period:] = [0.0] * (n - period)
    return res


def obv_slope_legacy(klines, period=10):
    ob = obv_legacy(klines)
    n = len(ob)
    res = [None] * n
    for i in range(period, n):
        seg = [x for x in ob[i - period + 1:i + 1] if x is not None]
        if len(seg) < period:
            continue
        x_mean = period / 2.0
        y_mean = sum(seg) / period
        num = sum((j + 1 - x_mean) * (seg[j] - y_mean) for j in range(period))
        den = sum((j + 1 - x_mean) ** 2 for j in range(period))
        res[i] = num / den if den > 0 else 0.0
    return res


def price_vol_divergence(closes, volumes, price_period=10, vol_period=10):
    """量价背离强度：价格动量方向 × 量能方向 不一致度。
    返回 list：>0 背离（价涨量缩或价跌量增），<0 一致。
    """
    n = len(closes)
    res = [None] * n
    start = max(price_period, vol_period)
    if n <= start:
        return res
    c = np.asarray(closes, dtype=float)
    v = np.asarray(volumes, dtype=float)
    base = c[:n - price_period]
    pc = np.where(base > 0, c[price_period:] / base - 1, 0.0)
    vw = np.lib.stride_tricks.sliding_window_view(v, vol_period)
    vavg = vw.sum(axis=1) / vol_period
    # 旧窗口 v[i-vol_period..i-1] → vw 前 n-vol_period 个
    pv = v[vol_period:] / np.maximum(vavg[:n - vol_period], 1.0) - 1
    P = pc[start - price_period:n - price_period]
    Q = pv[start - vol_period:n - vol_period]
    m = np.minimum(np.abs(P), np.abs(Q))
    sign_diff = ((P > 0) & (Q < 0)) | ((P < 0) & (Q > 0))
    res[start:] = np.where(sign_diff, m, -m).tolist()
    return res


def price_vol_divergence_legacy(closes, volumes, price_period=10, vol_period=10):
    n = len(closes)
    res = [None] * n
    for i in range(max(price_period, vol_period), n):
        pc = closes[i] / closes[i - price_period] - 1 if closes[i - price_period] > 0 else 0.0
        pv = volumes[i] / max(sum(volumes[i - vol_period:i]) / vol_period, 1) - 1
        if pc > 0 and pv < 0:
            res[i] = min(abs(pc), abs(pv))
        elif pc < 0 and pv > 0:
            res[i] = min(abs(pc), abs(pv))
        else:
            res[i] = -min(abs(pc), abs(pv))
    return res


# ==================== 4.4：成本筹码（CYQ 简化版） ====================
# 量最终沉淀为"成本结构"：每日成交量按三角形分布撒入价格网格，
# 历史筹码按衰减系数逐日衰减（换手置换），得到成本分布曲线。
# 输出与 klines 等长序列：winner 获利盘 / cost50 平均成本 / conc 集中度 / dev 成本偏离。

def chip_profile(klines, bins=120, decay=0.965):
    """筹码分布（通达信 CYQ 简化版，纯标准库，O(n×bins)）。
    bins: 价格网格档数；decay: 单日筹码衰减（0.965≈60日半衰期，模拟换手置换）。
    返回 dict{winner, cost50, concentration, cost_dev}，各 list 与 klines 等长（前缀 None）。
    H1 回退说明：向量化尝试（np.cumsum / numpy ufunc 撒入）产生 1 ULP 浮点尾差，
    经 total 放大后使 cost50 临界档判定差一个 bin（等价测试 1e-9 容差不达标），
    且衰减递推本身逐 bar 串行无法跨 bar 合并 → 按任务书"不通过函数回退旧实现"处置。
    """
    n = len(klines)
    lows = [k.get("low", k["close"]) for k in klines]
    highs = [k.get("high", k["close"]) for k in klines]
    closes = [k["close"] for k in klines]
    vols = [k.get("volume", 0) or 0 for k in klines]
    pmin, pmax = min(lows), max(highs)
    if pmax <= pmin:
        pmax = pmin * 1.01 + 1e-9
    width = (pmax - pmin) / bins
    chips = [0.0] * bins
    res = {"winner": [None] * n, "cost50": [None] * n,
           "concentration": [None] * n, "cost_dev": [None] * n}
    for i in range(n):
        lo, hi, c, v = lows[i], highs[i], closes[i], vols[i]
        chips = [x * decay for x in chips]
        if v > 0 and hi > lo:
            b0 = max(0, int((lo - pmin) / width))
            b1 = min(bins - 1, int((hi - pmin) / width))
            weights = []
            for b in range(b0, b1 + 1):
                px = pmin + (b + 0.5) * width
                if px < lo or px > hi:
                    weights.append(0.0)
                elif px <= c:
                    weights.append((px - lo) / (c - lo) if c > lo else 1.0)
                else:
                    weights.append((hi - px) / (hi - c) if hi > c else 1.0)
            sw = sum(weights)
            if sw > 0:
                for j, w in enumerate(weights):
                    chips[b0 + j] += v * w / sw
        total = sum(chips)
        if total <= 0:
            continue
        cidx = min(bins - 1, max(0, int((c - pmin) / width)))
        below = sum(chips[:cidx + 1])
        res["winner"][i] = below / total

        def _cost(p):
            acc = 0.0
            for b in range(bins):
                acc += chips[b]
                if acc >= total * p:
                    return pmin + (b + 0.5) * width
            return pmax

        c50 = _cost(0.50)
        res["cost50"][i] = c50
        c5, c95 = _cost(0.05), _cost(0.95)
        if c95 > c5:
            res["concentration"][i] = (c95 - c5) / (c95 + c5)
        if c50 > 0:
            res["cost_dev"][i] = c / c50 - 1
    return res


def chip_profile_legacy(klines, bins=120, decay=0.965):
    n = len(klines)
    lows = [k.get("low", k["close"]) for k in klines]
    highs = [k.get("high", k["close"]) for k in klines]
    closes = [k["close"] for k in klines]
    vols = [k.get("volume", 0) or 0 for k in klines]
    pmin, pmax = min(lows), max(highs)
    if pmax <= pmin:
        pmax = pmin * 1.01 + 1e-9
    width = (pmax - pmin) / bins
    chips = [0.0] * bins
    res = {"winner": [None] * n, "cost50": [None] * n,
           "concentration": [None] * n, "cost_dev": [None] * n}
    for i in range(n):
        lo, hi, c, v = lows[i], highs[i], closes[i], vols[i]
        chips = [x * decay for x in chips]
        if v > 0 and hi > lo:
            b0 = max(0, int((lo - pmin) / width))
            b1 = min(bins - 1, int((hi - pmin) / width))
            weights = []
            for b in range(b0, b1 + 1):
                px = pmin + (b + 0.5) * width
                if px < lo or px > hi:
                    weights.append(0.0)
                elif px <= c:
                    weights.append((px - lo) / (c - lo) if c > lo else 1.0)
                else:
                    weights.append((hi - px) / (hi - c) if hi > c else 1.0)
            sw = sum(weights)
            if sw > 0:
                for j, w in enumerate(weights):
                    chips[b0 + j] += v * w / sw
        total = sum(chips)
        if total <= 0:
            continue
        cidx = min(bins - 1, max(0, int((c - pmin) / width)))
        below = sum(chips[:cidx + 1])
        res["winner"][i] = below / total

        def _cost(p):
            acc = 0.0
            for b in range(bins):
                acc += chips[b]
                if acc >= total * p:
                    return pmin + (b + 0.5) * width
            return pmax

        c50 = _cost(0.50)
        res["cost50"][i] = c50
        c5, c95 = _cost(0.05), _cost(0.95)
        if c95 > c5:
            res["concentration"][i] = (c95 - c5) / (c95 + c5)
        if c50 > 0:
            res["cost_dev"][i] = c / c50 - 1
    return res
