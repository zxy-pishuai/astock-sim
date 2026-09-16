# -*- coding: utf-8 -*-
"""资金面数据增强（v3.8）—— 借鉴 akshare / a-stock-data 数据源模式
东方财富 datacenter-web 免费接口（零依赖，urllib）：
  1. 北向持股：RPT_MUTUAL_HOLDSTOCKNORTH_STA（个股沪深股通持股/变动率）
  2. 龙虎榜：RPT_DAILYBILLBOARD_DETAILSNEW（买卖额/净额/上榜原因/换手）
  3. 两融：RPTA_WEB_RZRQ_GGMX（融资余额/融券余额/融资买入额）
数据落 SQLite 磁盘缓存（moneyflow 表），供因子研究/评分/审计使用。
"""
import json
import sqlite3
import threading
import time
import urllib.parse
import urllib.request

from . import config as C
from . import db as _db            # ★ J3：统一连接工厂

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
_REF = "https://data.eastmoney.com/"
_lock = threading.Lock()
_mem = {}          # (kind, code) -> (ts, value)

# ★ 4.5：东财熔断器 —— 接口被 IP 风控掐断时，快速失败并冷却跳过，
#   避免每次调用都挂到超时（实测被封时每只 8.4s+，scan 400 只 → 灾难）
_breaker = {"fails": 0, "until": 0.0}
_BREAK_AFTER = 3          # 连续失败 N 次 → 熔断
_BREAK_COOLDOWN = 600     # 熔断冷却 10 分钟（之后自动重新探测）
_DEFAULT_TIMEOUT = 4      # 短超时：被封时快速失败（原 12s 太久）

# ★ 4.5：慢速经济模式 —— 东财被限速（每请求 >1.5s）时进入只读缓存模式，
#   不再发起新请求（扫描不被拖死；实测限速期单请求 2.5s+，20只×4调用→分把钟）
_ECONOMY = {"until": 0.0, "slow": 0, "slow_ts": 0.0}
_ECONOMY_LATENCY = 1.5
_ECONOMY_SLOW_HITS = 2
_ECONOMY_COOLDOWN = 600


def _http_json(url, timeout=_DEFAULT_TIMEOUT):
    now = time.time()
    if now < _breaker["until"]:
        raise TimeoutError("eastmoney circuit open (cooldown)")
    if now < _ECONOMY["until"]:
        raise TimeoutError("eastmoney economy mode (slow, using cache)")
    t0 = time.time()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA, "Referer": _REF})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", errors="replace"))
        _breaker["fails"] = 0    # 成功 → 重置计数
        # 自适应慢速检测：连续 2 次 >1.5s → 经济模式 10 分钟
        dur = time.time() - t0
        if dur > _ECONOMY_LATENCY:
            _ECONOMY["slow"] += 1
            _ECONOMY["slow_ts"] = now
        else:
            _ECONOMY["slow"] = 0
        if (_ECONOMY["slow"] >= _ECONOMY_SLOW_HITS
                and now - _ECONOMY["slow_ts"] < 30):
            _ECONOMY["until"] = now + _ECONOMY_COOLDOWN
        return data
    except Exception:
        _breaker["fails"] += 1
        if _breaker["fails"] >= _BREAK_AFTER:
            _breaker["until"] = now + _BREAK_COOLDOWN
        raise


def _dc(report_name, page_size=20, sort="TRADE_DATE", desc=True, filter_sql=""):
    """东财 datacenter-web 通用查询"""
    params = {
        "reportName": report_name,
        "columns": "ALL",
        "pageNumber": 1,
        "pageSize": page_size,
        "sortColumns": sort,
        "sortTypes": -1 if desc else 1,
    }
    if filter_sql:
        params["filter"] = filter_sql
    url = ("https://datacenter-web.eastmoney.com/api/data/v1/get?"
           + urllib.parse.urlencode(params))
    d = _http_json(url)
    return (d.get("result") or {}).get("data") or []


def _conn():
    conn = _db.open_rw(C.DB_FILE)   # ★ J3：统一连接工厂（WAL 初始化已由 db.py 一次完成）
    conn.execute("""CREATE TABLE IF NOT EXISTS moneyflow(
        kind TEXT, code TEXT, date TEXT,
        payload TEXT, PRIMARY KEY(kind, code, date))""")
    return conn


def _cache_save(kind, code, rows):
    try:
        with _lock:
            conn = _conn()
            try:
                for r in rows:
                    date = str(r.get("TRADE_DATE", r.get("DATE", "")))[:10]
                    conn.execute(
                        "INSERT OR REPLACE INTO moneyflow(kind,code,date,payload) VALUES(?,?,?,?)",
                        (kind, code, date, json.dumps(r, ensure_ascii=False)))
                conn.commit()
            finally:
                conn.close()
    except Exception:
        pass


def _cache_load(kind, code, limit=5):
    try:
        with _lock:
            conn = _conn()
            try:
                cur = conn.execute(
                    "SELECT date,payload FROM moneyflow WHERE kind=? AND code=? "
                    "ORDER BY date DESC LIMIT ?", (kind, code, limit))
                return [(r[0], json.loads(r[1])) for r in cur.fetchall()]
            finally:
                conn.close()
    except Exception:
        return []


# ==================== 龙虎榜 ====================
def dragon_tiger(limit=20, days=30, force=False):
    """近期龙虎榜明细（全市场）。返回 [{code,name,date,buy_amt,sell_amt,net_amt,reason,change_rate,turnover,explain}]"""
    key = ("lhb", "all")
    now = time.time()
    if not force:
        hit = _mem.get(key)
        if hit and now - hit[0] < 300:
            return hit[1]
    from datetime import date, timedelta
    since = (date.today() - timedelta(days=days)).strftime("%Y-%m-%d")
    flt = f"(TRADE_DATE>='{since}')"
    try:
        rows = _dc("RPT_DAILYBILLBOARD_DETAILSNEW", page_size=limit,
                   filter_sql=flt)  # v3.8: 不再预 quote（_dc 内 urlencode 会处理）
        out = []
        for r in rows:
            out.append({
                "code": r.get("SECURITY_CODE", ""),
                "name": r.get("SECURITY_NAME_ABBR", ""),
                "date": str(r.get("TRADE_DATE", ""))[:10],
                "buy_amt": r.get("BILLBOARD_BUY_AMT") or r.get("SUM_BUY_AMT") or 0,
                "sell_amt": r.get("BILLBOARD_SELL_AMT") or r.get("SUM_SELL_AMT") or 0,
                "net_amt": r.get("BILLBOARD_NET_AMT") or r.get("NET_BS_AMT") or 0,
                "reason": r.get("EXPLANATION", ""),
                "explain": r.get("EXPLAIN", ""),
                "change_rate": r.get("CHANGE_RATE"),
                "turnover": r.get("TURNOVERRATE"),
            })
        _mem[key] = (now, out)
        return out
    except Exception:
        return _mem.get(key, (0, []))[1]


def dragon_tiger_of(code, days=30, force=False):
    """单只股票近期龙虎榜（因子用）。返回列表（空=未上榜）"""
    rows = dragon_tiger(limit=200, days=days, force=force)
    return [r for r in rows if r["code"] == code]


# ==================== 两融 ====================
def margin_of(code, limit=5, force=False):
    """个股融资融券余额（近 N 个交易日）。返回 [{date,rzye(融资余额),rqye(融券余额),rzmre(融资买入额)}]"""
    key = ("rzrq", code)
    now = time.time()
    if not force:
        hit = _mem.get(key)
        if hit and now - hit[0] < 600:
            return hit[1]
    cached = _cache_load("rzrq", code, limit)
    if cached and not force:
        return [{"date": d, "rzye": r.get("RZYE"), "rqye": r.get("RQYE"),
                 "rzmre": r.get("RZMRE"), "rzrqye": r.get("RZRQYE")} for d, r in cached]
    flt = f'(SCODE="{code}")'
    try:
        rows = _dc("RPTA_WEB_RZRQ_GGMX", page_size=limit, sort="DATE", filter_sql=flt)
        out = [{"date": str(r.get("DATE", ""))[:10], "rzye": r.get("RZYE"),
                "rqye": r.get("RQYE"), "rzmre": r.get("RZMRE"),
                "rzrqye": r.get("RZRQYE")} for r in rows]
        _cache_save("rzrq", code, rows)
        _mem[key] = (now, out)
        return out
    except Exception:
        return cached if cached else []


def margin_change_ratio(code, days=5, force=False):
    """两融变化率：近 N 日融资余额变化比例（0.05=5%增长）。数据不足返回 None"""
    rows = margin_of(code, limit=days + 1, force=force)
    if len(rows) < 2:
        return None
    newest = rows[0]
    oldest = rows[-1]
    a, b = newest.get("rzye") or 0, oldest.get("rzye") or 0
    if a <= 0 or b <= 0:
        return None
    return round((a - b) / b, 4)


# ==================== 北向持股 ====================
def northbound_of(code, limit=5, force=False):
    """个股沪深股通持股（最新+历史）。返回 [{date,hold_ratio,change_rate,close}]"""
    key = ("north", code)
    now = time.time()
    if not force:
        hit = _mem.get(key)
        if hit and now - hit[0] < 600:
            return hit[1]
    cached = _cache_load("north", code, limit)
    if cached and not force:
        return [{"date": d, "hold_ratio": r.get("HOLD_MARKET_CAP"),
                 "change_rate": r.get("CHANGE_RATE"), "close": r.get("CLOSE_PRICE")}
                for d, r in cached]
    flt = f'(SECURITY_CODE="{code}")'
    try:
        rows = _dc("RPT_MUTUAL_HOLDSTOCKNORTH_STA", page_size=limit,
                   sort="TRADE_DATE", filter_sql=flt)
        out = []
        for r in rows:
            hold = r.get("HOLD_SHARES") or r.get("HOLD_MARKET_CAP")
            out.append({
                "date": str(r.get("TRADE_DATE", ""))[:10],
                "hold_ratio": r.get("HOLD_RATIO") or r.get("HOLD_MARKET_CAP"),
                "change_rate": r.get("CHANGE_RATE"),
                "close": r.get("CLOSE_PRICE"),
            })
        _cache_save("north", code, rows)
        _mem[key] = (now, out)
        return out
    except Exception:
        return cached if cached else []


def northbound_change(code, force=False):
    """北向持股近 5 日变动率（0.1=增持10%）。数据不足返回 None"""
    rows = northbound_of(code, limit=6, force=force)
    if len(rows) < 2:
        return None
    newest, oldest = rows[0], rows[-1]
    a, b = newest.get("change_rate"), oldest.get("change_rate")
    if a is None or b is None or b == 0:
        return None
    return round(a - b, 4)


# ==================== ★ 4.5：龙虎榜席位深化（游资/机构合力） ====================
# 数据源：东财 datacenter 买卖席位 TOP5 报表 + 本地知名游资名单
# 实战逻辑：机构专用席位净买 = 真金白银；机构+游资同日同股 = 定方向+点火；
#          知名游资持续动作 = 接力信号（区别于单日上榜）

# 本地游资席位名单（社区公开名单，营业部会换名，定期人工校准）
_KNOWN_HOT_MONEY = [
    "中信证券上海溧阳路", "中信证券北京总部", "华鑫证券上海分公司", "华鑫证券上海宛平南路",
    "国泰君安南京太平南路", "国泰君安上海江苏路", "银河证券绍兴", "银河证券北京中关村大街",
    "东方财富拉萨团结路", "东方财富拉萨东环路", "东方财富拉萨东城区", "东方财富拉萨金珠西路",
    "招商证券深圳蛇口工业七路", "申万宏源上海徐汇区龙漕路", "华泰证券深圳益田路",
    "财通证券杭州上塘路", "财通证券杭州体育场路", "东莞证券北京分公司", "光大证券宁波解放南路",
    "华宝证券上海东大名路", "广发证券上海东方路", "国信证券深圳泰然九路",
]
_INSTITUTION = "机构专用"


def _seat_report(report_name, code, date, page_size=5):
    """查询买卖席位报表（RPT_BILLBOARD_DAILYDETAILSBUY / ...SELL）。
    返回 [{dept, buy, sell, net, date}]"""
    try:
        flt = f'(SECURITY_CODE="{code}")(TRADE_DATE=\'{date}\')'
        rows = _dc(report_name, page_size=page_size, sort="BUY", desc=True, filter_sql=flt)
        out = []
        for r in rows:
            out.append({
                "dept": r.get("OPERATEDEPT_NAME", "") or r.get("DEPT_NAME", ""),
                "buy": r.get("BUY") or r.get("BUY_AMT") or 0,
                "sell": r.get("SELL") or r.get("SELL_AMT") or 0,
                "net": (r.get("BUY") or r.get("BUY_AMT") or 0)
                       - (r.get("SELL") or r.get("SELL_AMT") or 0),
            })
        return out
    except Exception:
        return []


def _seat_report_cached(code, date):
    """带缓存的双报表席位查询（买卖各 TOP5）"""
    key = ("seat", code, date)
    now = time.time()
    hit = _mem.get(key)
    if hit and now - hit[0] < 3600:
        return hit[1]
    buys = _seat_report("RPT_BILLBOARD_DAILYDETAILSBUY", code, date)
    sells = _seat_report("RPT_BILLBOARD_DAILYDETAILSSELL", code, date)
    result = {"buys": buys, "sells": sells, "date": date}
    _mem[key] = (now, result)
    return result


def seat_analysis(code, date=None):
    """★ 4.5 龙虎榜席位深度分析：机构净买 + 游资合力 + 席位画像。
    date: YYYY-MM-DD（默认最新上榜日）。
    返回 dict 或 None（未上榜/无数据）：
      {date, inst_net(机构净买额), hot_net(游资净买额), hot_seats(游资名单),
       inst_buy(机构买入额), verdict}
    """
    try:
        from datetime import date as _d, timedelta
        # 找该股最近上榜日（从龙虎榜明细取）
        lhb = dragon_tiger_of(code, days=30, force=False)
        if not lhb:
            return None
        target = date or str(lhb[0].get("date", ""))[:10]
        if len(target) != 10:
            return None
        st = _seat_report_cached(code, target)
        buys, sells = st.get("buys", []), st.get("sells", [])
        if not buys and not sells:
            return None
        # 机构净买 = 买入席位中机构专用 - 卖出席位中机构专用
        inst_buy = sum(r["net"] for r in buys if _INSTITUTION in (r.get("dept") or ""))
        inst_sell = sum(r["net"] for r in sells if _INSTITUTION in (r.get("dept") or ""))
        inst_net = inst_buy - inst_sell
        # 游资净买：买卖席位中的知名游资
        hot_net = 0.0
        hot_seats = []
        for r in buys:
            if any(k in (r.get("dept") or "") for k in _KNOWN_HOT_MONEY):
                hot_net += r.get("net") or 0
                hot_seats.append(r["dept"])
        for r in sells:
            if any(k in (r.get("dept") or "") for k in _KNOWN_HOT_MONEY):
                hot_net -= r.get("net") or 0
        # 结论：机构+游资合力 → 最强；仅机构 → 稳；仅游资 → 点火
        if inst_net > 0 and hot_net > 0:
            verdict = "机构+游资合力（定方向+点火，最强信号）"
        elif inst_net > 0:
            verdict = f"机构净买 {inst_net/1e4:.0f}万（真金白银，溢价持续性更强）"
        elif hot_net > 0:
            verdict = f"游资净买 {hot_net/1e4:.0f}万（题材点火，注意一日游）"
        elif inst_net < 0 or hot_net < 0:
            verdict = "机构/游资净卖出（分歧或出货，谨慎）"
        else:
            verdict = "席位无明显机构/游资动作"
        return {
            "date": target, "inst_net": round(inst_net, 2),
            "hot_net": round(hot_net, 2), "inst_buy": round(inst_buy, 2),
            "hot_seats": hot_seats[:5],
            "buy_count": len(buys), "sell_count": len(sells),
            "verdict": verdict,
        }
    except Exception:
        return None
