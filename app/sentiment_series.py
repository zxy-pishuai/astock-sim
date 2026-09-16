# -*- coding: utf-8 -*-
"""情绪历史序列（4.1）—— 用本地数据重建市场情绪历史，供 IC 检验与择时验证
涨停池接口仅保留约 15 个交易日历史，无法支撑 IC 检验。
本模块用本地全市场日K（或 494 只活跃股）重建每日涨停池，计算情绪序列：
  涨停家数 / 空间板 / 昨日涨停溢价 / 情绪分 → 检验其对次日市场的预测力（IC）。
"""
import sqlite3
import time
import threading

from . import config as C
from . import datafeed as df
from . import sentiment as senti

_lock = threading.Lock()
_mem = {}   # key -> (ts, data)


def detect_zt(kl, code):
    """严格涨停检测（前复权日K）：涨幅>=9.5%（主板）/19.5%（创业科创）。
    返回涨停日期列表（YYYY-MM-DD）。
    """
    lim = 19.5 if code.startswith(("30", "68")) else 9.5
    zt = []
    for i in range(1, len(kl)):
        pc, c = kl[i - 1]["close"], kl[i]["close"]
        if pc <= 0:
            continue
        pct = (c - pc) / pc * 100
        if pct >= lim - 0.3:
            zt.append((kl[i]["date"], round(pct, 2)))
    return zt


def rebuild_history(codes=None, days=400, verbose=False):
    """重建历史涨停池：{date: [涨停股票dict]}。
    codes: 股票池（默认优先用本地 day 缓存股票，其次 min5 活跃股，避免下载）。
    返回 {date: [{"code","name","pct"}]}，按日期升序。
    结果按 (codes,days) 缓存 30 分钟（API 重复调用秒回）。
    """
    cache_key = (tuple(codes or ["__auto__"]), days)
    hit = _mem.get(cache_key)
    if hit and time.time() - hit[0] < 1800:
        return hit[1]
    # 从 SQLite 取已缓存的日K股票（避免下载）
    if codes is None:
        try:
            conn = sqlite3.connect(C.DB_FILE, timeout=15)
            try:
                cur = conn.execute(
                    "SELECT DISTINCT code FROM kline WHERE period='day'")
                codes = [r[0] for r in cur.fetchall()]
            finally:
                conn.close()
        except Exception:
            codes = []
    codes = codes[:400]

    # ★ 4.1：并行读取（日K缓存命中秒级；min5 活跃股用本地数据聚合日K）
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _get_zt(code):
        try:
            kl = df.fetch_kline(code, "day", days)
            if len(kl) < 30:
                # 无 day 缓存且下载失败 → 用本地 min5 聚合日K
                kl = _daily_from_min5(code, days)
            return code, detect_zt(kl, code)
        except Exception:
            return code, []

    pool = {}
    with ThreadPoolExecutor(max_workers=12) as ex:
        futs = [ex.submit(_get_zt, c) for c in codes]
        for f in as_completed(futs):
            code, zt = f.result()
            for d, pct in zt:
                pool.setdefault(d, []).append({"code": code, "name": code, "pct": pct})
    result = dict(sorted(pool.items()))
    _mem[cache_key] = (time.time(), result)
    return result


def _daily_from_min5(code, days=400):
    """从本地 min5 数据聚合日K（用当日最后一根 5 分钟 bar 近似日K）。
    仅用于重建情绪历史（涨跌幅判断足够）。
    """
    try:
        m = df.fetch_kline(code, "min5", 0)  # 本地一年数据
        if not m:
            return []
        daily = {}
        for b in m:
            d = b["date"][:10]
            daily[d] = b  # 每日本地覆盖为最后一根
        kl = [{"date": d, "open": b["open"], "high": b["high"],
               "low": b["low"], "close": b["close"],
               "volume": b["volume"], "amount": b.get("amount", 0)}
              for d, b in sorted(daily.items())]
        return kl[-days:]
    except Exception:
        return []


def build_sentiment_series(pool=None, start=None, end=None):
    """从历史涨停池构建情绪序列（逐日）：
    返回 [{date, zt_count, max_days, prev_zt_count, prev_zt_premium, sentiment_score}]
    其中 prev_zt_premium = 前一日涨停股今日平均涨幅（情绪温度计）。
    """
    if pool is None:
        pool = rebuild_history()
    dates = sorted(pool.keys())
    if start:
        dates = [d for d in dates if d >= start]
    if end:
        dates = [d for d in dates if d <= end]

    # ★ 4.1 优化：一次性预取所有涉及股票的行情（避免逐日重复拉取）
    all_codes = set()
    for d in dates:
        for x in pool.get(d, []):
            all_codes.add(x["code"])
    quotes_all = {}
    codes_list = list(all_codes)
    for i in range(0, len(codes_list), 300):
        try:
            quotes_all.update(df.fetch_quotes(codes_list[i:i + 300]))
        except Exception:
            pass

    series = []
    prev_zt_codes = set()
    for i, d in enumerate(dates):
        zt = pool.get(d, [])
        prev_premium = None
        if prev_zt_codes:
            rets = []
            for c in list(prev_zt_codes)[:80]:
                q = quotes_all.get(c, {})
                yc = q.get("yest_close", 0) or 0
                px = q.get("price", 0) or 0
                if yc > 0 and px > 0:
                    rets.append((px - yc) / yc)
            if rets:
                prev_premium = round(sum(rets) / len(rets), 4)
        # 空间板：连板数无法从单日池直接得（需连续跟踪），简化用涨停家数
        series.append({
            "date": d,
            "zt_count": len(zt),
            "prev_zt_premium": prev_premium,
            "sentiment_score": round(min(100, len(zt) * 1.2), 1),
        })
        prev_zt_codes = {x["code"] for x in zt}
        if i > 0:
            series[i - 1]["next_zt_count"] = len(zt)  # 次日涨停家数（IC 目标）
    return series


def sentiment_ic(series):
    """情绪因子 IC：今日情绪分/涨停家数 vs 次日涨停家数（市场热度延续性）。
    返回 {factor, ic_mean, samples, positive_ratio}
    """
    pairs = [(s["sentiment_score"], s.get("next_zt_count")) for s in series
             if s.get("next_zt_count") is not None]
    if len(pairs) < 10:
        return {"factor": "情绪分→次日涨停", "ic_mean": None, "samples": len(pairs)}
    from . import factor as fac
    ic = fac.spearman([p[0] for p in pairs], [p[1] for p in pairs])
    return {"factor": "情绪分→次日涨停", "ic_mean": round(ic, 4) if ic else None,
            "samples": len(pairs)}
