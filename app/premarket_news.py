# -*- coding: utf-8 -*-
"""★ 4.7 盘前新闻聚合（全球实时要闻 + AI 筛选）
数据源（全部实测可用，多源容错）：
  1. 华尔街见闻直播（全球/美股/港股/外汇/大宗/A股 六频道，实时）
  2. 东财 7x24 快讯（np-weblist，带关联个股）
  3. 新浪 7x24（备用）
流程：多源拉取 → 归一化 → 标题去重 → 规则预评分(0-5) → AI 批量筛选(可关) → 排序输出
AI 复用 ai._chat（设置页配置的 LLM）；LLM 不可用自动退化为纯规则分。
前端入口：/api/news/premarket
"""
import json
import re
import threading
import time
import urllib.request

from . import config as C

_UA = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
}
_lock = threading.Lock()
_cache = {}                       # (scope, min_score) -> (ts, result)
_TTL = 300                        # 结果缓存 5 分钟（AI 调用有成本）
_RAW = {"ts": 0.0, "items": [], "src": []}   # 去重后原始池（跨 scope 共享）
_AI_CACHE = {"ts": 0.0, "by_id": {}}   # 后台 AI 结果（按原始池版本 ts 匹配）
_ai_jobs = {"running": False}

_WSCN_CHANNELS = ["global-channel", "a-stock-channel", "us-stock-channel",
                  "hk-stock-channel", "forex-channel", "commodity-channel"]

# ---------- HTTP ----------
def _http_json(url, ref=None, timeout=8):
    h = dict(_UA)
    if ref:
        h["Referer"] = ref
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


# ---------- 数据源 ----------
def _fetch_wscn(limit=15):
    """华尔街见闻直播：全球实时要闻（含美股/外汇/大宗分频道）"""
    out = []
    for ch in _WSCN_CHANNELS:
        try:
            url = ("https://api-one.wallstcn.com/apiv1/content/lives?"
                   "channel=%s&client=pc&limit=%d" % (ch, limit))
            d = _http_json(url)
            for it in (d.get("data") or {}).get("items") or []:
                title = (it.get("title") or it.get("highlight_title") or "").strip()
                text = (it.get("content_text") or "").strip()
                if not title:
                    title = text[:40]
                if not (title or text):
                    continue
                out.append({
                    "title": title, "text": text,
                    "ts": int(it.get("display_time") or 0),
                    "source": "华尔街见闻",
                    "channels": it.get("channels") or [],
                    "stocks": [],
                })
        except Exception:
            continue
    return out


def _fetch_em(limit=30):
    """东财 7x24 快讯（带关联个股）"""
    url = ("https://np-weblist.eastmoney.com/comm/web/getFastNewsList?"
           "client=web&biz=web_724&fastColumn=102&sortEnd=&pageSize=%d&req_trace=1" % limit)
    d = _http_json(url)
    out = []
    for it in (d.get("data") or {}).get("fastNewsList") or []:
        title = (it.get("title") or "").strip()
        text = (it.get("summary") or "").strip()
        if not (title or text):
            continue
        ts = 0
        stm = it.get("showTime") or ""
        if stm:
            try:
                ts = int(time.mktime(time.strptime(stm[:19], "%Y-%m-%d %H:%M:%S")))
            except Exception:
                ts = 0
        stocks = []
        for s in it.get("stockList") or []:
            if isinstance(s, dict):
                stocks.append({"code": str(s.get("code", "")), "name": s.get("name", "")})
        out.append({"title": title, "text": text, "ts": ts,
                    "source": "东财快讯", "channels": [], "stocks": stocks})
    return out


def _fetch_sina(limit=30):
    """新浪 7x24（备用）"""
    url = ("https://zhibo.sina.com.cn/api/zhibo/feed?page=1&page_size=%d"
           "&zhibo_id=152&tag_id=0&dire=f&dpc=1" % limit)
    d = _http_json(url, ref="https://finance.sina.com.cn/7x24/")
    out = []
    feed = ((d.get("result") or {}).get("data") or {}).get("feed") or {}
    for it in feed.get("list") or []:
        raw = (it.get("rich_text") or "").strip()
        if not raw:
            continue
        title, text = raw, ""
        m = re.match("【(.+?)】(.*)", raw, re.S)
        if m:
            title, text = m.group(1).strip(), m.group(2).strip()
        ts = 0
        ct = it.get("create_time") or ""
        if ct:
            try:
                ts = int(time.mktime(time.strptime(ct[:19], "%Y-%m-%d %H:%M:%S")))
            except Exception:
                ts = 0
        out.append({"title": title, "text": text, "ts": ts,
                    "source": "新浪7x24", "channels": [], "stocks": []})
    return out


# ---------- 分类与规则评分 ----------
_GLOBAL_KW = ["美联储", "美国", "美股", "纳斯达克", "道指", "标普", "欧央行", "欧洲", "英国",
              "日本", "日央行", "日经", "韩国", "原油", "黄金", "白银", "美债", "国债",
              "美元指数", "汇率", "离岸人民币", "关税", "制裁", "俄乌", "俄罗斯", "乌克兰",
              "中东", "以色列", "伊朗", "OPEC", "非农", "CPI", "PPI", "GDP", "PMI",
              "鲍威尔", "比特币", "英伟达", "特斯拉", "苹果", "谷歌", "微软", "台积电"]
_ASHARE_KW = ["A股", "沪指", "深成指", "创业板", "两市", "证监会", "央行", "国务院", "发改委",
              "财政部", "北向", "融资", "涨停", "跌停", "板块", "题材", "个股", "沪深",
              "IPO", "新股", "上市", "监管", "降准", "降息", "LPR", "MLF", "外资",
              "回购", "增持", "减持", "业绩", "财报", "预增", "预减", "半导体", "芯片",
              "新能源", "光伏", "锂电", "算力", "机器人", "军工", "医药", "券商", "银行"]
_HOT_KW = ["央行", "美联储", "降准", "降息", "加息", "LPR", "MLF", "国务院", "证监会",
           "财政部", "关税", "制裁", "战争", "袭击", "冲突", "CPI", "非农", "GDP",
           "退市", "立案", "停牌", "重组", "并购", "业绩预告", "业绩快报", "违约", "暴雷",
           "熔断", "暴跌", "暴涨", "跌停", "涨停", "回购", "增持", "减持", "解禁", "IPO",
           "危机", "破产", "调查", "处罚"]
_WARM_KW = ["美股", "纳指", "道指", "原油", "黄金", "美债", "汇率", "北向", "融资融券",
            "板块", "概念", "半导体", "芯片", "AI", "算力", "机器人", "新能源", "光伏",
            "医药", "军工", "券商", "地产", "订单", "中标", "涨价", "提价", "获批",
            "新高", "新低", "签约", "突破", "超预期"]
_LOW_KW = ["到访", "会见", "调研", "座谈", "考察", "交流", "签约仪式", "战略合作", "论坛",
           "指导会", "圆桌", "致辞", "回访"]


def _classify(it):
    blob = it["title"] + " " + it.get("text", "")
    chs = it.get("channels") or []
    g_hit = any(k in blob for k in _GLOBAL_KW) or any(
        c in chs for c in ("us-stock-channel", "hk-stock-channel",
                           "forex-channel", "commodity-channel"))
    a_hit = any(k in blob for k in _ASHARE_KW) or "a-stock-channel" in chs
    if g_hit and a_hit:
        return "global" if sum(1 for k in _GLOBAL_KW if k in blob) >= 2 else "ashare"
    if g_hit:
        return "global"
    if a_hit:
        return "ashare"
    return "general"


# ---------- ★ Phase19 市场维度标签（美股/日韩/港股/欧洲/商品/外汇/其他） ----------
_MARKET_RULES = [
    # (市场名, 关键词列表)
    ("美股", ["美联储", "纳指", "纳斯达克", "标普", "道指", "道琼斯", "美股", "华尔街",
               "英伟达", "特斯拉", "苹果", "微软", "谷歌", "亚马逊", "美债收益率", "关税"]),
    ("日韩", ["日银", "日元", "日经", "日本央行", "韩国", "三星", "SK海力士", "KOSPI"]),
    ("港股", ["恒生", "港股", "南向", "联汇"]),
    ("欧洲", ["欧央行", "欧元", "DAX", "富时", "欧洲央行", "英国央行"]),
    ("商品", ["黄金", "金价", "原油", "OPEC", "铜", "铁矿", "白银", "天然气"]),
    ("外汇", ["人民币", "美元指数", "离岸", "汇率", "美元兑"]),
]


def market_tags(it):
    """新闻 → 市场维度标签列表（6 类，可命中多个；未命中 → ['其他']）"""
    blob = (it["title"] or "") + " " + (it.get("text") or "")
    hits = [m for m, kws in _MARKET_RULES if any(k in blob for k in kws)]
    return hits or ["其他"]


def _rule_score(it):
    blob = it["title"] + " " + it.get("text", "")
    hot = sum(1 for k in _HOT_KW if k in blob)
    warm = sum(1 for k in _WARM_KW if k in blob)
    low = sum(1 for k in _LOW_KW if k in blob)
    score = 1 + min(3, hot * 2 + warm)
    if low and hot == 0:
        score = min(score, 1)      # 公关稿/调研到访类 → 低价值
    return max(0, min(5, score))


def _norm_key(t):
    return re.sub(r"[\s\u3000【】\[\]，。、：；！？“”‘’（）()]", "", t or "")


def _dedup(items):
    """标题去重（完全相同/包含关系视为同一条；保留关联个股更多的版本）
    ★ 包含判定用完整归一化标题（截断前），避免 18 字截断导致漏判"""
    seen = []
    out = []
    for it in items:
        full = _norm_key(it["title"]) or _norm_key(it.get("text", ""))
        if not full:
            continue
        full = full[:60]
        key = full[:18]
        dup = False
        for k2, f2, idx in seen:
            if key == k2 or (len(full) >= 10 and len(f2) >= 10 and (full in f2 or f2 in full)):
                if len(it.get("stocks") or []) > len(out[idx].get("stocks") or []):
                    it["ts"] = it.get("ts") or out[idx].get("ts")
                    out[idx] = it
                dup = True
                break
        if not dup:
            seen.append((key, full, len(out)))
            out.append(it)
    return out


# ---------- AI 筛选 ----------
_AI_SYSTEM = ("你是资深A股盘前新闻编辑，用户是短线交易者。请客观、克制地评估每条新闻对"
              "今天A股开盘与盘面的潜在影响，不要夸大。")


def _ai_screen(cands):
    """批量 AI 筛选：返回 {候选下标: {s,c,w}}；失败返回 None（退化为规则分）"""
    try:
        from . import ai as aim
    except Exception:
        return None
    lines = []
    for i, it in enumerate(cands):
        snippet = (it["title"] + " " + it.get("text", ""))[:80]
        lines.append("%d. %s" % (i, snippet))
    prompt = ("以下是今日盘前收集的新闻（编号列表），请逐条评估对今天A股的影响：\n"
              + "\n".join(lines) + "\n\n"
              "输出严格JSON数组（不要任何其他文字），每条格式："
              '{"i":编号,"s":0-5重要度,"c":"分类","w":"≤30字影响说明","sec":[{"n":"板块名","d":1}]}\n'
              "分类只能从：宏观/央行/美股/商品/地缘/A股政策/行业/公司/其他 中选。\n"
              "评分标准：5=直接影响大盘或板块异动，3=间接影响，0-1=无关或公关稿。\n"
              "sec=该新闻直接影响的A股行业/板块（0-3个），d: 1=利好，-1=利空，0=中性；"
              "板块名用2-5字（如半导体、地产、白酒、算力、军工、券商），与具体板块无关则sec为空数组。\n"
              "影响说明中不要提及编号或“同第N条”。")
    raw = aim._chat(_AI_SYSTEM, prompt, max_tokens=6000, timeout=240)
    if not raw:
        return None
    a, b = raw.find("["), raw.rfind("]")
    if a < 0 or b <= a:
        return None
    try:
        arr = json.loads(raw[a:b + 1])
    except Exception:
        return None
    m = {}
    for r in arr:
        if not isinstance(r, dict):
            continue
        try:
            secs = []
            for s0 in (r.get("sec") or [])[:3]:
                if isinstance(s0, dict):
                    n0 = str(s0.get("n") or "").strip()[:8]
                    try:
                        d0 = int(s0.get("d", 0))
                    except Exception:
                        d0 = 0
                    if n0:
                        secs.append({"n": n0, "d": 1 if d0 > 0 else (-1 if d0 < 0 else 0)})
            m[int(r.get("i"))] = {"s": int(r.get("s", 0)),
                                  "c": str(r.get("c", "") or ""),
                                  "w": str(r.get("w", "") or ""),
                                  "sec": secs}
        except Exception:
            continue
    return m or None


def _ai_bg(raw_ts, cands):
    """后台线程跑 AI 筛选：完成后写回 _AI_CACHE 并作废结果缓存（下次轮询自动带上 AI 分）"""
    try:
        m = _ai_screen(cands)
        if m is None:
            print("[premarket_news] AI 筛选本轮未返回有效结果（超时/解析失败）", flush=True)
        by_id = {}
        if m:
            for idx, aiv in m.items():
                if 0 <= idx < len(cands):
                    by_id[id(cands[idx])] = aiv
        with _lock:
            _AI_CACHE["ts"] = raw_ts
            _AI_CACHE["by_id"] = by_id
            _cache.clear()
    except Exception as e:
        print("[premarket_news] AI 后台任务异常: %s" % e, flush=True)
    finally:
        _ai_jobs["running"] = False


# ---------- 入口 ----------
def _collect():
    items, src_ok = [], []
    for fn, nm in ((_fetch_wscn, "wscn"), (_fetch_em, "eastmoney"), (_fetch_sina, "sina")):
        try:
            got = fn()
            if got:
                items.extend(got)
                src_ok.append(nm)
        except Exception:
            continue
    items = _dedup(items)
    for it in items:
        it["scope"] = _classify(it)
        it["rule"] = _rule_score(it)
    items.sort(key=lambda x: x.get("ts") or 0, reverse=True)
    return items, src_ok


def premarket_news(scope="all", min_score=2, force=False):
    """盘前新闻聚合入口。scope: all|global|ashare；min_score: 0-5 重要度下限"""
    if not getattr(C, "PREMARKET_NEWS_ENABLED", True):
        return {"disabled": True, "items": [], "stats": {}, "fetched_at": ""}
    scope = scope if scope in ("all", "global", "ashare") else "all"
    try:
        min_score = max(0, min(5, int(min_score)))
    except Exception:
        min_score = 2
    key = (scope, min_score)
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and not force and now - hit[0] < _TTL:
            return hit[1]
        if force or now - _RAW["ts"] > 600 or not _RAW["items"]:  # 4.9: 600s窗口，避免重采清掉未跑完的AI结果
            items, src_ok = _collect()
            _RAW["ts"], _RAW["items"], _RAW["src"] = now, items, src_ok
            _AI_CACHE.update({"ts": 0.0, "by_id": {}})
        items, src_ok = _RAW["items"], _RAW["src"]
    pool = [x for x in items if scope == "all" or x["scope"] == scope]
    pool = pool[:getattr(C, "PREMARKET_NEWS_LIMIT", 80)]
    cands = [x for x in pool if x["rule"] >= 1][:30]
    # ★ 4.7 异步 AI：首响秒回规则分，AI 后台跑（推理模型需 1-2 分钟），完成后下次轮询自动带上
    ai_by_id, ai_pending = {}, False
    if getattr(C, "PREMARKET_NEWS_AI", True) and cands:
        if _AI_CACHE.get("ts") == _RAW["ts"]:
            ai_by_id = _AI_CACHE.get("by_id") or {}
        elif not _ai_jobs.get("running"):
            _ai_jobs["running"] = True
            try:
                threading.Thread(target=_ai_bg, args=(_RAW["ts"], list(cands)),
                                 daemon=True).start()
                ai_pending = True
            except Exception:
                _ai_jobs["running"] = False
        else:
            ai_pending = True
    out = []
    for it in pool:
        aiv = ai_by_id.get(id(it))
        if aiv:
            score = max(0, min(5, aiv["s"]))
            cat, why, used = aiv["c"], aiv["w"], True
        else:
            score, cat, why, used = it["rule"], "", "", False
        if score < min_score:
            continue
        if aiv and aiv.get("sec"):
            sectors = [{"name": x["n"], "dir": x.get("d", 0)} for x in aiv["sec"]]
        else:
            sectors = [{"name": nm, "dir": 0} for nm in _rule_sectors(it)]
        _mkts = market_tags(it)   # ★ Phase19 市场维度标签
        out.append({
            "title": it["title"], "text": it.get("text", ""),
            "time": time.strftime("%m-%d %H:%M", time.localtime(it["ts"])) if it.get("ts") else "",
            "ts": it.get("ts") or 0,
            "source": it["source"], "scope": it["scope"],
            "score": score, "cat": cat, "why": why, "ai": used,
            "stocks": it.get("stocks") or [],
            "sectors": sectors,
            "market": _mkts[0] if _mkts else "其他",
            "markets": _mkts,
        })
    out.sort(key=lambda x: (-x["score"], -x["ts"]))
    out = out[:getattr(C, "PREMARKET_NEWS_TOP", 30)]
    res = {
        "items": out,
        "stats": {"total": len(items), "pool": len(pool), "kept": len(out),
                  "ai_used": bool(ai_by_id), "ai_pending": ai_pending, "sources": src_ok},
        "fetched_at": time.strftime("%H:%M:%S"),
    }
    with _lock:
        _cache[key] = (time.time(), res)
    return res


# ---------- ★ 4.9 板块视图辅助 ----------
_SECTOR_ENV = {"smap": None, "names": None}


def _sector_env():
    """(个股代码→行业列表, 行业名集合)，懒加载并缓存"""
    if _SECTOR_ENV["smap"] is None:
        try:
            from . import sector as _sec
            smap = _sec.load_sector_map() or {}
        except Exception:
            smap = {}
        names = set()
        for v in smap.values():
            for nm in v or []:
                if nm:
                    names.add(nm)
        _SECTOR_ENV["smap"], _SECTOR_ENV["names"] = smap, names
    return _SECTOR_ENV["smap"], _SECTOR_ENV["names"]


def _norm_sector(nm):
    nm = str(nm or "").strip()
    for suf in ("板块", "行业"):
        if nm.endswith(suf) and len(nm) > len(suf) + 1:
            nm = nm[:-2]
    return nm


def _rule_sectors(it):
    """规则法提取关联行业名（个股映射 + 标题正文匹配），无方向"""
    smap, names = _sector_env()
    rel = set()
    for s in it.get("stocks") or []:
        code = str(s.get("code") or "")[-6:]
        for nm in smap.get(code) or []:
            rel.add(_norm_sector(nm))
    txt = (it.get("title") or "") + (it.get("text") or "")[:120]
    for nm in names:
        short = _norm_sector(nm)
        if len(short) >= 2 and short in txt:
            rel.add(short)
    rel.discard("")
    return list(rel)


# ---------- ★ 4.8 盘前简报 + 板块联动 ----------
_BRIEF_CACHE = {"ts": 0.0, "day": "", "data": None}
_HITS_CACHE = {"ts": 0.0, "data": None}


def premarket_brief(force=False):
    """盘前简报（开盘自动弹窗用）。非交易日返回 enabled=False；force 绕过交易日与缓存检查"""
    today = time.strftime("%Y-%m-%d")
    now = time.time()
    if not force:
        try:
            from . import trading_calendar as tcal
            if not tcal.is_trading_day(today):
                return {"enabled": False, "reason": "非交易日", "day": today, "items": []}
        except Exception:
            pass
        if _BRIEF_CACHE["data"] and _BRIEF_CACHE["day"] == today and now - _BRIEF_CACHE["ts"] < 120:
            return _BRIEF_CACHE["data"]
    d = premarket_news(scope="all", min_score=0, force=False)
    items = [x for x in (d.get("items") or []) if x.get("score", 0) >= 3][:12]
    res = {"enabled": True, "day": today,
           "items": [{"title": x["title"], "score": x["score"], "cat": x.get("cat") or "",
                      "why": x.get("why") or "", "source": x.get("source") or "",
                      "time": x.get("time") or ""} for x in items],
           "ai_pending": bool(d.get("stats", {}).get("ai_pending")),
           "fetched_at": d.get("fetched_at", "")}
    with _lock:
        _BRIEF_CACHE.update({"ts": time.time(), "day": today, "data": res})
    return res


def news_sector_hits():
    """盘前新闻 -> 关联板块 {name: {count, score, bull, bear}}。供板块资金流页高亮（60s 缓存，不阻塞）"""
    now = time.time()
    if _HITS_CACHE["data"] is not None and now - _HITS_CACHE["ts"] < 60:
        return _HITS_CACHE["data"]
    hits = {}
    items = []
    ai_map = {}
    try:
        with _lock:
            if _RAW["items"] and now - _RAW["ts"] < 1800:
                items = list(_RAW["items"])
                if _AI_CACHE.get("ts") == _RAW["ts"]:
                    ai_map = _AI_CACHE.get("by_id") or {}
        if items:
            for it in items:
                w = max(1, int(it.get("rule") or 0))
                aiv = ai_map.get(id(it))
                rel = {}
                if aiv and aiv.get("sec"):
                    for x in aiv["sec"]:
                        nm = _norm_sector(x.get("n"))
                        if nm:
                            rel[nm] = x.get("d", 0)
                else:
                    for nm in _rule_sectors(it):
                        rel[nm] = 0
                for nm, d in rel.items():
                    h = hits.setdefault(nm, {"count": 0, "score": 0, "bull": 0, "bear": 0})
                    h["count"] += 1
                    h["score"] += w
                    if d > 0:
                        h["bull"] += 1
                    elif d < 0:
                        h["bear"] += 1
        elif not _ai_jobs.get("hits_warming"):
            _ai_jobs["hits_warming"] = True

            def _warm():
                try:
                    got, src_ok = _collect()
                    with _lock:
                        _RAW["ts"], _RAW["items"], _RAW["src"] = time.time(), got, src_ok
                except Exception:
                    pass
                finally:
                    _ai_jobs["hits_warming"] = False

            threading.Thread(target=_warm, daemon=True).start()
    except Exception:
        pass
    with _lock:
        _HITS_CACHE.update({"ts": now, "data": hits})
    return hits


def sector_view():
    """板块视图：新闻按行业/板块聚合，标注利好利空（★4.9）"""
    d = premarket_news(scope="all", min_score=0, force=False)
    agg = {}
    for it in d.get("items") or []:
        for sec in it.get("sectors") or []:
            nm = _norm_sector(sec.get("name"))
            if not nm:
                continue
            g = agg.setdefault(nm, {"bull": 0, "bear": 0, "flat": 0, "score": 0, "news": []})
            dr = sec.get("dir", 0)
            if dr > 0:
                g["bull"] += 1
            elif dr < 0:
                g["bear"] += 1
            else:
                g["flat"] += 1
            g["score"] += it.get("score", 0)
            g["news"].append({"title": it["title"], "time": it.get("time", ""),
                              "source": it.get("source", ""), "score": it.get("score", 0),
                              "dir": dr, "why": it.get("why") or ""})
    sectors = []
    for nm, g in agg.items():
        g["news"].sort(key=lambda x: -x["score"])
        sectors.append({"name": nm, "net": g["bull"] - g["bear"], "bull": g["bull"],
                        "bear": g["bear"], "flat": g["flat"], "count": len(g["news"]),
                        "score": g["score"], "news": g["news"][:8]})
    sectors.sort(key=lambda x: (-abs(x["net"]), -x["score"], -x["count"]))
    st = d.get("stats") or {}
    return {"sectors": sectors[:40], "ai_used": bool(st.get("ai_used")),
            "ai_pending": bool(st.get("ai_pending")), "fetched_at": d.get("fetched_at", "")}
