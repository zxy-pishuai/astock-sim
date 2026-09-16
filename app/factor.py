# -*- coding: utf-8 -*-
"""因子研究流水线（v3.6）—— 借鉴 qlib Alpha158 / 聚宽四步法 / WorldQuant Alpha101 流程
把 v3.4 之前的"固定权重评分"升级为可验证的因子研究体系：
  1. 因子库：每个因子是一个函数 klines/quote -> 因子值（indicators.py 扩展）
  2. IC 检验：因子值与未来 N 日收益的 Spearman 秩相关（IC、ICIR、胜率）
  3. 分层回测：按因子值分 5 层，各层未来收益对比（单调性 = 因子有效）
  4. 因子组合：多因子等权/IC 加权合成
纯标准库实现（手写 Spearman 秩相关）。
"""
import math

from . import config as C
from . import indicators as ind


def _rank(series):
    """手写秩（平均秩处理并列）"""
    n = len(series)
    idx = sorted(range(n), key=lambda i: series[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and series[idx[j + 1]] == series[idx[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[idx[k]] = avg
        i = j + 1
    return ranks


def spearman(x, y):
    """Spearman 秩相关系数（手写，处理并列）"""
    n = len(x)
    if n < 5:
        return None
    rx, ry = _rank(x), _rank(y)
    mx = sum(rx) / n
    my = sum(ry) / n
    sxy = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    sxx = sum((rx[i] - mx) ** 2 for i in range(n))
    syy = sum((ry[i] - my) ** 2 for i in range(n))
    if sxx <= 0 or syy <= 0:
        return None
    return sxy / math.sqrt(sxx * syy)


# ==================== 因子库（注册表） ====================
# 每个因子：函数(klines) -> list（与 klines 等长，前缀 None）
def _f_ma5(closes):
    return ind.sma(closes, 5)


def _f_ma20(closes):
    return ind.sma(closes, 20)


def _f_rsi(closes):
    return ind.rsi(closes, 14)


def _f_kdj_k(klines):
    k, _, _ = ind.kdj(klines)
    return k


def _f_atr(closes, klines):
    a = ind.atr(klines, 14)
    return a


def _f_mom10(closes):
    return ind.momentum(closes, 10)


def _f_mom20(closes):
    return ind.momentum(closes, 20)


def _f_vol20(closes):
    return ind.volatility(closes, 20)


def _f_obv(closes, klines):
    o = ind.obv(klines)
    n = len(o)
    if n < 21:
        return [None] * n
    sma = ind.sma([x if x is not None else 0.0 for x in o], 20)
    return sma


def _f_macd_hist(closes):
    _, _, hist = ind.macd(closes, C.MACD_FAST, C.MACD_SLOW, C.MACD_SIGNAL)
    return hist


def _f_amt5(closes):
    return ind.amt_change(closes, 5)


def _f_vol_ratio(closes, volumes):
    vr = []
    for i in range(len(closes)):
        if i >= 6:
            avg = sum(volumes[i - 5:i]) / 5
            vr.append(volumes[i] / avg if avg > 0 else 1.0)
        else:
            vr.append(None)
    return vr


def _f_rsrs(closes, klines):
    return ind.rsrs(klines, C.RSRS_WINDOW, C.RSRS_ZSCORE)


# ==================== 4.4：量价深度因子 ====================
def _f_vwap_dev(closes, klines):
    """VWAP 偏离：收盘 / VWAP - 1"""
    v = ind.vwap(klines)
    return [closes[i] / v[i] - 1 if v[i] else None for i in range(len(closes))]


def _f_vol_surge(closes, volumes):
    """量能突变：当日量 / 前5日均量"""
    return ind.volume_surge(volumes, 5)


def _f_vol_slope(closes, volumes):
    """量能斜率：5日量能线性趋势"""
    return ind.volume_slope(volumes, 5)


def _f_pv_corr(closes, volumes):
    """量价相关性：10日价格与量的相关"""
    return ind.price_volume_corr(closes, volumes, 10)


def _f_obv_slope(closes, klines):
    """OBV 斜率：10日资金流趋势"""
    return ind.obv_slope(klines, 10)


def _f_divergence(closes, volumes):
    """量价背离强度：价涨量缩/价跌量增"""
    return ind.price_vol_divergence(closes, volumes, 10, 10)


def _f_vma_cross(closes, volumes):
    """量能均线：量MA5 vs 量MA10 方向"""
    return ind.volume_ma_cross(volumes, 5, 10)


# ★ 4.4：成本筹码（量沉淀为成本结构）
def _f_chip_winner(closes, klines):
    """获利盘比例：成本低于现价的筹码占比（高=套牢少，多头健康）"""
    prof = ind.chip_profile(klines)
    return prof["winner"]


def _f_chip_conc(closes, klines):
    """筹码集中度：90%成本带宽度/均价（低=高度集中，主力控盘）"""
    prof = ind.chip_profile(klines)
    return prof["concentration"]


def _f_chip_dev(closes, klines):
    """成本偏离：现价/COST50-1（>0 站上平均成本，多数人盈利）"""
    prof = ind.chip_profile(klines)
    return prof["cost_dev"]


# 因子注册表：{name: (func, needs_klines)}
FACTORS = {
    "MA5偏离": (_f_ma5, False),
    "MA20偏离": (_f_ma20, False),
    "RSI14": (_f_rsi, False),
    "KDJ_K": (_f_kdj_k, True),
    "ATR14": (_f_atr, True),
    "动量10日": (_f_mom10, False),
    "动量20日": (_f_mom20, False),
    "波动率20": (_f_vol20, False),
    "OBV20均线": (_f_obv, True),
    "MACD柱": (_f_macd_hist, False),
    "5日涨幅": (_f_amt5, False),
    "量比5日": (_f_vol_ratio, True),
    "RSRS": (_f_rsrs, True),
    # ★ 4.4 量价深度因子
    "VWAP偏离": (_f_vwap_dev, True),
    "量能突变": (_f_vol_surge, True),
    "量能斜率": (_f_vol_slope, True),
    "量价相关": (_f_pv_corr, True),
    "OBV斜率": (_f_obv_slope, True),
    "量价背离": (_f_divergence, True),
    "量能均线": (_f_vma_cross, True),
    # ★ 4.4 成本筹码
    "获利盘": (_f_chip_winner, True),
    "筹码集中度": (_f_chip_conc, True),
    "成本偏离": (_f_chip_dev, True),
}

# 只依赖成交量序列的量能类因子（compute_factor 需传 volumes 而非 klines）
_VOL_ONLY_FACTORS = {"量能突变", "量能斜率", "量价相关", "量价背离", "量能均线"}

FACTOR_DESC = {
    "MA5偏离": "收盘/MA5-1（短期趋势偏离）",
    "MA20偏离": "收盘/MA20-1（中期趋势偏离）",
    "RSI14": "相对强弱指标",
    "KDJ_K": "随机指标 K 值",
    "ATR14": "平均真实波幅（波动性）",
    "动量10日": "10日涨跌幅",
    "动量20日": "20日涨跌幅",
    "波动率20": "20日年化波动率",
    "OBV20均线": "能量潮 20 日均线",
    "MACD柱": "MACD 柱状值",
    "5日涨幅": "5日累计涨跌幅",
    "量比5日": "当日量/前5日均量",
    "RSRS": "右偏标准分（趋势强度）",
    # ★ 4.4
    "VWAP偏离": "收盘偏离成交量加权均价",
    "量能突变": "当日量/前5日均量（放量倍数）",
    "量能斜率": "5日量能线性趋势（递增放量/递减缩量）",
    "量价相关": "10日价格与成交量相关性（正=量价齐升）",
    "OBV斜率": "OBV 10日斜率（资金持续流入/流出）",
    "量价背离": "价涨量缩/价跌量增背离强度",
    "量能均线": "量MA5 vs 量MA10（放量/缩量方向）",
    # ★ 4.4 成本筹码
    "获利盘": "成本低于现价的筹码占比（套牢少=健康）",
    "筹码集中度": "90%成本带宽/均价（低=主力控盘）",
    "成本偏离": "现价/COST50-1（站上平均成本=多数盈利）",
}


def compute_factor(name, klines):
    """计算单因子值序列（与 klines 等长）。返回 (values, ok)"""
    try:
        func, needs_klines = FACTORS[name]
        closes = [k["close"] for k in klines]
        volumes = [k.get("volume", 0) or 0 for k in klines]
        if name in _VOL_ONLY_FACTORS:
            vals = func(closes, volumes)  # 量能类因子只依赖成交量序列
        elif name == "量比5日":
            vals = _f_vol_ratio(closes, volumes)
        elif name == "RSRS":
            vals = func(closes, klines)
        elif name == "ATR14" or name == "OBV20均线":
            vals = func(closes, klines)
        elif name == "KDJ_K":
            vals = func(klines)
        elif needs_klines:
            vals = func(closes, klines)
        else:
            vals = func(closes)
        return vals, True
    except Exception:
        return [None] * len(klines), False


def factor_ic(name, klines_by_code, horizon=5, min_cross=None):
    """截面 IC：某因子值 vs 未来 N 日收益，逐日截面 Spearman 相关。
    klines_by_code: {code: [klines]}（各股票需对齐日期）。
    min_cross: 每日截面最小股票数（默认取股票数的一半，至少 3）。
    返回 {ic_mean, ic_std, icir, positive_ratio, samples}
    """
    if min_cross is None:
        min_cross = max(3, len(klines_by_code) // 2)
    # 收集每日截面
    daily = {}   # date -> [(factor_val, fwd_ret)]
    for code, kl in klines_by_code.items():
        if len(kl) < horizon + 30:
            continue
        closes = [k["close"] for k in kl]
        vals, ok = compute_factor(name, kl)
        if not ok:
            continue
        for i in range(len(kl) - horizon):
            if vals[i] is None or closes[i + horizon] <= 0 or closes[i] <= 0:
                continue
            fwd = closes[i + horizon] / closes[i] - 1
            d = kl[i]["date"]
            daily.setdefault(d, []).append((vals[i], fwd))
    ics = []
    for d, pairs in daily.items():
        if len(pairs) < min_cross:
            continue
        xs = [p[0] for p in pairs]
        ys = [p[1] for p in pairs]
        ic = spearman(xs, ys)
        if ic is not None:
            ics.append(ic)
    if len(ics) < 3:
        return {"name": name, "samples": 0, "ic_mean": None, "ic_std": None,
                "icir": None, "positive_ratio": None, "error": "样本不足"}
    mean = sum(ics) / len(ics)
    std = math.sqrt(sum((x - mean) ** 2 for x in ics) / len(ics)) if len(ics) > 1 else 0.0
    pos = sum(1 for x in ics if x > 0)
    return {
        "name": name, "samples": len(ics),
        "ic_mean": round(mean, 4),
        "ic_std": round(std, 4),
        "icir": round(mean / std, 4) if std > 0 else 0.0,
        "positive_ratio": round(pos / len(ics), 3),
        "horizon": horizon,
    }


def factor_layers(name, klines_by_code, horizon=5, layers=5):
    """分层回测：因子值分 N 层，各层未来收益均值（单调性=因子有效）。
    返回 [{layer, avg_ret, up_ratio, count}] 从第1层(最小)到第5层(最大)
    """
    pairs = []
    for code, kl in klines_by_code.items():
        if len(kl) < horizon + 30:
            continue
        closes = [k["close"] for k in kl]
        vals, ok = compute_factor(name, kl)
        if not ok:
            continue
        for i in range(len(kl) - horizon):
            if vals[i] is None or closes[i + horizon] <= 0 or closes[i] <= 0:
                continue
            pairs.append((vals[i], closes[i + horizon] / closes[i] - 1))
    if len(pairs) < layers * 20:
        return {"name": name, "layers": [], "error": "样本不足"}
    pairs.sort(key=lambda x: x[0])
    n = len(pairs)
    size = n // layers
    out = []
    for L in range(layers):
        seg = pairs[L * size:(L + 1) * size] if L < layers - 1 else pairs[L * size:]
        if not seg:
            continue
        rets = [s[1] for s in seg]
        up = sum(1 for r in rets if r > 0)
        out.append({
            "layer": L + 1,
            "avg_ret": round(sum(rets) / len(rets), 5),
            "up_ratio": round(up / len(rets), 3),
            "count": len(rets),
        })
    return {"name": name, "layers": out}


def factor_monotonicity(layers):
    """分层单调性打分：收益随层号单调递增程度（0~1）"""
    rets = [l["avg_ret"] for l in layers]
    if len(rets) < 3:
        return 0.0
    asc = sum(1 for i in range(1, len(rets)) if rets[i] > rets[i - 1])
    desc = sum(1 for i in range(1, len(rets)) if rets[i] < rets[i - 1])
    return max(asc, desc) / (len(rets) - 1)


def ic_weighted_composite(ic_results, factor_vals):
    """IC 加权因子合成：各因子值按 IC 均值加权归一化求和。
    factor_vals: {factor_name: [values]}（对齐）
    返回 [composite_value,...]
    """
    names = list(factor_vals.keys())
    if not names:
        return []
    n = len(factor_vals[names[0]])
    weights = {}
    for name in names:
        w = 0.0
        for r in ic_results:
            if r.get("name") == name and r.get("ic_mean") is not None:
                w = r["ic_mean"]
                break
        weights[name] = w
    total_w = sum(abs(w) for w in weights.values())
    if total_w <= 0:
        for name in names:
            weights[name] = 1.0 / len(names)
        total_w = 1.0
    # 归一化每个因子到 [0,1]，再按权重求和
    out = [0.0] * n
    for name in names:
        vals = factor_vals[name]
        valid = [v for v in vals if v is not None]
        if not valid:
            continue
        vmin, vmax = min(valid), max(valid)
        rng = vmax - vmin
        sign = 1.0 if weights[name] >= 0 else -1.0
        w = abs(weights[name]) / total_w
        for i in range(n):
            if vals[i] is not None and rng > 0:
                out[i] += sign * w * (vals[i] - vmin) / rng
    return out


# ★ 4.6：GTJA191 量价因子注册（阶段3；仅注册计算能力，不改变任何现有评分/回测行为）
#   因子列表与表达式见 app/factor_gtja.py；接入评分需 config.GTJA_FACTOR_WEIGHTS 且默认关闭
try:
    from . import factor_gtja as _fg
    _fg.register_into_factor()
except Exception:
    pass
