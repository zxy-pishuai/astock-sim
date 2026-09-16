# -*- coding: utf-8 -*-
"""Alpha158-style 特征构建（qlib Alpha158 特征子集，pandas 实现）
说明：qlib 官方 Alpha158 依赖 bundle 格式数据集；本模块按 Alpha158 的
特征族（KBAR：MA/STD/BETA/RSQR/RESI/MAX/HIGH/MIN/LOW/QTLU/QTLD/RANK/IMAX/IMIN/IMXD/CMRA；
VOL 同族 + 量价交叉相关）用 pandas 在日K面板上等价实现，供 LightGBM 训练。
全部为单股时序特征（同 qlib 单股切片语义）；特征在 t 日只用 <=t 数据（PIT 安全）。
"""
import numpy as np
import pandas as pd

WINDOWS = [5, 10, 20, 30, 60]


def build_features(df):
    """df: 单股日K DataFrame（列: date,open,high,low,close,volume,amount，按 date 升序）
    返回 (features_df, label_series)：features 索引与 df 对齐，label=未来5日收益。
    """
    df = df.sort_values("date").reset_index(drop=True)
    close = df["close"].astype(float)
    vol = df["volume"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    amt = df["amount"].astype(float)
    ret = close.pct_change()
    feats = pd.DataFrame(index=df.index)
    feats["ret1"] = ret
    feats["close"] = close
    feats["log_vol"] = np.log1p(vol)
    feats["log_amt"] = np.log1p(amt)
    feats["amplitude"] = (high - low) / close.replace(0, np.nan)
    feats["upper_shadow"] = (high - np.maximum(close, df["open"].astype(float))) / close.replace(0, np.nan)
    feats["lower_shadow"] = (np.minimum(close, df["open"].astype(float)) - low) / close.replace(0, np.nan)
    # KBAR 族（价格）
    for w in WINDOWS:
        feats[f"MA{w}"] = close.rolling(w).mean() / close - 1.0
        feats[f"STD{w}"] = ret.rolling(w).std()
        feats[f"MAX{w}"] = close / high.rolling(w).max() - 1.0
        feats[f"MIN{w}"] = close / low.rolling(w).min() - 1.0
        feats[f"RANK{w}"] = _rolling_rank_pct(close, w)
        feats[f"RESI{w}"] = _rolling_resid(close, w)
        feats[f"BETA{w}"] = _rolling_beta(close, w)
        feats[f"RSQR{w}"] = _rolling_rsqr(close, w)
    # VOL 族（量）
    for w in WINDOWS:
        vma = vol.rolling(w).mean()
        vstd = vol.rolling(w).std()
        feats[f"VOLRATIO{w}"] = vol / vma.replace(0, np.nan)
        feats[f"VOLZ{w}"] = (vol - vma) / vstd.replace(0, np.nan)
        feats[f"VOLRANK{w}"] = _rolling_rank_pct(vol, w)
        feats[f"AMTRATIO{w}"] = amt / amt.rolling(w).mean().replace(0, np.nan)
        feats[f"CORR_CV{w}"] = close.rolling(w).corr(vol)
        feats[f"CORR_RV{w}"] = ret.rolling(w).corr(vol)
        feats[f"CORR_RA{w}"] = ret.rolling(w).corr(amt)
    # 量价交叉
    obv = (np.sign(ret) * vol).cumsum()
    feats["OBVSLOPE10"] = obv.diff(10) / 1e7
    feats["VRETCOEF5"] = ret.rolling(5).corr(vol.rolling(5).mean())
    # 累计收益范围 CMRA(20)
    feats["CMRA20"] = close.rolling(20).max() / close.rolling(20).min() - 1.0
    # 标签：未来 5 日收益（最后 5 行为 NaN）
    label = close.shift(-5) / close - 1.0
    return feats, label


def _rolling_rank_pct(s, w):
    """滚动窗口内当前值分位（0~1）"""
    def _f(x):
        x = x[~np.isnan(x)]
        if len(x) < 2:
            return 0.5
        v = x[-1]
        return float((x < v).sum() / (len(x) - 1))
    return s.rolling(w, min_periods=2).apply(_f, raw=True)


def _rolling_resid(s, w):
    """滚动线性拟合残差（价格对时间的 OLS 残差，归一化到价格）"""
    def _f(x):
        x = x[~np.isnan(x)]
        if len(x) < w or np.std(x) == 0:
            return np.nan
        t = np.arange(len(x), dtype=float)
        a, b = np.polyfit(t, x, 1)
        resid = x - (a * t + b)
        return float(resid[-1] / x[-1])
    return s.rolling(w).apply(_f, raw=True)


def _rolling_beta(s, w):
    def _f(x):
        x = x[~np.isnan(x)]
        if len(x) < w:
            return np.nan
        t = np.arange(len(x), dtype=float)
        if np.std(t) == 0 or np.std(x) == 0:
            return np.nan
        return float(np.cov(t, x)[0, 1] / np.var(t))
    return s.rolling(w).apply(_f, raw=True)


def _rolling_rsqr(s, w):
    def _f(x):
        x = x[~np.isnan(x)]
        if len(x) < w or np.std(x) == 0:
            return np.nan
        t = np.arange(len(x), dtype=float)
        c = np.corrcoef(t, x)[0, 1]
        return float(c * c)
    return s.rolling(w).apply(_f, raw=True)