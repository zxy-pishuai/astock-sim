# -*- coding: utf-8 -*-
"""★ 4.5 新闻情绪模块（调研补充维度：新闻情绪 = 场外情绪）
数据源：东财快讯 np-weblist（免费零鉴权，纯 urllib）+
        新浪 7x24（备用，需 Referer）。
功能：
  1. fetch_news(limit)      拉取最新快讯（A股相关过滤）
  2. market_sentiment()     新闻情绪分（利好/利空关键词统计，-1~1）
  3. stock_news(code)       个股相关新闻（stockList 匹配）
  4. news_burst(topics)     题材新闻热度（配合情绪周期）
纯标准库，10 分钟缓存。
"""
import json
import re
import time
import urllib.request

from . import config as C

_mem = {}          # key -> (ts, value)
_TTL = 600         # 10 分钟缓存

# 利好/利空关键词（A股短视域）
_BULLISH = ["大涨", "涨停", "利好", "增持", "回购", "中标", "预增", "扭亏", "签约", "突破",
            "新高", "获批", "放量", "抢筹", "加仓", "提价", "涨价", "供应紧张", "超预期",
            "买入评级", "推荐评级", "重组", "并购", "订单"]
_BEARISH = ["大跌", "跌停", "利空", "减持", "亏损", "预减", "退市", "处罚", "立案", "调查",
            "暴雷", "违约", "解禁", "质押", "下修", "警示", "风险", "下跌", "卖出评级",
            "下调评级", "资金出逃", "跌超", "闪崩"]

# A股相关关键词（过滤全球新闻）
_ASTOCK_KW = ["A股", "沪指", "深成指", "创业板", "两市", "证监会", "央行", "国务院", "发改委",
              "北向", "融资", "涨停", "跌停", "板块", "题材", "个股", "沪深", "IPO", "新股",
              "上市", "监管", "降准", "降息", "LPR", "MLF", "汇率", "外资", "主力",
              "亿元", "证券", "基金", "机构", "散户", "股民", "回购", "增持", "减持",
              "业绩", "财报", "季报", "半年报", "年报", "预增", "预减", "半导体", "芯片",
              "新能源", "光伏", "锂电", "AI", "算力", "机器人", "军工", "医药", "消费"]


def _http_json(url, timeout=10, ref=None):
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    if ref:
        headers["Referer"] = ref
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _em_news(limit=30):
    url = ("https://np-weblist.eastmoney.com/comm/web/getFastNewsList?"
           "client=web&biz=web_724&fastColumn=102&sortEnd=&pageSize=%d&req_trace=1" % limit)
    d = _http_json(url)
    data = d.get("data") or {}
    return (data.get("fastNewsList") or [])


def _sina_news(limit=30):
    """新浪 7x24（备用）"""
    url = (f"https://zhibo.sina.com.cn/api/zhibo/feed?page=1&page_size={limit}"
           "&zhibo_id=152&tag_id=0&dire=f&dpc=1")
    d = _http_json(url, ref="https://finance.sina.com.cn/7x24/")
    items = []
    result = d.get("result", {}) or {}
    feed = result.get("data", {}) or {}
    for row in (feed.get("feed", {}) or {}).get("list", []):
        items.append({
            "title": row.get("rich_text", "") or row.get("content", ""),
            "showTime": str(row.get("create_time", ""))[:19],
            "summary": row.get("rich_text", ""),
            "stockList": [],
        })
    return items


def fetch_news(limit=30, force=False):
    """拉取最新快讯（东财优先，新浪备用）。返回 [{title, summary, showTime, stockList}]"""
    key = ("news", limit)
    now = time.time()
    if not force:
        hit = _mem.get(key)
        if hit and now - hit[0] < _TTL:
            return hit[1]
    rows = []
    try:
        rows = _em_news(limit)
    except Exception:
        pass
    if not rows:
        try:
            rows = _sina_news(limit)
        except Exception:
            rows = []
    out = []
    for r in rows:
        title = r.get("title", "") or ""
        summary = r.get("summary", "") or ""
        text = title + " " + summary
        # 只保留 A股相关
        if not any(k in text for k in _ASTOCK_KW):
            continue
        out.append({
            "title": title, "summary": summary,
            "showTime": r.get("showTime", "") or "",
            "stockList": r.get("stockList") or [],
            "bull": sum(1 for k in _BULLISH if k in text),
            "bear": sum(1 for k in _BEARISH if k in text),
        })
    _mem[key] = (now, out)
    return out


def market_sentiment(force=False):
    """新闻情绪分：近 N 条快讯的利好/利空净得分（-1~1）。
    返回 {score, bull_count, bear_count, total, trend, top_news}"""
    rows = fetch_news(40, force)
    if not rows:
        return {"score": 0.0, "bull_count": 0, "bear_count": 0, "total": 0,
                "trend": "无新闻", "top_news": []}
    bull = sum(r["bull"] for r in rows)
    bear = sum(r["bear"] for r in rows)
    total = bull + bear
    score = (bull - bear) / total if total else 0.0
    if score >= 0.3:
        trend = "偏多"
    elif score <= -0.3:
        trend = "偏空"
    elif total == 0:
        trend = "中性(无明确信号)"
    else:
        trend = "中性"
    return {
        "score": round(score, 3), "bull_count": bull, "bear_count": bear,
        "total": total, "trend": trend,
        "top_news": [{"title": r["title"], "showTime": r["showTime"],
                      "bull": r["bull"], "bear": r["bear"]} for r in rows[:8]],
    }


def stock_news(code, limit=10, force=False):
    """个股相关新闻（按 stockList 代码匹配或标题含代码）。"""
    rows = fetch_news(60, force)
    out = []
    for r in rows:
        sl = r.get("stockList") or []
        hit = False
        for s in sl:
            if isinstance(s, dict):
                sc = str(s.get("code", ""))
            else:
                sc = str(s)
            if sc == code or code in sc:
                hit = True
                break
        if not hit and code in (r.get("title", "") + r.get("summary", "")):
            hit = True
        if hit:
            out.append(r)
            if len(out) >= limit:
                break
    return out


def news_burst(force=False):
    """题材新闻热度：{关键词: 提及次数}（利好关键词 Top10）"""
    rows = fetch_news(40, force)
    heat = {}
    for r in rows:
        text = (r.get("title", "") + " " + r.get("summary", ""))
        for kw in ["A股", "沪指", "证监会", "央行", "北向", "涨停", "板块", "题材",
                   "半导体", "芯片", "AI", "算力", "机器人", "新能源", "光伏", "医药",
                   "军工", "消费", "房地产", "券商", "银行", "黄金", "原油"]:
            if kw in text:
                heat[kw] = heat.get(kw, 0) + 1
    return dict(sorted(heat.items(), key=lambda x: -x[1])[:10])
