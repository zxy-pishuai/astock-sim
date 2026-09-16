# -*- coding: utf-8 -*-
"""★ Phase28-A：score_stock 打分因子 IC 审计（只读，不修改任何现有代码）

范式：tools/ml_ic_verify.py（逐月 Spearman IC / ICIR / 分组未来收益）。

样本池：data/bt_pool.json 前 500（排除 DATA_EXCLUDE_CODES，与回测口径一致）
标签：未来 5 日收益 fwd5 = close[t+5]/close[t] - 1（个股自身 bar 序列，
     与 ml_ic_verify 相同规则；特征只用 ≤t 数据——所有引用指标均为因果函数）
成分：scoring.score_stock 中"仅用 k 线历史即可计算"的全部 18 个二值触发成分，
     触发值=带符号权重（罚分项为负），与打板/业绩/ML 等非 K 线成分严格区分。

方法说明：
- 日频横截面 Spearman IC（成分分 vs fwd5）。成分为二值信号，用秩和法精确计算：
  同一日内 fwd5 的秩对所有成分相同，只需按组累积 (n0,S0,n1,S1) 即可还原
  含并列平均秩的 Spearman，内存 O(成分×天数)，可全历史全样本复跑。
- 月度聚合：月内日 IC 均值；ICIR = 月度IC均值/月度IC标准差(ddof=1)；
  分类：有效 ICIR≥0.3 / 弱 0≤ICIR<0.3 / 负 ICIR<0。
- 五分位单调性对二值信号退化为两组对照：另给 触发组/未触发组 平均 fwd5 价差
  （spread）与逐年价差，作为方向稳定性的佐证。
- 需要实时 quote / 外部接口 / 非 K 线数据的成分单独列清单，不硬算。

用法: python tools/factor_ic_audit.py [--start 2019-08-01]
输出: data/factor_ic_audit.json
"""
import argparse
import json
import os
import sqlite3
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from app import config as C
from app import indicators as ind

OUT_JSON = os.path.join(BASE, "data", "factor_ic_audit.json")
MIN_BARS = 60          # 与 score_stock 的 len(klines)<60 拦截一致
MIN_DAY_SAMPLES = 30   # 单日横截面最少样本
MIN_GROUP = 3          # 二值分组每组最少样本（秩相关退化保护）
FWD_N = 5

# 成分清单（key 与 config.SCORE_WEIGHTS 一致；条件与 scoring.score_stock 逐行对应）
KLINE_COMPONENTS = [
    ("volume_price", "量价齐升", "阳线且涨幅≥2% 且量比≥1.8"),
    ("breakout", "放量突破20日新高", "high≥20日最高 且 量比≥1.5"),
    ("ma_bullish", "均线多头", "MA5>MA10>MA20"),
    ("rsrs_bullish", "RSRS看涨", "RSRS>0.7"),
    ("macd_golden", "MACD金叉", "DIF 上穿 DEA"),
    ("rsi_oversold", "RSI超卖反弹", "前日RSI<30 且 今日RSI回升"),
    ("volume_shrink", "缩量回踩", "MA5>MA10 且 量比≤0.7 且 收盘≥MA10"),
    ("pullback_ma", "回踩20日线企稳", "0<收盘-MA20≤MA20×2% 且 昨收<今收"),
    ("ma_cross", "MA5/10金叉", "前日MA5≤MA10 且 今日MA5>MA10"),
    ("turtle_break", "海龟20日突破", "收盘≥20日最高×99.5%"),
    ("rsrs_accel", "RSRS加速", "RSRS今日>昨日>0"),
    ("penalty_stall", "高位放量滞涨(罚)", "涨幅>5% 且 量比>3 且 收盘<high×98%", ),
    ("penalty_overbought", "RSI超买(罚)", "RSI>70"),
    ("vp_corr", "量价相关良好", "近10日量价相关≥0.6"),
    ("vp_vol_slope", "量能递增", "近5日量能斜率>0"),
    ("vp_vwap", "站稳VWAP", "收盘>VWAP(累计)"),
    ("vp_surge", "放量启动", "量能突变≥2.0"),
    ("vp_divergence", "量价背离/负相关(罚)", "背离强度≥0.03 或 近10日量价相关<0"),
]
# 非 K 线成分（只列清单与定性说明，不参与 IC 计算）
NON_KLINE_COMPONENTS = [
    {"key": "earnings_signal", "name": "业绩事件加分", "weight": "见 earnings.py",
     "source": "库表 earnings（业绩公告，按公告日 PIT）",
     "note": "非 K 线数据源；本阶段审计范围限定 K 线成分，不硬算"},
    {"key": "ml_score_bonus", "name": "ML 选股分", "source": "data/ml_scores.json 文件",
     "note": "默认关闭(ML_SCORE_ENABLED=False)；依赖夜间 sidecar 文件，不可历史回算"},
    {"key": "ml_pred_bonus", "name": "ml_pred 加权分", "source": "库表 ml_pred",
     "note": "默认 weight=0 跳过；Phase25 已独立做过 IC 验证(ml_ic_verify)"},
    {"key": "volprice_signals", "name": "量价信号包", "source": "app/volprice.py",
     "note": "默认关闭(VOLPRICE_SIGNALS_ENABLED=False)"},
    {"key": "gtja_factors", "name": "GTJA191 因子", "source": "app/factor.py",
     "note": "默认关闭(GTJA_FACTORS_ENABLED=False)"},
]


def build_fwd_and_ranks(codes, start):
    """fwd5 标签 + 每日横截面秩（并列平均秩）+ Σr²。返回 (fwd, ranks, sum_r2)"""
    conn = sqlite3.connect("file:%s?mode=ro" % C.DB_FILE, uri=True, timeout=30)
    ph = ",".join("?" * len(codes))
    rows = conn.execute(
        "SELECT code, date, close FROM kline WHERE period='day' AND date>=? "
        "AND close IS NOT NULL AND close > 0 AND code IN (%s) "
        "ORDER BY code, date" % ph, tuple([start] + codes)).fetchall()
    conn.close()
    fwd, last_code, seq = {}, None, []
    def flush(code, seq):
        for i in range(len(seq) - FWD_N):
            c0, d0 = seq[i]
            c5 = seq[i + FWD_N][0]
            if c0 and c0 > 0 and c5:
                fwd.setdefault(d0, {})[code] = c5 / c0 - 1.0
    for code, d, c in rows:
        if code != last_code:
            if last_code is not None:
                flush(last_code, seq)
            last_code, seq = code, []
        seq.append((c, d))
    if last_code is not None:
        flush(last_code, seq)
    # 每日秩
    ranks, sum_r2 = {}, {}
    for d, m in fwd.items():
        items = sorted(m.items(), key=lambda kv: kv[1])
        rk = {}
        s2 = 0.0
        i = 0
        n = len(items)
        while i < n:
            j = i
            while j + 1 < n and items[j + 1][1] == items[i][1]:
                j += 1
            avg = (i + j) / 2.0 + 1.0
            for k in range(i, j + 1):
                rk[items[k][0]] = avg
                s2 += avg * avg
            i = j + 1
        ranks[d] = rk
        sum_r2[d] = s2
    return fwd, ranks, sum_r2


def load_klines(codes):
    ph = ",".join("?" * len(codes))
    conn = sqlite3.connect("file:%s?mode=ro" % C.DB_FILE, uri=True, timeout=30)
    rows = conn.execute(
        "SELECT code, date, open, high, low, close, volume FROM kline "
        "WHERE period='day' AND close IS NOT NULL AND close > 0 AND code IN (%s) "
        "ORDER BY code, date" % ph, codes).fetchall()
    conn.close()
    by = {}
    for c, d, o, h, l, cl, v in rows:
        by.setdefault(c, []).append({"date": d, "open": o or cl, "high": h or cl,
                                     "low": l or cl, "close": cl, "volume": v or 0})
    return by


def precompute(kl):
    """每股一次计算全部指标序列（全部为因果函数；index=i 取值 == 截止 i 的前缀取值）"""
    closes = [k["close"] for k in kl]
    volumes = [k["volume"] for k in kl]
    highs = [k["high"] for k in kl]
    opens = [k["open"] for k in kl]
    dif, dea, _hist = ind.macd(closes, C.MACD_FAST, C.MACD_SLOW, C.MACD_SIGNAL)
    return {
        "closes": closes, "volumes": volumes, "highs": highs, "opens": opens,
        "ma5": ind.sma(closes, 5), "ma10": ind.sma(closes, 10),
        "ma20": ind.sma(closes, 20), "vma5": ind.sma(volumes, 5),
        "dif": dif, "dea": dea,
        "rsi": ind.rsi(closes, C.RSI_PERIOD),
        "h20": ind.rolling_max(highs, 20),
        "rs": ind.rsrs(kl, C.RSRS_WINDOW, C.RSRS_ZSCORE),
        "vwap": ind.vwap(kl),
        "vp_corr": ind.price_volume_corr(closes, volumes, 10),
        "vp_slope": ind.volume_slope(volumes, 5),
        "vp_surge": ind.volume_surge(volumes, 5),
        "vp_div": ind.price_vol_divergence(closes, volumes, 10, 10),
    }


def component_values(P, i):
    """复刻 scoring.score_stock 的 18 个 K 线成分在 bar i 的带符号触发（0/±w）。"""
    W = C.SCORE_WEIGHTS
    ma5, ma10, ma20, vma5 = P["ma5"], P["ma10"], P["ma20"], P["vma5"]
    dif, dea, rsi = P["dif"], P["dea"], P["rsi"]
    h20, rs, vwap = P["h20"], P["rs"], P["vwap"]
    vp_corr, vp_slope = P["vp_corr"], P["vp_slope"]
    vp_surge, vp_div = P["vp_surge"], P["vp_div"]
    out = {}

    def put(key, fired):
        w = W[key]
        out[key] = (abs(w) if fired else 0) * (1 if w > 0 else -1)

    closes, opens, highs, volumes = P["closes"], P["opens"], P["highs"], P["volumes"]
    c, o, h = closes[i], opens[i], highs[i]
    prev = closes[i - 1] if i > 0 else c
    pct = (c - prev) / prev if prev else 0.0
    vr = volumes[i] / vma5[i] if vma5[i] else 1.0

    put("volume_price", bool(c > o and pct >= 0.02 and vr >= C.VOL_RATIO_HIGH))
    put("breakout", bool(h20[i] and h >= h20[i] and vr >= C.VOL_RATIO_BREAKOUT))
    put("ma_bullish", bool(ma5[i] and ma10[i] and ma20[i] and ma5[i] > ma10[i] > ma20[i]))
    put("rsrs_bullish", bool(rs[i] is not None and rs[i] > C.RSRS_BUY_THRESHOLD))
    put("macd_golden", bool(dif[i] and dea[i] and dif[i - 1] and dea[i - 1]
                            and dif[i - 1] <= dea[i - 1] and dif[i] > dea[i]))
    put("rsi_oversold", bool(rsi[i] and rsi[i - 1]
                             and rsi[i - 1] < C.RSI_OVERSOLD and rsi[i] > rsi[i - 1]))
    put("volume_shrink", bool(ma5[i] and ma10[i] and ma5[i] > ma10[i]
                              and vr <= C.VOLUME_SHRINK_RATIO and c >= ma10[i]))
    put("pullback_ma", bool(ma20[i] and 0 < c - ma20[i] <= ma20[i] * 0.02 and prev < c))
    put("ma_cross", bool(ma5[i] and ma10[i] and ma5[i - 1] and ma10[i - 1]
                         and ma5[i - 1] <= ma10[i - 1] and ma5[i] > ma10[i]))
    put("turtle_break", bool(h20[i] and c >= h20[i] * 0.995))
    put("rsrs_accel", bool(rs[i] is not None and rs[i - 1] is not None
                           and rs[i] > rs[i - 1] > 0))
    put("penalty_stall", bool(pct > 0.05 and vr > 3.0 and c < h * 0.98))
    put("penalty_overbought", bool(rsi[i] and rsi[i] > C.RSI_OVERBOUGHT))
    put("vp_corr", bool(vp_corr[i] is not None and vp_corr[i] >= C.VP_CORR_GOOD))
    put("vp_vol_slope", bool(vp_slope[i] is not None and vp_slope[i] > C.VP_VOL_SLOPE_GOOD))
    put("vp_vwap", bool(vwap[i] and c > vwap[i]))
    put("vp_surge", bool(vp_surge[i] is not None and vp_surge[i] >= C.VP_SURGE_GOOD))
    put("vp_divergence",
        bool((vp_div[i] is not None and vp_div[i] > 0 and vp_div[i] >= C.VP_DIVERGENCE_BAD)
             or (vp_corr[i] is not None and vp_corr[i] < C.VP_CORR_BAD)))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2019-08-01")
    a = ap.parse_args()
    t0 = time.time()
    pool = json.load(open(os.path.join(BASE, "data", "bt_pool.json"), encoding="utf-8"))
    ex = set(getattr(C, "DATA_EXCLUDE_CODES", None) or [])
    codes = [c for c in pool["codes"][:500] if c not in ex]
    print("池: %d 只 | 标签: 未来%d日收益 | 起点: %s" % (len(codes), FWD_N, a.start), flush=True)

    print("构建 fwd%d 标签与每日秩..." % FWD_N, flush=True)
    fwd, ranks, sum_r2 = build_fwd_and_ranks(codes, a.start)
    print("  有标签股票日: %d（%d 个交易日，%.0fs）" % (
        sum(len(m) for m in fwd.values()), len(fwd), time.time() - t0), flush=True)

    print("加载 K 线并计算成分..." , flush=True)
    kls = load_klines(codes)
    keys = [k[0] for k in KLINE_COMPONENTS]
    acc = {k: {} for k in keys}          # key -> date -> [n0, S0, n1, S1]
    pooled = {k: [0, 0.0, 0, 0.0] for k in keys}   # key -> [n0, sum_r0, n1, sum_r1]
    year_spread = {k: {} for k in keys}  # key -> year -> [n0,s0,n1,s1]
    nsamp = 0
    for code, kl in kls.items():
        n = len(kl)
        if n < MIN_BARS + FWD_N:
            continue
        P = precompute(kl)
        for i in range(MIN_BARS - 1, n - FWD_N):
            d = kl[i]["date"]
            fm = fwd.get(d)
            if not fm or code not in fm:
                continue
            vals = component_values(P, i)
            r = ranks[d][code]
            ret = fm[code]
            nsamp += 1
            yr = d[:4]
            for k in keys:
                v = vals[k]
                a4 = acc[k].get(d)
                if a4 is None:
                    a4 = acc[k][d] = [0, 0.0, 0, 0.0]
                p = pooled[k]
                ys = year_spread[k].setdefault(yr, [0, 0.0, 0, 0.0])
                if v == 0:
                    a4[0] += 1; a4[1] += r
                    p[0] += 1; p[1] += ret
                    ys[0] += 1; ys[1] += ret
                else:
                    a4[2] += 1; a4[3] += r
                    p[2] += 1; p[3] += ret
                    ys[2] += 1; ys[3] += ret
    print("成分扫描完成: %d 股票日（%.0fs）" % (nsamp, time.time() - t0), flush=True)

    # ---- 日频 IC → 月度聚合 ----
    def daily_ic(k, d):
        n0, S0, n1, S1 = acc[k][d]
        n = n0 + n1
        if n < MIN_DAY_SAMPLES or n0 < MIN_GROUP or n1 < MIN_GROUP:
            return None
        rxm = (n0 + 1) / 2.0
        rx1 = n0 + (n1 + 1) / 2.0
        mx = (n0 * rxm + n1 * rx1) / n
        my = (S0 + S1) / n
        rm0 = S0 / n0
        rm1 = S1 / n1
        cov = n0 * (rxm - mx) * (rm0 - my) + n1 * (rx1 - mx) * (rm1 - my)
        varx = n0 * (rxm - mx) ** 2 + n1 * (rx1 - mx) ** 2
        vary = sum_r2[d] - n * my * my
        if varx <= 0 or vary <= 0:
            return None
        return cov / (varx ** 0.5 * vary ** 0.5)

    results = []
    for key, name, desc in [(k[0], k[1], k[2]) for k in KLINE_COMPONENTS]:
        days = sorted(acc[key].keys())
        ics = []
        for d in days:
            ic = daily_ic(key, d)
            if ic is not None:
                ics.append((d, ic))
        monthly = {}
        for d, ic in ics:
            monthly.setdefault(d[:7], []).append(ic)
        m_ics = [sum(v) / len(v) for v in monthly.values()]
        n_pos = sum(1 for x in m_ics if x > 0)
        ic_mean = sum(m_ics) / len(m_ics) if m_ics else None
        ic_std = ((sum((x - ic_mean) ** 2 for x in m_ics) / max(len(m_ics) - 1, 1)) ** 0.5
                  if m_ics and len(m_ics) > 1 else None)
        icir = (ic_mean / ic_std) if ic_mean is not None and ic_std and ic_std > 0 else None
        n0, s0, n1, s1 = pooled[key]
        trig_ret = s1 / n1 if n1 else None
        non_ret = s0 / n0 if n0 else None
        spread = (trig_ret - non_ret) if (trig_ret is not None and non_ret is not None) else None
        ysp = {}
        for yr, (n_nont, s_nont, n_trig, s_trig) in sorted(year_spread[key].items()):
            ysp[yr] = {
                "trigger_rate": round(n_trig / max(1, n_nont + n_trig), 4),
                "spread_bp": round(((s_trig / n_trig - s_nont / n_nont)
                                    if n_nont and n_trig else 0) * 10000, 1)}
        cls = ("有效" if icir is not None and icir >= 0.3
               else "负" if icir is not None and icir < 0 else "弱")
        results.append({
            "key": key, "name": name, "trigger": desc,
            "weight": C.SCORE_WEIGHTS.get(key),
            "source": "kline",
            "months": len(m_ics), "daily_ic_days": len(ics),
            "monthly_ic_mean": round(ic_mean, 4) if ic_mean is not None else None,
            "monthly_ic_std": round(ic_std, 4) if ic_std is not None else None,
            "icir": round(icir, 3) if icir is not None else None,
            "ic_positive_month_ratio": round(n_pos / max(len(m_ics), 1), 3) if m_ics else None,
            "daily_ic_positive_ratio": round(sum(1 for _, x in ics if x > 0) / max(len(ics), 1), 3) if ics else None,
            "trigger_rate": round(n1 / max(1, n0 + n1), 4),
            "trig_fwd5_mean_bp": round(trig_ret * 10000, 1) if trig_ret is not None else None,
            "nontrig_fwd5_mean_bp": round(non_ret * 10000, 1) if non_ret is not None else None,
            "spread_bp": round(spread * 10000, 1) if spread is not None else None,
            "spread_by_year": ysp,
            "class": cls,
        })

    order = {"有效": 0, "弱": 1, "负": 2}
    results.sort(key=lambda x: (order[x["class"]], -(x["icir"] or -9)))
    payload = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "phase": "28A",
        "method": {
            "label": "未来5日收益 close[t+5]/close[t]-1（个股自身bar序列）",
            "feature_pit": "全部引用指标为因果函数（indicators.py 纯函数），t 日成分只用 ≤t 数据",
            "universe": "data/bt_pool.json[:500] 排除 DATA_EXCLUDE_CODES",
            "start": a.start,
            "ic": "日频横截面 Spearman（二值成分按秩和法精确还原含并列平均秩）",
            "aggregate": "月度IC=月内日IC均值；ICIR=月度均值/月度标准差(ddof=1)",
            "classes": "有效 ICIR>=0.3；弱 0<=ICIR<0.3；负 ICIR<0",
            "sample_stock_days": nsamp,
            "min_day_samples": MIN_DAY_SAMPLES,
        },
        "components": results,
        "non_kline_components": NON_KLINE_COMPONENTS,
        "summary_counts": {
            "valid": sum(1 for r in results if r["class"] == "有效"),
            "weak": sum(1 for r in results if r["class"] == "弱"),
            "negative": sum(1 for r in results if r["class"] == "负"),
        },
    }
    with open(OUT_JSON, "w", encoding="utf-8", newline="\n") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    print("\n=== 结果摘要（按分类排序）===")
    print("%-22s %-6s %-8s %-9s %-9s %-9s %s" % (
        "key", "class", "ICIR", "月IC均值", "IC>0占比", "spread(bp)", "触发率"))
    for r in results:
        print("%-22s %-6s %-8s %-9s %-9s %-9s %.3f" % (
            r["key"], r["class"], r["icir"], r["monthly_ic_mean"],
            r["ic_positive_month_ratio"], r["spread_bp"], r["trigger_rate"]))
    print("\n分类统计:", payload["summary_counts"])
    print("已写出 %s | 总耗时 %.0fs" % (OUT_JSON, time.time() - t0), flush=True)


if __name__ == "__main__":
    main()
