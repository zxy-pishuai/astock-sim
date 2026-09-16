# -*- coding: utf-8 -*-
"""★ 4.6 GTJA191 量价因子精选（阶段3，参考国泰君安 191 因子库）
纯 Python 列表实现 15 个成交量/成交额/换手相关因子，注册进 factor.FACTORS。

适配说明（重要）：
  - GTJA191 的 RANK 是截面（每日跨股票），本系统因子按单股时序计算，
    故 RANK → 时序分位 ts_rank（近 N 日滚动百分位）；CORR 为个股滚动相关。
    这是单股特征库的标准适配（同 qlib Alpha158 单股切片实现思路）。
  - 因子签名统一 (closes, klines)，内部自取 volume/amount/high/low，
    满足 factor.compute_factor 对 needs_klines=True 的派发约定。
验证：tools/validate_gtja_factors.py（IC / ICIR / 分层单调性 / 与现有因子相关性）
"""
import math

from . import indicators as ind


# ==================== 工具 ====================
def _tsrank(series_vals, period):
    """时序分位：每个点在其近 period 窗口内的百分位（含自身，0~1）"""
    n = len(series_vals)
    out = [None] * n
    for i in range(period, n):
        win = series_vals[i - period + 1:i + 1]
        valid = [v for v in win if v is not None]
        if len(valid) < 2:
            continue
        x = valid[-1]
        below = sum(1 for v in valid if v < x)
        out[i] = below / (len(valid) - 1) if len(valid) > 1 else 0.5
    return out


def _roll_corr(a, b, period):
    """滚动 Pearson 相关（同 indicators.price_volume_corr 模式）"""
    n = len(a)
    out = [None] * n
    for i in range(period, n):
        xs, ys = a[i - period + 1:i + 1], b[i - period + 1:i + 1]
        mx, my = sum(xs) / period, sum(ys) / period
        num = sum((xs[j] - mx) * (ys[j] - my) for j in range(period))
        dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
        dy = math.sqrt(sum((y - my) ** 2 for y in ys))
        out[i] = num / (dx * dy) if dx > 0 and dy > 0 else 0.0
    return out


def _std(series, period):
    n = len(series)
    out = [None] * n
    for i in range(period, n):
        seg = series[i - period + 1:i + 1]
        m = sum(seg) / period
        var = sum((x - m) ** 2 for x in seg) / period
        out[i] = math.sqrt(var)
    return out


def _conv(klines):
    """解包日K → (closes, volumes, amounts, highs, lows)"""
    closes = [k["close"] for k in klines]
    volumes = [k.get("volume", 0) or 0 for k in klines]
    amounts = [k.get("amount", 0) or 0 for k in klines]
    highs = [k["high"] for k in klines]
    lows = [k["low"] for k in klines]
    return closes, volumes, amounts, highs, lows


# ==================== 15 个 GTJA191 量价因子 ====================
def _g_vol_dev20(closes, klines):
    """VMA20偏离：vol/sma(vol,20)-1（量能相对位置）"""
    _, vols, _, _, _ = _conv(klines)
    vma = ind.sma(vols, 20)
    return [vols[i] / vma[i] - 1 if vma[i] else None for i in range(len(vols))]


def _g_vol_cv20(closes, klines):
    """量能变异20：20日量标准差/20日均量（量能稳定度，高=忽冷忽热）"""
    _, vols, _, _, _ = _conv(klines)
    vma = ind.sma(vols, 20)
    vsd = _std(vols, 20)
    return [vsd[i] / vma[i] if (vma[i] and vsd[i]) else None
            for i in range(len(vols))]


def _g_vol_ma5o20(closes, klines):
    """量能均线5/20：sma(vol,5)/sma(vol,20)-1（短期量能趋势）"""
    _, vols, _, _, _ = _conv(klines)
    v5, v20 = ind.sma(vols, 5), ind.sma(vols, 20)
    return [v5[i] / v20[i] - 1 if v20[i] else None for i in range(len(vols))]


def _g_amt_dev20(closes, klines):
    """成交额偏离20：amount/sma(amount,20)-1（成交额突增，近似换手放大）"""
    _, _, amts, _, _ = _conv(klines)
    ama = ind.sma(amts, 20)
    return [amts[i] / ama[i] - 1 if ama[i] else None for i in range(len(amts))]


def _g_amt_ma5o20(closes, klines):
    """成交额均线5/20：sma(amount,5)/sma(amount,20)-1"""
    _, _, amts, _, _ = _conv(klines)
    a5, a20 = ind.sma(amts, 5), ind.sma(amts, 20)
    return [a5[i] / a20[i] - 1 if a20[i] else None for i in range(len(amts))]


def _g_vol_z20(closes, klines):
    """量能Z分20：(vol-sma(vol,20))/std(vol,20)"""
    _, vols, _, _, _ = _conv(klines)
    vma, vsd = ind.sma(vols, 20), _std(vols, 20)
    return [(vols[i] - vma[i]) / vsd[i] if vma[i] and vsd[i] else None
            for i in range(len(vols))]


def _g_amt_z20(closes, klines):
    """成交额Z分20：(amount-sma(amount,20))/std(amount,20)"""
    _, _, amts, _, _ = _conv(klines)
    ama, asd = ind.sma(amts, 20), _std(amts, 20)
    return [(amts[i] - ama[i]) / asd[i] if ama[i] and asd[i] else None
            for i in range(len(amts))]


def _g_vp_rank_corr5(closes, klines):
    """GTJA Alpha#10 时序化：corr(tsrank(vol,20), tsrank(close,20), 5)"""
    _, vols, _, _, _ = _conv(klines)
    rv = _tsrank(vols, 20)
    rc = _tsrank(closes, 20)
    n = len(rv)
    out = [None] * n
    for i in range(24, n):          # 20(rank窗口) + 5(corr窗) - 1
        xs, ys = rv[i - 4:i + 1], rc[i - 4:i + 1]
        if any(x is None for x in xs) or any(y is None for y in ys):
            continue
        mx, my = sum(xs) / 5, sum(ys) / 5
        num = sum((xs[j] - mx) * (ys[j] - my) for j in range(5))
        dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
        dy = math.sqrt(sum((y - my) ** 2 for y in ys))
        out[i] = num / (dx * dy) if dx > 0 and dy > 0 else 0.0
    return out


def _g_vh_rank_corr5(closes, klines):
    """GTJA Alpha#30 时序化：sign(corr(tsrank(vol,10), tsrank(high,10), 5))"""
    _, vols, _, highs, _ = _conv(klines)
    rv = _tsrank(vols, 10)
    rh = _tsrank(highs, 10)
    n = len(rv)
    out = [None] * n
    for i in range(14, n):          # 10(rank窗口) + 5(corr窗) - 1
        xs, ys = rv[i - 4:i + 1], rh[i - 4:i + 1]
        if any(x is None for x in xs) or any(y is None for y in ys):
            continue
        mx, my = sum(xs) / 5, sum(ys) / 5
        num = sum((xs[j] - mx) * (ys[j] - my) for j in range(5))
        dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
        dy = math.sqrt(sum((y - my) ** 2 for y in ys))
        c = num / (dx * dy) if dx > 0 and dy > 0 else 0.0
        out[i] = 1.0 if c > 0 else (-1.0 if c < 0 else 0.0)
    return out


def _g_hl_vwap(closes, klines):
    """GTJA Alpha#41：(high*low)^0.5 - vwap（筹码中位价 vs 均价差，归一化到 close）"""
    n = len(klines)
    vw = ind.vwap(klines)
    out = [None] * n
    for i in range(n):
        hi, lo, c = klines[i]["high"], klines[i]["low"], closes[i]
        if vw[i] and c > 0 and hi > 0 and lo > 0:
            out[i] = (math.sqrt(hi * lo) - vw[i]) / c
    return out


def _g_vwap_rank_ratio(closes, klines):
    """GTJA Alpha#47 时序化：tsrank(vwap-close,20)/tsrank(vwap+close,20)
    （价相对成本的时序地位比，>1＝近期价格地位抬升）"""
    n = len(klines)
    vw = ind.vwap(klines)
    diff = [(vw[i] - closes[i]) if vw[i] else None for i in range(n)]
    sm = [(vw[i] + closes[i]) if vw[i] else None for i in range(n)]
    rd = _tsrank(diff, 20)
    rs = _tsrank(sm, 20)
    out = [None] * n
    for i in range(n):
        if rd[i] is not None and rs[i] and rs[i] > 0:
            out[i] = rd[i] / rs[i]
    return out


def _g_vrh_mean3o10(closes, klines):
    """GTJA Alpha#102 近似：sma(vol*(high-low),3)/sma(vol*(high-low),10)-1
    （量幅积短均比，捕捉放量+振幅放大）"""
    _, vols, _, highs, lows = _conv(klines)
    n = len(klines)
    vh = [vols[i] * (highs[i] - lows[i]) for i in range(n)]
    m3, m10 = ind.sma(vh, 3), ind.sma(vh, 10)
    return [m3[i] / m10[i] - 1 if m10[i] else None for i in range(n)]


def _g_low_dev20(closes, klines):
    """GTJA Alpha#54 近似：(close - min(low,20))/min(low,20)（自低点反弹幅度）"""
    _, _, _, _, lows = _conv(klines)
    lm = ind.rolling_min(lows, 20)
    return [(closes[i] - lm[i]) / lm[i] if lm[i] else None for i in range(len(closes))]


def _g_amt_ret_corr(closes, klines):
    """量额价一致10：corr(amount, close, 10)（成交额与价格同向度，正=量价齐升）"""
    _, _, amts, _, _ = _conv(klines)
    return _roll_corr(amts, closes, 10)


def _g_vol_mom10_signed(closes, klines):
    """GTJA Alpha#16 近似：-tsrank(corr(tsrank(high,20), tsrank(vol,20), 5), 10)
    （高位放量反转信号：量价集中高 → 负值）"""
    _, vols, _, highs, _ = _conv(klines)
    rh = _tsrank(highs, 20)
    rv = _tsrank(vols, 20)
    c = _roll_corr([v if v is not None else 0.0 for v in rh],
                   [v if v is not None else 0.0 for v in rv], 5)
    tr = _tsrank(c, 10)
    return [-x if x is not None else None for x in tr]


# ==================== 注册表 ====================
GTJA_FACTORS = {
    "VOL偏离20": (_g_vol_dev20, "vol/sma(vol,20)-1（量能位置）"),
    "VOL变异20": (_g_vol_cv20, "std(vol,20)/sma(vol,20)（量能稳定度）"),
    "VOL均线5比20": (_g_vol_ma5o20, "sma(vol,5)/sma(vol,20)-1"),
    "AMT偏离20": (_g_amt_dev20, "amount/sma(amount,20)-1（换手放大近似）"),
    "AMT均线5比20": (_g_amt_ma5o20, "sma(amount,5)/sma(amount,20)-1"),
    "VOL-Z20": (_g_vol_z20, "(vol-sma(vol,20))/std(vol,20)"),
    "AMT-Z20": (_g_amt_z20, "(amount-sma(amount,20))/std(amount,20)"),
    "量价秩相关5": (_g_vp_rank_corr5, "GTJA#10时序化 corr(tsrank(vol,20),tsrank(close,20),5)"),
    "量高秩相关5": (_g_vh_rank_corr5, "GTJA#30时序化 sign(corr(tsrank(vol,10),tsrank(high,10),5))"),
    "高低中价VWAP差": (_g_hl_vwap, "GTJA#41 (sqrt(high*low)-vwap)/close"),
    "价VWAP秩比": (_g_vwap_rank_ratio, "GTJA#47时序化 tsrank(vwap-close,20)/tsrank(vwap+close,20)"),
    "量幅积比3比10": (_g_vrh_mean3o10, "GTJA#102近似 sma(vol*(high-low),3)/sma(vol*(high-low),10)-1"),
    "低点偏离20": (_g_low_dev20, "GTJA#54近似 (close-minlow20)/minlow20"),
    "量额价相关10": (_g_amt_ret_corr, "corr(amount,close,10)（量额价同向度）"),
    "高位放量反转": (_g_vol_mom10_signed, "GTJA#16近似 -tsrank(corr(tsrank(high,20),tsrank(vol,20),5),10)"),
}

# 与现有因子注册表合并（factor.py 在其导入后调用 register_into_factor）
def register_into_factor():
    """把 GTJA 因子注册进 factor.FACTORS / FACTOR_DESC（幂等；不改动现有因子）"""
    import app.factor as fmod
    for name, (func, desc) in GTJA_FACTORS.items():
        fmod.FACTORS[name] = (func, True)          # needs_klines=True：签名 (closes, klines)
        fmod.FACTOR_DESC[name] = desc