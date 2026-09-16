# -*- coding: utf-8 -*-
"""C1｜筹码分布（CYQ）本地重建引擎
验收批文选型（C0 94/100 签发）：
- 分布假设：三角分布主口径（峰=当日均价 amount/volume，面积归一，峰高 2/d）；均匀作对照
- 衰减式：D_{i+1}=D_i×(1−effective)+A_i，effective=min(换手率比例×系数,0.95)，系数=1
- 除权/方言：qfq 价上直接算；|chg|>band+2pp 断层日只衰减不叠加并标 seam_flag；指标比值化
- 换手率主源：新浪 stock_zh_a_daily（东财 push2his 网络不可达→降级，见报告）；新浪 close 用于
  逐日复权因子修正 avg（qfq 价坐标系下峰位=真实均价×复权因子）
- 窗口：缺省 210 交易日（指标本身全序列递推，输出全历史）；价格网格 1% 相对步长
输出：data/cyq_cache/{code}.parquet（date × 6 指标 + turnover_src + seam_flag）
CLI：--codes / --limit / --uniform / --start / --top50
"""
import os, sys, math, argparse, sqlite3, time
import numpy as np
import pandas as pd

ROOT = r"C:\Users\26838\A股模拟盘"
SNAP_URI = "file:C:/Users/26838/A股模拟盘/data/snapshots/2026-09-02/market.db?mode=ro&immutable=1"
TURNOVER = os.path.join(ROOT, "data", "cyq_turnover.parquet")
CACHE = os.path.join(ROOT, "data", "cyq_cache")
os.makedirs(CACHE, exist_ok=True)

GRID_STEP = 1.01  # 1% 相对步长


def band_of(code):
    return 0.097 if code.startswith(("60", "00")) else 0.194


def build_grid(low_min, high_max):
    """等比 1% 网格。返回节点价数组（升序），节点 i 代表价格 grid[i]。"""
    if high_max <= low_min:
        low_min, high_max = low_min * 0.99, high_max * 1.01
    n = int(math.ceil(math.log(high_max / low_min) / math.log(GRID_STEP))) + 2
    n = max(n, 4)
    return low_min * GRID_STEP ** np.arange(n)


def tri_cdf(x, lo, pk, hi):
    """三角分布 CDF（面积归一=1，峰高 2/d）。x 为数组。"""
    x = np.clip(x, lo, hi)
    d = hi - lo
    if d <= 1e-12:
        return np.where(x >= hi, 1.0, 0.0)
    pk = min(max(pk, lo), hi)
    left = pk - lo
    right = hi - pk
    out = np.zeros_like(x, dtype=float)
    m_left = x <= pk
    m_right = x > pk
    if left > 1e-12:
        out[m_left] = (x[m_left] - lo) ** 2 / (d * left)
    else:
        out[m_left] = 0.0
    if right > 1e-12:
        out[m_right] = 1.0 - (hi - x[m_right]) ** 2 / (d * right)
    else:
        out[m_right] = 1.0
    return out


def tri_bin_weights(grid, lo, pk, hi):
    """三角分布落在每个网格区间 [grid[i-1],grid[i]] 的积分（面积归一，总和=1）。"""
    cdf = tri_cdf(grid, lo, pk, hi)
    w = np.diff(cdf)  # len = n-1
    s = w.sum()
    if s > 1e-12:
        w = w / s
    return w


def uniform_bin_weights(grid, lo, hi):
    """均匀分布落在每个网格区间的积分。"""
    cdf = np.clip(grid, lo, hi)
    if hi > lo:
        cdf = (cdf - lo) / (hi - lo)
    else:
        cdf = (cdf >= hi).astype(float)
    w = np.diff(cdf)
    s = w.sum()
    if s > 1e-12:
        w = w / s
    return w


def cost_pct(D, grid, pct):
    """累计分布达 pct% 的成本价（线性插值）。pct ∈ [0,100]。"""
    cum = np.cumsum(D)
    target = pct / 100.0
    if target <= 0:
        return grid[0]
    if target >= 1:
        return grid[-1]
    idx = np.searchsorted(cum, target)
    idx = min(max(idx, 0), len(D) - 1)
    if idx == 0:
        return grid[0]
    c0, c1 = cum[idx - 1], cum[idx]
    if c1 <= c0:
        return grid[idx]
    frac = (target - c0) / (c1 - c0)
    return grid[idx - 1] + frac * (grid[idx] - grid[idx - 1])


def local_maxima_idx(D):
    """局部极大索引（比两侧都大）。"""
    if len(D) < 3:
        return []
    idx = []
    for i in range(1, len(D) - 1):
        if D[i] >= D[i - 1] and D[i] > D[i + 1]:
            idx.append(i)
    return idx


def compute_indicators(D, grid, close, prev_winner):
    """计算当日 6 指标。返回 dict。"""
    out = {}
    winner = 100.0 * D[grid <= close].sum()
    out["winner_pct"] = round(winner, 3)
    c5 = cost_pct(D, grid, 5)
    c95 = cost_pct(D, grid, 95)
    mid = (c95 + c5) / 2.0
    out["conc90"] = round((c95 - c5) / mid, 4) if mid > 1e-12 else np.nan
    pk_i = int(np.argmax(D))
    peak_price = grid[pk_i]
    out["peak_dist_pct"] = round((peak_price / close - 1.0) * 100.0, 3) if close > 0 else np.nan
    out["peak_density"] = round(float(D[pk_i]), 4)
    lms = local_maxima_idx(D)
    if len(lms) >= 2:
        vals = sorted([(D[i], grid[i]) for i in lms], reverse=True)
        p1, p2 = vals[0][1], vals[1][1]
        out["dual_peak_gap"] = round(abs(p1 - p2) / close * 100.0, 3) if close > 0 else np.nan
    else:
        out["dual_peak_gap"] = np.nan
    out["profit_trend5"] = round(winner - prev_winner, 3) if prev_winner is not None else np.nan
    return out


def build_one(code, kdf, tdf, uniform=False, start="2018-01-01"):
    """单票重建。kdf=快照 kline day；tdf=换手率缓存。返回 (df, meta)。"""
    k = kdf.sort_values("date").reset_index(drop=True)
    k = k[k["date"] >= start].reset_index(drop=True)
    if len(k) == 0:
        return None, None
    band = band_of(code)
    if tdf is not None and len(tdf):
        t2 = tdf.copy()
        t2 = t2.drop(columns=["code"], errors="ignore")
        t2["date"] = t2["date"].astype(str)  # 统一为 "YYYY-MM-DD"（k 的 date 为 str）
        if "close_real" not in t2.columns and "close" in t2.columns:
            t2 = t2.rename(columns={"close": "close_real"})
        m = k.merge(t2, on="date", how="left")
    else:
        m = k.copy()
        m["turnover"] = np.nan
        m["outstanding_share"] = np.nan
        m["close_real"] = np.nan
        m["turnover_src"] = "missing"
    m["turnover"] = pd.to_numeric(m["turnover"], errors="coerce")
    m["outstanding_share"] = pd.to_numeric(m["outstanding_share"], errors="coerce")
    m["close_real"] = pd.to_numeric(m["close_real"], errors="coerce")
    # 兜底：新浪换手率缺失但有历史流通股本 → volume/流通股本 自算（批文 mootdx 兜底的增强版）
    self_mask = m["turnover"].isna() & m["outstanding_share"].gt(0) & m["volume"].gt(0)
    m.loc[self_mask, "turnover"] = m.loc[self_mask, "volume"] / m.loc[self_mask, "outstanding_share"]
    turn_ratio = m["turnover"].to_numpy(dtype=float)
    close_qfq = m["close"].to_numpy(dtype=float)
    close_real = m["close_real"].to_numpy(dtype=float)
    low = m["low"].to_numpy(dtype=float)
    high = m["high"].to_numpy(dtype=float)
    amount = m["amount"].to_numpy(dtype=float)
    volume = m["volume"].to_numpy(dtype=float)
    dates = m["date"].tolist()
    f = np.ones_like(close_qfq)
    mask = (close_real > 0) & np.isfinite(close_real)
    f[mask] = close_qfq[mask] / close_real[mask]
    effective = np.minimum(turn_ratio * 1.0, 0.95)
    effective[np.isnan(effective)] = 0.0
    lo_all = np.nanmin(low)
    hi_all = np.nanmax(high)
    grid = build_grid(lo_all, hi_all)
    D = np.zeros(len(grid), dtype=float)
    D[len(grid) // 2] = 1.0
    rows = []
    prev_winner = None
    n_seam = 0
    n_listed5 = 0
    for i in range(len(dates)):
        eff = effective[i]
        if i < 5:  # 新股前 5 交易日豁免
            n_listed5 += 1
            rows.append({"date": dates[i], "winner_pct": np.nan, "conc90": np.nan,
                         "peak_dist_pct": np.nan, "peak_density": np.nan,
                         "dual_peak_gap": np.nan, "profit_trend5": np.nan,
                         "turnover_src": "listed5", "seam_flag": 0})
            continue
        is_seam = 0
        if i > 0:
            chg = close_qfq[i] / close_qfq[i - 1] - 1.0 if close_qfq[i - 1] > 0 else 0.0
            if abs(chg) > band + 0.02:
                is_seam = 1
        one_word = eff < 0.001
        lo_i, hi_i = low[i], high[i]
        if not (np.isfinite(lo_i) and np.isfinite(hi_i) and hi_i > lo_i):
            lo_i, hi_i = grid[0], grid[-1]
        if is_seam or one_word:
            D = D * (1.0 - eff)
            if is_seam:
                n_seam += 1
        else:
            avg_real = amount[i] / volume[i] if volume[i] > 0 else close_qfq[i]
            avg_qfq = avg_real * f[i]
            avg_qfq = min(max(avg_qfq, lo_i), hi_i)
            if uniform:
                w = uniform_bin_weights(grid, lo_i, hi_i)
            else:
                w = tri_bin_weights(grid, lo_i, avg_qfq, hi_i)
            D = D * (1.0 - eff)
            D[: len(w)] += eff * w
            s = D.sum()
            if s > 1e-12:
                D = D / s
        ind = compute_indicators(D, grid, close_qfq[i], prev_winner)
        if mask[i] and np.isfinite(turn_ratio[i]) and turn_ratio[i] > 0:
            src = "sina"
        elif self_mask[i]:
            src = "self"
        else:
            src = "missing"
        rows.append({"date": dates[i], **ind, "turnover_src": src, "seam_flag": is_seam})
        prev_winner = ind["winner_pct"]
    out = pd.DataFrame(rows)
    meta = {"code": code, "n_days": len(out), "n_seam": n_seam, "n_listed5": n_listed5,
            "grid_nodes": len(grid), "dist": "uniform" if uniform else "triangle",
            "turnover_src": "sina", "start": start}
    return out, meta


def load_codes(limit=None, top50=None, codes=None):
    conn = sqlite3.connect(SNAP_URI, uri=True)
    if codes:
        cl = [x.strip() for x in codes.split(",") if x.strip()]
    else:
        df = pd.read_sql_query("SELECT DISTINCT code FROM kline WHERE period='day'", conn)
        cl = sorted(x for x in df["code"] if len(x) == 6 and x[:2] in ("00", "30", "60", "68"))
    conn.close()
    if top50:
        t = pd.read_csv(top50, dtype={"code": str})
        cl = [x for x in t["code"].tolist() if x in cl]
    if limit:
        cl = cl[:limit]
    return cl

def load_kline(code):
    conn = sqlite3.connect(SNAP_URI, uri=True)
    df = pd.read_sql_query(
        "SELECT date,open,high,low,close,volume,amount FROM kline WHERE code=? AND period='day' ORDER BY date",
        conn, params=(code,))
    conn.close()
    return df


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", default=None)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--uniform", action="store_true")
    ap.add_argument("--start", default="2018-01-01")
    ap.add_argument("--top50", default=None)
    ap.add_argument("--meta-out", default=None)
    args = ap.parse_args()
    codes = load_codes(limit=args.limit, top50=args.top50, codes=args.codes)
    print(f"共 {len(codes)} 票，dist={'uniform' if args.uniform else 'triangle'}", flush=True)
    tdf_all = None
    if os.path.exists(TURNOVER):
        tdf_all = pd.read_parquet(TURNOVER)
        print("换手率缓存:", len(tdf_all), "行", tdf_all["code"].nunique(), "票", flush=True)
    metas = []
    t0 = time.time()
    for j, code in enumerate(codes):
        k = load_kline(code)
        tdf = tdf_all[tdf_all["code"] == code] if tdf_all is not None else None
        out, meta = build_one(code, k, tdf, uniform=args.uniform, start=args.start)
        if out is not None:
            out.to_parquet(os.path.join(CACHE, f"{code}.parquet"), index=False)
            metas.append(meta)
        if (j + 1) % 200 == 0 or (j + 1) == len(codes):
            el = time.time() - t0
            print(f"[{j+1}/{len(codes)}] 耗时 {el:.1f}s", flush=True)
    print(f"完成 {len(metas)}/{len(codes)} 票，总耗时 {time.time()-t0:.1f}s", flush=True)
    if args.meta_out:
        pd.DataFrame(metas).to_json(args.meta_out, orient="records", force_ascii=False, indent=1)


if __name__ == "__main__":
    main()
