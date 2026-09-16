# -*- coding: utf-8 -*-
"""市场情绪周期模块（4.0 实战体系核心）—— 参考 daben-review emotion.py / vibe-astock / limit-up-sniper
A股超短实战的"天气系统"：以涨停池为原料、以昨日涨停股今日表现为温度计。

情绪指标：
  1. 涨停家数 / 跌停家数 / 涨跌停比
  2. 空间板（连板高度）与连板梯队完整性
  3. 炸板率（炸板数 / (封板+炸板)）
  4. 昨日涨停今日溢价（情绪温度计）：高开率 / 平均涨幅 / 红盘率
  5. 分层晋级率（首板→2板，2板→3板）
  6. 赚钱效应（昨日涨停股今日红盘比例）
  7. 情绪周期相位：冰点 → 发酵 → 高潮 → 退潮

输出：sentiment_snapshot() 供全局开仓开关、打板评分、Web 仪表盘使用。
"""
import time

from . import config as C
from . import datafeed as df
from . import limitup as lu


def _fbt_time(fbt):
    """封板时间 92500 → '09:25'"""
    try:
        v = int(fbt)
        return f"{v // 10000:02d}:{(v % 10000) // 100:02d}"
    except (ValueError, TypeError):
        return ""


def compute_sentiment(date=None, force=False):
    """计算当日市场情绪指标。date: YYYYMMDD 或 None(今天)。
    返回 dict：完整情绪快照
    """
    date = date or time.strftime("%Y%m%d")
    zt = lu.fetch_pool("zt", date, force)
    dt = lu.fetch_pool("dt", date, force)
    zt_rows = [r for r in zt]
    dt_rows = [r for r in dt]

    # 涨停家数 / 跌停家数 / 空间板 / 梯队
    zt_count = len(zt_rows)
    dt_count = len(dt_rows)
    max_days = 0
    ladder = {}
    for r in zt_rows:
        days = int((r.get("zttj") or {}).get("days", 1) or 1)
        max_days = max(max_days, days)
        ladder[days] = ladder.get(days, 0) + 1

    # 炸板率：zbc>0 表示曾炸板；封板数=当前涨停，炸板数≈(涨停+曾炸)
    zhaban = sum(1 for r in zt_rows if int(r.get("zbc", 0) or 0) > 0)
    zhaban_rate = zhaban / zt_count if zt_count else 0.0

    # 昨日涨停今日表现（情绪温度计）
    yzt_rows = lu.yesterday_zt(date, force)
    yzt_premium = _yesterday_premium(yzt_rows, date)

    # 分层晋级率：今日连板 N 的家数 / 昨日连板 N-1 的家数
    promotion = {}
    if yzt_rows:
        yzt_ladder = {}
        for r in yzt_rows:
            d = int(r.get("days", 1) or 1)
            yzt_ladder[d] = yzt_ladder.get(d, 0) + 1
        for n in (1, 2, 3):
            base = yzt_ladder.get(n, 0)
            up = ladder.get(n + 1, 0)
            promotion[f"{n}进{n+1}"] = round(up / base, 3) if base > 0 else None

    # 情绪综合评分 0-100（实战加权）
    score = 0.0
    # 涨停家数（>50 加分，<20 减分）
    score += min(30, zt_count * 0.6)
    # 空间板高度（>5 加分）
    score += min(15, max_days * 3)
    # 炸板率（<20% 加分，>50% 减分）
    score += 10 - zhaban_rate * 20
    # 昨日涨停溢价（赚钱效应）
    if yzt_premium["avg_ret"] is not None:
        score += max(-20, min(25, yzt_premium["avg_ret"] * 100 * 2.5))
    # 跌停家数惩罚
    score -= min(20, dt_count * 1.5)
    score = max(0, min(100, round(score, 1)))

    # 情绪相位判定（实战规则，参考 daben-review 相位规则）
    phase = _phase_of(zt_count, dt_count, max_days, zhaban_rate,
                      yzt_premium["avg_ret"])

    return {
        "date": date,
        "phase": phase,
        "score": score,
        "zt_count": zt_count,
        "dt_count": dt_count,
        "zt_dt_ratio": round(zt_count / dt_count, 2) if dt_count else None,
        "max_days": max_days,          # 空间板
        "ladder": ladder,              # 连板梯队
        "zhaban_rate": round(zhaban_rate, 3),
        "first_board": sum(1 for r in zt_rows if int((r.get("zttj") or {}).get("days", 1) or 1) == 1),
        "promotion": promotion,        # 分层晋级率
        "yesterday": yzt_premium,      # 昨日涨停今日表现
        "seal_fund": round(sum(float(r.get("fund", 0) or 0) for r in zt_rows), 0),
        "sectors": _sector_burst(zt_rows),
        "themes": _theme_burst(),      # 4.1 题材爆发（涨停原因主题词）
    }


def _theme_burst():
    """题材爆发度：{题材: 涨停家数}（同花顺涨停原因主题词，4.1）"""
    try:
        from . import limitup as lu
        return lu.theme_burst()
    except Exception:
        return {}


def _yesterday_premium(yzt_rows, today):
    """昨日涨停股今日表现：高开率/平均涨幅/红盘率（赚钱效应温度计）"""
    if not yzt_rows:
        return {"count": 0, "avg_ret": None, "up_ratio": None,
                "open_up_ratio": None, "rows": []}
    codes = [r["code"] for r in yzt_rows]
    quotes = df.fetch_quotes(codes)
    rets, opens, ups = [], [], []
    rows_out = []
    for r in yzt_rows:
        q = quotes.get(r["code"], {})
        price = q.get("price", 0) or 0
        yc = q.get("yest_close", 0) or 0
        op = q.get("open", 0) or 0
        if price <= 0 or yc <= 0:
            continue
        ret = (price - yc) / yc
        rets.append(ret)
        if op > 0:
            opens.append((op - yc) / yc)
        ups.append(1 if ret > 0 else 0)
        rows_out.append({"code": r["code"], "name": q.get("name", r["name"]),
                         "days": r.get("days", 1), "ret": round(ret * 100, 2),
                         "open": round((op - yc) / yc * 100, 2) if op > 0 else None})
    n = len(rets)
    avg = sum(rets) / n if n else 0.0
    return {
        "count": n,
        "avg_ret": round(avg, 4),
        "avg_ret_pct": round(avg * 100, 2),
        "up_ratio": round(sum(ups) / n, 3) if n else None,
        "open_up_ratio": round(sum(1 for o in opens if o > 0) / len(opens), 3) if opens else None,
        "rows": sorted(rows_out, key=lambda x: -x["ret"])[:30],
    }


def _sector_burst(zt_rows):
    """涨停板块爆发度：{sector: count} Top8"""
    secs = {}
    for r in zt_rows:
        s = r.get("hybk", "") or "其他"
        secs[s] = secs.get(s, 0) + 1
    return dict(sorted(secs.items(), key=lambda x: -x[1])[:8])


def _phase_of(zt, dt, max_days, zhaban_rate, yzt_avg):
    """情绪相位判定（实战规则）：
    冰点：涨停<25 或 跌停>涨停
    高潮：涨停>60 且 空间板>=5 且 炸板率<0.3
    退潮：涨停骤减 + 昨日溢价转负 或 空间板崩塌
    发酵：其余（涨停回升、炸板率适中）
    """
    if zt < 25 or (dt > 0 and dt >= zt):
        return "冰点"
    if zt >= 60 and max_days >= 5 and zhaban_rate < 0.30:
        return "高潮"
    if yzt_avg is not None and yzt_avg < -0.02 and max_days <= 3:
        return "退潮"
    return "发酵"


def phase_position(phase):
    """情绪相位 → 交易策略（开仓开关 + 仓位建议）"""
    return {
        "冰点": {"open": False, "max_pos": 0, "action": "空仓等待，情绪冰点不宜打板",
                 "desc": "涨停少、炸板多或跌停潮，管住手"},
        "发酵": {"open": True, "max_pos": 2, "action": "轻仓试探，做首板/低位板",
                 "desc": "情绪回暖，涨停增加、溢价转正，可参与"},
        "高潮": {"open": True, "max_pos": 3, "action": "正常打板，做龙头/高标",
                 "desc": "涨停潮+空间板高+炸板率低，赚钱效应强"},
        "退潮": {"open": False, "max_pos": 0, "action": "空仓防守，回避接力",
                 "desc": "昨日涨停大面积亏损，连板崩塌，风险大于机会"},
    }.get(phase, {"open": True, "max_pos": 2, "action": "谨慎参与",
                  "desc": "情绪不明，轻仓观望"})


def sentiment_snapshot(date=None, force=False):
    """Web 仪表盘用完整快照"""
    s = compute_sentiment(date, force)
    pos = phase_position(s["phase"])
    return {**s, **pos}


# 模块级缓存（避免频繁调用重复拉数据）
_cache = {"ts": 0, "data": None}


def cached_sentiment(max_age=60, date=None, force=False):
    """带 TTL 缓存的情绪快照（实盘轮询用）"""
    now = time.time()
    if _cache["data"] and not force and now - _cache["ts"] < max_age:
        return _cache["data"]
    s = sentiment_snapshot(date, force)
    _cache["data"] = s
    _cache["ts"] = now
    return s
