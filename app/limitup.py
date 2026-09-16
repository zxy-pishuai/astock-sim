# -*- coding: utf-8 -*-
"""涨停池数据模块（4.0 实战体系）—— 参考 akshare stock_ztb_em / limit-up-sniper / daben-review
东财 push2ex 免费接口（纯 urllib）：
  - getTopicZTPool 涨停池：连板数/封板时间/炸板次数/封板资金/行业/换手率
  - getTopicDTPool 跌停池
  - getYesterdayZTPool 昨日涨停池（情绪温度计原料）
数据落 SQLite 缓存，供情绪周期/实战因子/打板评分使用。
"""
import json
import sqlite3
import threading
import time
import urllib.parse

from . import db as _db            # ★ J3：统一连接工厂
import urllib.request

from . import config as C

_UT = "7eea3edcaed734bea9cbfc24409ed989"
_BASE = "https://push2ex.eastmoney.com/"
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
_lock = threading.Lock()
_mem = {}          # (kind, date) -> (ts, list)

# ★ 4.5：数据源熔断器 —— 东财 push2ex / 同花顺被 IP 风控时快速失败 + 冷却跳过
#   之前：被封时每次 urlopen 挂到 12s 超时，sentiment 每 90s 重算 → 一挂几十秒
_breakers = {}     # name -> {"fails": n, "until": ts}
_BREAK_AFTER = 3
_BREAK_COOLDOWN = 600
_DEFAULT_TIMEOUT = 4


def _http_json(url, timeout=_DEFAULT_TIMEOUT, breaker="em", headers=None):
    now = time.time()
    b = _breakers.get(breaker)
    if b and now < b["until"]:
        raise TimeoutError(f"circuit open: {breaker}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA, **(headers or {})})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", errors="replace"))
        if b:
            b["fails"] = 0
        return data
    except Exception:
        if b is None:
            b = _breakers.setdefault(breaker, {"fails": 0, "until": 0.0})
        b["fails"] += 1
        if b["fails"] >= _BREAK_AFTER:
            b["until"] = now + _BREAK_COOLDOWN
        raise


def _conn():
    conn = _db.open_rw(C.DB_FILE)   # ★ J3：统一连接工厂（WAL 初始化已由 db.py 一次完成）
    conn.execute("""CREATE TABLE IF NOT EXISTS limit_pool(
        kind TEXT, date TEXT, code TEXT, name TEXT, payload TEXT,
        PRIMARY KEY(kind, date, code))""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_lp_dk ON limit_pool(kind, date)")
    return conn


def _save(kind, date, rows):
    try:
        with _lock:
            # R2-P1.4：写入侧清洗门——周末日期不落库（东财接口对周末 date 会
            # 返回最近交易日快照，落周末键即脏数据，如 08-22/23、08-29/30 行）。
            try:
                from datetime import datetime as _dt
                if _dt.strptime(date, "%Y%m%d").weekday() >= 5:
                    return
            except Exception:
                pass
            conn = _conn()
            try:
                for r in rows:
                    conn.execute(
                        "INSERT OR REPLACE INTO limit_pool(kind,date,code,name,payload) VALUES(?,?,?,?,?)",
                        (kind, date, r.get("c", ""), r.get("n", ""),
                         json.dumps(r, ensure_ascii=False)))
                conn.commit()
            finally:
                conn.close()
    except Exception:
        pass


def _load(kind, date):
    try:
        with _lock:
            # R2-P1.4：读侧清洗门——历史已落的周末脏键不读出（防御既有脏行）。
            try:
                from datetime import datetime as _dt
                if _dt.strptime(date, "%Y%m%d").weekday() >= 5:
                    return []
            except Exception:
                pass
            conn = _conn()
            try:
                cur = conn.execute(
                    "SELECT payload FROM limit_pool WHERE kind=? AND date=? ORDER BY code",
                    (kind, date))
                return [json.loads(r[0]) for r in cur.fetchall()]
            finally:
                conn.close()
    except Exception:
        return []


def _fetch_pool(kind, date, page_size=400):
    """抓取东财涨停/跌停池。kind: zt(涨停) | dt(跌停) | yzt(昨日涨停)"""
    api = {"zt": "getTopicZTPool", "dt": "getTopicDTPool",
           "yzt": "getYesterdayZTPool"}[kind]
    url = (f"{_BASE}{api}?ut={_UT}&dpt=wz.ztzt&Pageindex=0&pagesize={page_size}"
           f"&sort=fbt%3Aasc&date={date}")
    try:
        d = _http_json(url)
        return ((d.get("data") or {}).get("pool") or [])
    except Exception:
        return []


def _is_trading_day(date):
    """date(YYYYMMDD) 是否为工作日（周末非交易日；节假日不识别，
    东财接口对休市日返回空/旧快照时自然回退缓存，无副作用）。"""
    try:
        from datetime import datetime as _dt
        return _dt.strptime(date, "%Y%m%d").weekday() < 5
    except Exception:
        return False


def fetch_pool(kind, date=None, force=False):
    """获取某日涨停池（带缓存）。date: YYYYMMDD，默认今天。
    kind: zt | dt | yzt
    返回 [{c,n,zbc,hybk,fbt,lbt,fund,zttj:{days,ct},hs,lb,...}]

    ★ BUG 修复（2026-09-08，用户报告：市场涨跌停 64:0 系统显示 10:0 误判冰点）：
      交易日当日涨停/跌停池是**实时变化**数据；原逻辑 DB 缓存一旦存在就不重拉，
      早盘竞价快照（实测 09:25 仅 10 只涨停）被全天复用 → 情绪 phase=冰点 →
      全局禁止开仓，错过盘中新增涨停。现改为：**当日且工作日 → 先拉网络**
      （mem 缓存兜底频率，盘中 TTL 120s；网络失败回退 DB 缓存），
      历史日期/非交易日仍走 DB 缓存（_save 已有周末脏写清洗门，互不干扰）。
    """
    date = date or time.strftime("%Y%m%d")
    key = (kind, date)
    now = time.time()
    live_today = _is_trading_day(date)
    ttl = (int(getattr(C, "LIMITUP_MEM_TTL_LIVE", 120)) if live_today
           else int(getattr(C, "LIMITUP_MEM_TTL_OFF", 300)))
    # ★SPEED-3：开盘热窗自适应 TTL——9:30-10:10 涨停池高速拓增期 40s（涨停家数/情绪
    #   链路对 R1→R2 切换提前感知）；其余时段维持 120s 不增加源站压力。
    if live_today:
        try:
            hm = time.strftime("%H:%M")
            if "09:30" <= hm <= "10:10":
                ttl = min(ttl, int(getattr(C, "LIMITUP_MEM_TTL_HOT", 40)))
        except Exception:
            pass
    if not force:
        hit = _mem.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    cached = _load(kind, date)
    if live_today:
        # 当日盘中：先拉网络拿实时涨停/跌停池；失败回退 DB 缓存（服务刚启动/网络抖动）
        rows = _fetch_pool(kind, date)
        if rows:
            _save(kind, date, rows)
            _mem[key] = (now, rows)
            return rows
        if cached:
            _mem[key] = (now, cached)
            return cached
        return []
    if cached and not force:
        _mem[key] = (now, cached)
        return cached
    rows = _fetch_pool(kind, date)
    if rows:
        _save(kind, date, rows)
        _mem[key] = (now, rows)
        return rows
    if cached:
        _mem[key] = (now, cached)
        return cached
    return []


def backfill(kind, start_date, end_date=None, verbose=False):
    """历史回补涨停池（逐日）。start/end: YYYYMMDD。
    返回回补天数。可用于构建情绪周期历史序列。
    """
    from datetime import date, timedelta
    end_date = end_date or time.strftime("%Y%m%d")
    d0 = date(int(start_date[:4]), int(start_date[4:6]), int(start_date[6:8]))
    d1 = date(int(end_date[:4]), int(end_date[4:6]), int(end_date[6:8]))
    filled = 0
    d = d0
    while d <= d1:
        ds = d.strftime("%Y%m%d")
        rows = _load(kind, ds)
        if not rows:
            got = _fetch_pool(kind, ds)
            if got:
                _save(kind, ds, got)
                filled += 1
                if verbose:
                    print(f"  {ds} {kind}: {len(got)} 只")
        d += timedelta(days=1)
    return filled


def pool_summary(rows):
    """从涨停池原始行提取情绪指标（单日）：
    涨停家数、连板高度、炸板统计、封板资金、行业分布、连板梯队
    """
    total = len(rows)
    max_days = 0
    ladder = {}          # 连板数 -> 家数
    seal_fund = 0.0
    sectors = {}
    first_board = 0      # 首板（连板1）
    for r in rows:
        zttj = r.get("zttj") or {}
        days = int(zttj.get("days", 1) or 1)
        max_days = max(max_days, days)
        ladder[days] = ladder.get(days, 0) + 1
        seal_fund += float(r.get("fund", 0) or 0)
        hy = r.get("hybk", "") or "其他"
        sectors[hy] = sectors.get(hy, 0) + 1
        if days == 1:
            first_board += 1
    return {
        "total": total,
        "max_days": max_days,          # 空间板（连板高度）
        "first_board": first_board,    # 首板家数
        "seal_fund": round(seal_fund, 0),
        "ladder": ladder,              # 连板梯队 {n: count}
        "sectors": dict(sorted(sectors.items(), key=lambda x: -x[1])[:10]),
    }


def limit_up_pool(date=None, force=False):
    """今日涨停池（简化字段，供外部调用）"""
    rows = fetch_pool("zt", date, force)
    out = []
    for r in rows:
        zttj = r.get("zttj") or {}
        out.append({
            "code": r.get("c", ""), "name": r.get("n", ""),
            "days": int(zttj.get("days", 1) or 1),
            "zbc": r.get("zbc", 0),          # 炸板次数
            "fbt": r.get("fbt", 0),          # 首封时间
            "lbt": r.get("lbt", 0),          # 最后封板时间
            "fund": r.get("fund", 0),        # 封板资金
            "ltsz": r.get("ltsz", 0) or 0,   # ★ Phase17 流通市值（封单资金比值用）
            "hs": r.get("hs", 0),            # 换手率
            "sector": r.get("hybk", ""),     # 行业
            "lb": r.get("lb", 0),            # 量比
        })
    return out


def limit_down_pool(date=None, force=False):
    """今日跌停池"""
    rows = fetch_pool("dt", date, force)
    return [{"code": r.get("c", ""), "name": r.get("n", ""),
             "fund": r.get("fund", 0)} for r in rows]


def yesterday_zt(date=None, force=False):
    """昨日涨停池（今日表现统计原料）。
    东财 getYesterdayZTPool 已停用，用 getTopicZTPool 抓昨日日期替代。
    date: 今日 YYYYMMDD；自动回推最近有涨停的前一交易日。
    """
    from datetime import date as _d, timedelta
    date = date or time.strftime("%Y%m%d")
    # 回推最多 7 天找最近有涨停的日子
    d = _d(int(date[:4]), int(date[4:6]), int(date[6:8])) - timedelta(days=1)
    for _ in range(7):
        ds = d.strftime("%Y%m%d")
        rows = fetch_pool("zt", ds, force)
        if rows:
            return [{"code": r.get("c", ""), "name": r.get("n", ""),
                     "days": (r.get("zttj") or {}).get("days", 1)} for r in rows]
        d -= timedelta(days=1)
    return []


# ==================== 4.1：涨停原因（同花顺涨停揭秘） ====================
def limit_up_reasons(date=None, force=False, limit=100):
    """当日涨停池涨停原因（同花顺 dataapi 免费接口，纯 urllib）。
    date: YYYYMMDD 或 None(自动回退最近交易日，非交易日/盘中无数据时回退上一交易日)。
    返回 [{code, name, reason, first_time, days}]（reason 如"专网通信+电磁线+..."）
    """
    date = date or time.strftime("%Y%m%d")
    key = ("reason", date)
    now = time.time()
    if not force:
        hit = _mem.get(key)
        if hit and now - hit[0] < 300:
            return hit[1]
    # 非交易日/当日无数据：回退最近 7 天
    from datetime import date as _d, timedelta
    d0 = _d(int(date[:4]), int(date[4:6]), int(date[6:8]))
    for back in range(8):
        ds = (d0 - timedelta(days=back)).strftime("%Y%m%d")
        url = ("https://data.10jqka.com.cn/dataapi/limit_up/limit_up_pool?"
               f"page=1&limit={limit}&field=199112,10,9001,330323,330324,330325,330329,"
               "133338,133339,133330,133331,133332,133333,133349,133350,133336,133337,"
               "133335,133334&filter=HS,GEM2STAR&order_field=330324&order_type=0"
               f"&date={ds}&_=1")
        try:
            d = _http_json(url, breaker="ths",   # ★ 4.5：同花顺熔断 + 短超时
                           headers={"Referer": "https://data.10jqka.com.cn/"})
            items = ((d.get("data") or {}).get("info") or [])
            if items:
                out = []
                for s in items:
                    reason = s.get("reason_type") or s.get("133349") or s.get("theme") or ""
                    # 连板数：high_days 文本（"首板"/"2板"）优先，fallback 数字
                    hd = s.get("high_days", "") or ""
                    days = 1
                    if isinstance(hd, str) and "板" in hd:
                        try:
                            days = int(hd.replace("首板", "1").replace("板", ""))
                        except ValueError:
                            days = 1
                    out.append({
                        "code": s.get("code", ""),
                        "name": s.get("name", ""),
                        "reason": reason,
                        "first_time": s.get("first_limit_up_time", ""),
                        "days": days,
                        "date": ds,
                    })
                _mem[key] = (now, out)
                return out
        except Exception:
            continue
        time.sleep(0.5)  # 回退尝试间限流
    return _mem.get(key, (0, []))[1]


def theme_burst(date=None, force=False):
    """题材爆发度：{题材: 涨停家数} Top10（涨停原因中的主题词统计）"""
    rows = limit_up_reasons(date, force, limit=100)
    themes = {}
    for r in rows:
        reason = r.get("reason", "") or ""
        for part in reason.split("+"):
            part = part.strip()
            if part and len(part) <= 12:  # 过滤过长/无效词
                themes[part] = themes.get(part, 0) + 1
    return dict(sorted(themes.items(), key=lambda x: -x[1])[:10])


# ==================== ★ 4.5：资金聚焦榜（封板资金 = 主力真金白银） ====================
def seal_fund_board(date=None, force=False, top=15):
    """封板资金榜：涨停池按封板资金排序（主力资金聚焦方向）。
    返回 [{code,name,fund,days,sector,zbc,ltsz}]（fund 单位：元）。
    """
    rows = fetch_pool("zt", date, force)
    out = []
    for r in rows:
        zttj = r.get("zttj") or {}
        out.append({
            "code": r.get("c", ""), "name": r.get("n", ""),
            "fund": r.get("fund", 0) or 0,
            "days": int(zttj.get("days", 1) or 1),
            "sector": r.get("hybk", ""),
            "zbc": r.get("zbc", 0),
            "ltsz": r.get("ltsz", 0) or 0,   # 流通市值
            "hs": r.get("hs", 0),            # 换手
        })
    out.sort(key=lambda x: -x["fund"])
    return out[:top]


def fund_focus(date=None, force=False):
    """资金聚焦摘要：封板资金 TOP 板块 + 最强封板个股。
    返回 {top_sectors, top_seals, total_seal_fund}"""
    rows = fetch_pool("zt", date, force)
    if not rows:
        return {"top_sectors": [], "top_seals": [], "total_seal_fund": 0}
    sec_fund = {}
    for r in rows:
        sec = r.get("hybk", "") or "其他"
        sec_fund[sec] = sec_fund.get(sec, 0) + (r.get("fund", 0) or 0)
    top_secs = sorted(sec_fund.items(), key=lambda x: -x[1])[:8]
    seals = sorted(rows, key=lambda x: -(x.get("fund", 0) or 0))[:10]
    return {
        "top_sectors": [{"sector": s, "fund": f} for s, f in top_secs],
        "top_seals": [{"code": r.get("c"), "name": r.get("n"),
                       "fund": r.get("fund", 0) or 0,
                       "days": (r.get("zttj") or {}).get("days", 1)} for r in seals],
        "total_seal_fund": round(sum(r.get("fund", 0) or 0 for r in rows), 0),
    }
