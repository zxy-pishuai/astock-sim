# -*- coding: utf-8 -*-
"""★ Phase18 全球外盘行情模块
数据源（都实测过格式，urllib 直连 + UA/Referer）：
  1) 腾讯 qt.gtimg.cn —— 主力：美股/港股指数 + 全部个股 + 日韩个股（GBK，~ 分隔；
     ★实测统一：f[3]=最新价 f[4]=昨收 f[31]=涨跌额 f[32]=涨跌幅%
     ——用 NVDA / 腾讯控股 / 000660.KS / 7203.T 校准一致）
  2) 东财 push2 ulist.np —— 日韩欧台指数主源（本机 IP 可能被阻断）
  2b) Yahoo Finance chart —— 日韩欧指数备用源：东财缺失时自动补位，
     独立熔断器；东财解除阻断后自动恢复优先
  3) 新浪 hq.sinajs.cn —— 独占：离岸人民币/COMEX黄金/WTI原油/美元指数（GBK）；
     涨跌幅由昨收/昨结计算（hf_[7]、fx_[2]，实测校准），缺失时 global_kline 兜底
     + push2his 指数日K（历史序列，探测可用则用，否则本地轮询积累）
  3) 新浪 hq.sinajs.cn —— 独占：离岸人民币/COMEX黄金/WTI原油/美元指数（GBK）
每源独立熔断器（连续失败3次冷却10分钟，参考 limitup._breakers），互不影响。
内存缓存：各市场交易时段内 30s、休市 5min（北京时间；含夏令时规则）。
"""
import json
import sqlite3
import threading
import time
import urllib.parse
import urllib.request

from . import config as C
from . import db as _db            # ★ J3：统一连接工厂（★ 验收修复 2026-09-15：补回缺失导入）

# ============ ★ Phase19 A股映射提示规则（纯展示，不参与交易逻辑；可在此增删） ============
# 每条: (触发条件 dict, 提示文案模板)。条件值=相关品种代码或字段 + 阈值（带方向）。
# 触发时用最新行情计算；缺失字段自动跳过该规则。
ASHARE_MAP_RULES = [
    # 半导体链：纳指 ±1.5% 且 英伟达 ±3%
    {"name": "半导体链", "cond": [("usIXIC", 1.5, "and"), ("usNVDA", 3.0, "and")],
     "text": "{dir}半导体链情绪偏强（纳指{p1:.1f}%、英伟达{p2:.1f}%）",
     "dir_code": "usIXIC"},
    # 港股科技映射：恒生科技 ±2%
    {"name": "港股科技", "cond": [("hkHSTECH", 2.0, "or")],
     "text": "{dir}港股科技映射（恒科{p1:.1f}%）", "dir_code": "hkHSTECH"},
    # ★ Phase57：港股科技低阈值实验组——P39 回测结论 ±2% 把有效触发滤掉了
    #   （±50% 扰动下命中率稳定 57~58%），前瞻台账按 ±1% 单独记账，
    #   满 60 触发日按设计文档口径判定后再决定是否转正/下线。
    {"name": "港股科技±1%", "cond": [("hkHSTECH", 1.0, "or")],
     "text": "[实验]{dir}港股科技映射±1%（恒科{p1:.1f}%）", "dir_code": "hkHSTECH",
     "experimental": True},
    # 油气：WTI ±3%
    {"name": "油气", "cond": [("CL", 3.0, "or")],
     "text": "{dir}油气（WTI{p1:.1f}%）：利好上游/航空成本承压", "dir_code": "CL"},
    # 股指波动：VIX > 25
    {"name": "全球避险", "cond": [("usVIX", 25.0, "vix_gt")],
     "text": "VIX {p1:.1f} >25：全球避险，注意仓位", "dir_code": None},
    # 汇率：离岸人民币日内变动 >0.3%
    {"name": "汇率", "cond": [("USDCNH", 0.3, "fx_abs")],
     "text": "离岸人民币变动 {p1:.2f}%（{p2}）：汇率压力", "dir_code": None},
]


def a_share_hint(quotes=None):
    """计算 A股映射提示（纯展示）。quotes: 行情 dict（缺省内部拉取）。
    返回 [{rule, text, dir}]（dir: up/down/flag）。"""
    q = quotes if quotes is not None else all_quotes()
    out = []
    for rule in ASHARE_MAP_RULES:
        try:
            vals = []
            matched = False
            logic = "and"
            for cond in rule["cond"]:
                code, thr, mode = cond[0], cond[1], cond[2]
                it = q.get(code) or {}
                v = it.get("pct")
                px = it.get("price")
                if v is None:
                    # 商品/汇无 pct → 用价格差值近似（无昨收时跳过）
                    vals.append(None)
                    continue
                if mode == "and":
                    logic = "and"
                if mode == "vix_gt":
                    matched = (px or 0) > thr
                    vals.append(px or 0)
                elif mode == "fx_abs":
                    # FX 无 pct：用 price 与 prev_close 差（若有）；否则跳过
                    pc = it.get("prev_close")
                    if pc and pc > 0:
                        chg = abs((px - pc) / pc) * 100
                        matched = chg > thr
                        vals.append(chg)
                        vals.append(px if px else "")
                    else:
                        vals.extend([None, ""])
                else:
                    matched = abs(v) >= thr
                    vals.append(v)
            if not matched:
                continue
            d = "up" if (rule.get("dir_code") and q.get(rule["dir_code"], {}).get("pct", 0) or 0) > 0 else ("down" if rule.get("dir_code") else "flag")
            txt = rule["text"]
            # 格式化占位符
            p = dict((str(k), v) for k, v in enumerate(vals))
            try:
                txt = txt.format(dir={"up": "📈", "down": "📉", "flag": "⚡"}[d], **p)
            except Exception:
                txt = rule["name"] + "（触发）"
            out.append({"rule": rule["name"], "text": txt, "dir": d})
        except Exception:
            continue
    return out

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

# ---------- 数据源熔断器（每源独立） ----------
_breakers = {}     # src -> {fails, until}
_BREAK_AFTER = 3
_BREAK_COOLDOWN = 600
_DEFAULT_TIMEOUT = 6


def _src_open(src):
    b = _breakers.get(src)
    if b and time.time() < b["until"]:
        raise TimeoutError("circuit open: %s" % src)


# ★B-3 直连 opener：绕开系统代理。实测（2026-09-16）壳环境 HTTP(S)_PROXY=127.0.0.1:7890，
# 代理进程一旦关闭 → 所有外网请求 ConnectionRefused → 行情拉空 → 界面"冻结"。
# 腾讯/新浪/东财均为国内直连源（实测绕代理 0.21s 成功，走代理反而被拒），一律直连；
# 仅 yahoo（墙外源）继续吃系统代理。
_DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_PROXY_OPENER = urllib.request.build_opener()   # 跟随环境代理


def _request(url, ref, decode="utf-8", timeout=_DEFAULT_TIMEOUT, src="tx",
             send=None, direct=True):
    """带熔断的 HTTP GET。返回解码文本。send: 可选自定义 urlopen（如带 cookie 的
    opener），签名 send(req) -> response。
    direct=True（默认）：忽略代理环境变量直连（国内行情源）；
    direct=False：跟随系统代理（墙外源用）。"""
    _src_open(src)
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": _UA, "Referer": ref})
        if send is not None:
            opener = send
        elif direct:
            opener = lambda r, _o=_DIRECT_OPENER, _t=timeout: _o.open(r, timeout=_t)
        else:
            opener = lambda r, _o=_PROXY_OPENER, _t=timeout: _o.open(r, timeout=_t)
        with opener(req) as r:
            data = r.read().decode(decode, errors="replace")
        b = _breakers.get(src)
        if b:
            b["fails"] = 0
        return data
    except Exception:
        b = _breakers.setdefault(src, {"fails": 0, "until": 0.0})
        b["fails"] += 1
        if b["fails"] >= _BREAK_AFTER:
            b["until"] = time.time() + _BREAK_COOLDOWN
        raise


def _http_json(src, url, ref):
    return json.loads(_request(url, ref, src=src))

# ---------- 缓存与交易时段 ----------
_cache = {}      # key -> (ts, value)
_clk = threading.Lock()
_LIVE_TTL = 30      # 交易时段
_CLOSE_TTL = 300    # 休市


def _is_dst_us():
    """美股夏令时：3月第2个周日 ~ 11月第1个周日（简化：按日期段近似）"""
    y = time.localtime().tm_year
    def second_sunday(m):
        import datetime
        d = datetime.date(y, m, 1)
        # 第一个周日
        d += datetime.timedelta(days=(6 - d.weekday()) % 7)
        return d + datetime.timedelta(days=7)  # 第二个周日
    def first_sunday(m):
        import datetime
        d = datetime.date(y, m, 1)
        return d + datetime.timedelta(days=(6 - d.weekday()) % 7)
    s = second_sunday(3)
    e = first_sunday(11)
    import datetime
    today = datetime.date.today()
    return s <= today < e


def _is_dst_eu():
    """欧洲夏令时：3月最后周日 ~ 10月最后周日"""
    import datetime
    y = time.localtime().tm_year
    def last_sunday(m):
        d = datetime.date(y, m, 31)
        d = d.replace(day=28)
        d += datetime.timedelta(days=(6 - d.weekday()) % 7)
        while d.month != m:   # 后退到当月末周日
            d -= datetime.timedelta(days=7)
        return d
    s = last_sunday(3)
    e = last_sunday(10)
    today = datetime.date.today()
    return s <= today < e


def _hm():
    l = time.localtime()
    return l.tm_hour * 100 + l.tm_min


def _trading_us():
    """美股盘中（北京时间）"""
    dst = _is_dst_us()
    s, e = (2130, 400) if dst else (2230, 500)
    hm = _hm()
    if hm >= s or hm < e:   # 跨午夜
        return True
    return False


def _trading_hk():
    hm = _hm()
    return 930 <= hm <= 1600


def _trading_jp():
    hm = _hm()
    return 800 <= hm <= 1400


def _trading_kr():
    hm = _hm()
    return 830 <= hm <= 1530


def _trading_eu():
    dst = _is_dst_eu()
    s = 1400 if dst else 1500  # 欧股开盘（夏令时提前1小时）
    hm = _hm()
    return s <= hm <= 2330


def _ttl_for(market):
    return _LIVE_TTL if _trading_now(market) else _CLOSE_TTL


def _trading_now(market):
    return {"us": _trading_us, "hk": _trading_hk, "jp": _trading_jp,
            "kr": _trading_kr, "eu": _trading_eu}.get(market, lambda: False)()


def _cache_get(key):
    with _clk:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < hit[1][0]:
            return hit[1][1]
    return None


def _cache_set(key, ttl, value):
    with _clk:
        _cache[key] = (time.time(), (ttl, value))


def _cache_ts(key):
    """缓存条目的真实抓取时间（0=无）。★B-1 修复：给缓存数据盖"数据时间"而非当前时间。"""
    with _clk:
        hit = _cache.get(key)
        return hit[0] if hit else 0.0

# ---------- 腾讯（主力） ----------
_TX_CODES = ("usDJI,usIXIC,usINX,usVIX,hkHSI,hkHSTECH,hkHSCEI,"
             "usNVDA,usAAPL,usTSLA,usMSFT,usGOOGL,usAMZN,usTSM,usMETA,usASML,"
             "hk00700,hk09988,hk03690,"
             "kr000660,kr005930,jp7203,jp6758")

# ★ SYMBOLS 注册表：全部跟踪标的（指数+个股），带市场/币种/类别标签
SYMBOLS = {
    # -- 指数 --
    "usDJI":    {"name": "道琼斯",      "market": "us", "currency": "",    "kind": "index"},
    "usIXIC":   {"name": "纳斯达克",    "market": "us", "currency": "",    "kind": "index"},
    "usINX":    {"name": "标普500",     "market": "us", "currency": "",    "kind": "index"},
    "usVIX":    {"name": "VIX恐慌指数", "market": "us", "currency": "",    "kind": "index"},
    "hkHSI":    {"name": "恒生指数",    "market": "hk", "currency": "",    "kind": "index"},
    "hkHSTECH": {"name": "恒生科技",    "market": "hk", "currency": "",    "kind": "index"},
    "hkHSCEI":  {"name": "国企指数",    "market": "hk", "currency": "",    "kind": "index"},
    # -- 美股个股 --
    "usNVDA":  {"name": "英伟达",     "market": "us", "currency": "USD", "kind": "stock"},
    "usAAPL":  {"name": "苹果",       "market": "us", "currency": "USD", "kind": "stock"},
    "usTSLA":  {"name": "特斯拉",     "market": "us", "currency": "USD", "kind": "stock"},
    "usMSFT":  {"name": "微软",       "market": "us", "currency": "USD", "kind": "stock"},
    "usGOOGL": {"name": "谷歌A",      "market": "us", "currency": "USD", "kind": "stock"},
    "usAMZN":  {"name": "亚马逊",     "market": "us", "currency": "USD", "kind": "stock"},
    "usTSM":   {"name": "台积电ADR",  "market": "us", "currency": "USD", "kind": "stock"},
    "usMETA":  {"name": "Meta",       "market": "us", "currency": "USD", "kind": "stock"},
    "usASML":  {"name": "阿斯麦ADR",  "market": "us", "currency": "USD", "kind": "stock"},
    # -- 港股个股 --
    "hk00700": {"name": "腾讯控股",   "market": "hk", "currency": "HKD", "kind": "stock"},
    "hk09988": {"name": "阿里巴巴-W", "market": "hk", "currency": "HKD", "kind": "stock"},
    "hk03690": {"name": "美团-W",     "market": "hk", "currency": "HKD", "kind": "stock"},
    # -- 日韩个股 --
    "jp7203":  {"name": "丰田",       "market": "jp", "currency": "JPY", "kind": "stock"},
    "jp6758":  {"name": "索尼",       "market": "jp", "currency": "JPY", "kind": "stock"},
    "kr000660": {"name": "SK海力士",  "market": "kr", "currency": "KRW", "kind": "stock"},
    "kr005930": {"name": "三星电子",  "market": "kr", "currency": "KRW", "kind": "stock"},
}
_STOCK_CODES = tuple(c for c, s in SYMBOLS.items() if s["kind"] == "stock")
_TX_INDEX_CODES = tuple(c for c, s in SYMBOLS.items() if s["kind"] == "index")
# 腾讯返回 sym（f[2]）与请求 code 的附加对应（实测：000660.KS / 7203.T）
_TX_SYM_EXTRA = {
    "kr000660": "000660.KS", "kr005930": "005930.KS",
    "jp7203": "7203.T", "jp6758": "6758.T",
}


def _fetch_tx():
    url = "https://qt.gtimg.cn/q=" + _TX_CODES
    raw = _request(url, "https://gu.qq.com/", decode="gbk", src="tx")
    out = {}
    # 反向映射：请求 code -> 可接受的 sym（f[2]）
    want = {}
    for code in _TX_CODES.split(","):
        bare = code[2:] if len(code) > 2 and code[:2] in ("us", "hk") else code
        acc = {bare, ".%s" % bare, bare.split(".")[0]}
        extra = _TX_SYM_EXTRA.get(code)
        if extra:
            acc.add(extra)
        want[code] = acc
    for line in raw.strip().split(";"):
        line = line.strip()
        if not line.startswith("v_"):
            continue
        try:
            body = line.split("=", 1)[1].strip().strip('"')
            f = body.split("~")
            if len(f) < 33:
                continue
            sym = f[2]
            # 归一化候选：原始 sym / 去后缀（NVDA.OQ→NVDA）/.前缀（.DJI）
            base = sym.split(".")[0]
            cands = {sym, base, "." + base}
            code = None
            for c, acc in want.items():
                if cands & acc:
                    code = c
                    break
            if not code:
                continue
            # ★实测统一字段（NVDA/腾讯控股/000660.KS/7203.T 校准一致）：
            # f[3]=最新价 f[4]=昨收 f[31]=涨跌额 f[32]=涨跌幅%（指数/个股一致）
            chg = float(f[31]) if f[31] else None
            pct = float(f[32]) if len(f) > 32 and f[32] else None
            reg = SYMBOLS.get(code, {})
            out[code] = {
                "code": code,
                "name": reg.get("name") or f[1],
                "price": float(f[3] or 0),
                "prev_close": float(f[4] or 0),
                "chg": chg, "pct": pct,
                "market": reg.get("market") or _tx_market(code),
                "currency": reg.get("currency", ""),
                "kind": reg.get("kind", "stock"),
                "source": "tx",
            }
        except Exception:
            continue
    return out


def _tx_market(code):
    if code[:2] in ("us", "hk", "kr", "jp"):
        return code[:2]
    return "other"

# ---------- 东财 push2（日韩欧台；主源 + push2delay 延迟镜像） ----------
_EM_SECIDS = {
    "N225": "日经225", "KS11": "韩国KOSPI", "TWII": "台湾加权",
    "FTSE": "英国富时100", "DAX": "德国DAX", "FCHI": "法国CAC",
}
_EM_VARIANTS = {"DAX": ["100.GDAXI", "100.DAX30"], "FCHI": ["100.CAC"]}
# ★2026-09-11 修复：主源 push2.eastmoney.com 对本机 IP 持续 RemoteDisconnected（风控）；
#   push2delay.eastmoney.com 为东财延迟行情镜像（同协议同字段），作自动降级第二宿主。
#   两宿主独立熔断（src=em / emdelay），主源恢复后自动回到优先位。
_EM_HOSTS = ("push2.eastmoney.com", "push2delay.eastmoney.com")


def _em_quote(code, x, src="em"):
    """东财 ulist 字段：f2=最新价 f3=涨跌幅% f4=涨跌额 f12=代码 f14=名称。
    ★2026-09-11 实测：f4 是涨跌额（如 -1726.83），不是昨收——昨收 = price - chg。"""
    price = x.get("f2")
    chg = x.get("f4")
    prev_close = None
    if isinstance(price, (int, float)) and isinstance(chg, (int, float)) \
            and price is not None and chg is not None:
        prev_close = round(price - chg, 2)
    return {
        "code": code, "name": x.get("f14", _EM_SECIDS.get(code, code)),
        "price": price, "prev_close": prev_close,
        "chg": chg, "pct": x.get("f3"),
        "market": _em_market(code), "source": src,
    }


def _em_pull(host):
    """单宿主拉取：返回 {code: quote}（空 dict = 宿主可达但无数据）。"""
    out = {}
    bases = ["100.%s" % k for k in _EM_SECIDS]
    url = ("https://%s/api/qt/ulist.np/get?" % host
           + urllib.parse.urlencode({
               "secids": ",".join(bases),
               "fields": "f2,f3,f4,f12,f14", "fltt": "2"}))
    src = "em" if host == "push2.eastmoney.com" else "emdelay"
    raw = _request(url, "https://quote.eastmoney.com/", src=src)
    d = json.loads(raw)
    diff = (d.get("data") or {}).get("diff") or []
    for x in diff:
        code = x.get("f12", "")
        if code in _EM_SECIDS:
            out[code] = _em_quote(code, x, src)
    # DAX / FCHI 探测变体（若基础 secid 无效）
    for k, variants in _EM_VARIANTS.items():
        if k in out:
            continue
        for v in variants[:2]:
            try:
                u2 = ("https://%s/api/qt/ulist.np/get?" % host
                      + urllib.parse.urlencode({
                          "secids": v, "fields": "f2,f3,f4,f12,f14", "fltt": "2"}))
                d2 = json.loads(_request(u2, "https://quote.eastmoney.com/", src=src))
                dd = ((d2.get("data") or {}).get("diff") or [])
                if dd:
                    out[k] = _em_quote(k, dd[0], src)
                    break
            except Exception:
                continue
    return out


def _fetch_em():
    """日韩欧台指数：主源 push2 失败/空 → 自动降级 push2delay 延迟镜像。
    两宿主都不可用时返回 {}（由 all_quotes 交给 Yahoo 备用/前端展示不可用）。"""
    for host in _EM_HOSTS:
        try:
            out = _em_pull(host)
            if out:
                return out
        except Exception:
            continue
    return {}


def _em_market(code):
    return {"N225": "jp", "KS11": "kr", "TWII": "hk"}.get(code, "eu")

# ---------- Yahoo Finance（日韩欧指数备用源；东财被阻断时自动补位） ----------
# ★验收修复：Yahoo 符号→系统代码（对齐东财/前端；GDAXI→DAX）
_YAHOO_KEYMAP = {"GDAXI": "DAX"}
_YAHOO_SYMS = (
    ("N225", "日经225", "jp"), ("KS11", "韩国KOSPI", "kr"),
    ("GDAXI", "德国DAX", "eu"), ("FTSE", "英国富时100", "eu"),
)
_yahoo_opener = None


def _yahoo_send(req):
    """带 cookie 握手的 Yahoo urlopen（部分网络需先取 A3 cookie）。"""
    global _yahoo_opener
    import http.cookiejar
    if _yahoo_opener is None:
        cj = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(cj))
        try:
            # fc.yahoo.com 固定 404，仅为取 cookie
            opener.open(urllib.request.Request(
                "https://fc.yahoo.com",
                headers={"User-Agent": _UA}), timeout=4)
        except Exception:
            pass
        _yahoo_opener = opener
    return _yahoo_opener.open(req, timeout=8)


def _fetch_yahoo(want_codes=None):
    """Yahoo chart 备用源：只拉 want_codes（缺省全部）。
    返回 {code: quote}，code 与东财一致（N225/KS11/DAX/FTSE）。
    meta.regularMarketPrice=最新；涨跌幅用 bars 最近两根收盘计算（回退 chartPreviousClose）。"""
    out = {}
    for sym, cname, mkt in _YAHOO_SYMS:
        ocode = _YAHOO_KEYMAP.get(sym, sym)
        if want_codes and ocode not in want_codes:
            continue
        try:
            raw = _request(
                "https://query1.finance.yahoo.com/v8/finance/chart/%5E" + sym
                + "?range=5d&interval=1d",
                "https://finance.yahoo.com/", src="yahoo",
                send=_yahoo_send)
            result = (json.loads(raw).get("chart") or {}).get("result") or []
            meta = (result[0].get("meta") or {}) if result else {}
            price = meta.get("regularMarketPrice")
            # ★验收修复：chartPreviousClose 基准日各标的不一致（KOSPI 曾误显 +6.83%，实际 +0.88%）
            # 改用 bars 最近两根非空收盘自算涨跌幅；无 bars 时回退 chartPreviousClose
            closes = []
            try:
                _q0 = ((result[0].get("indicators") or {}).get("quote") or [{}])
                closes = [c for c in (_q0[0].get("close") or []) if c]
            except Exception:
                closes = []
            if len(closes) >= 2:
                prev = closes[-2]
                if price is None:
                    price = closes[-1]
            else:
                prev = meta.get("chartPreviousClose")
            if price is None or not prev:
                continue
            out[ocode] = {
                "code": ocode, "name": cname, "price": float(price),
                "prev_close": float(prev), "chg": None,
                "pct": round((float(price) - float(prev)) / float(prev) * 100, 2),
                "market": mkt, "currency": "", "kind": "index",
                "source": "yahoo",
            }
        except Exception:
            continue
    return out

# ---------- 东财 push2his 指数日K（历史；探测可用则用） ----------
_HIS_PROBED = {"ok": False, "ts": 0}


def _probe_push2his():
    if time.time() - _HIS_PROBED["ts"] < 3600:
        return _HIS_PROBED["ok"]
    try:
        url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?"
               + urllib.parse.urlencode({
                   "secid": "100.DJIA", "klt": "101", "fqt": "1", "lmt": "3",
                   "fields1": "f1,f2,f3", "fields2": "f51,f53"}))
        d = _http_json("emhis", url, "https://quote.eastmoney.com/")
        kl = (d.get("data") or {}).get("klines") or []
        _HIS_PROBED["ok"] = bool(kl)
    except Exception:
        _HIS_PROBED["ok"] = False
    _HIS_PROBED["ts"] = time.time()
    return _HIS_PROBED["ok"]


def fetch_history_push2his(sym, days=60):
    """优先用 push2his 取指数日K（收盘序列）。sym: DJIA/NDX/SPX/HSI/..."""
    if not _probe_push2his():
        return None   # 探测失败 → 本地积累路径
    secid = {"DJIA": "100.DJIA", "NDX": "100.NDX", "SPX": "100.SPX",
             "HSI": "100.HSI"}.get(sym, "100." + sym)
    try:
        url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?"
               + urllib.parse.urlencode({
                   "secid": secid, "klt": "101", "fqt": "1", "lmt": str(days),
                   "fields1": "f1,f2,f3", "fields2": "f51,f53"}))
        d = _http_json("emhis", url, "https://quote.eastmoney.com/")
        kl = (d.get("data") or {}).get("klines") or []
        seq = []
        for line in kl:
            parts = line.split(",")
            if len(parts) >= 2:
                seq.append({"date": parts[0], "close": float(parts[1])})
        return seq
    except Exception:
        return None

# ---------- 历史落库（push2his 可用则写，否则本地轮询积累当日收盘） ----------
def _save_kline(market, sym, close):
    """落库 global_kline（主键 sym,date；INSERT OR REPLACE）。失败静默。"""
    try:
        conn = _db.open_rw(C.DB_FILE)   # ★ J3：统一连接工厂（WAL 初始化已由 db.py 一次完成）
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS global_kline("
                "market TEXT, sym TEXT, date TEXT, close REAL, "
                "PRIMARY KEY(sym, date))")
            today = time.strftime("%Y-%m-%d")
            conn.execute(
                "INSERT OR REPLACE INTO global_kline(market,sym,date,close) "
                "VALUES(?,?,?,?)", (market, sym, today, close))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def load_history(sym, days=60):
    """从 global_kline 读历史（本地积累路径）"""
    try:
        conn = _db.open_ro(C.DB_FILE, 10000)   # ★ J3：统一连接工厂（读复用）
        rows = conn.execute(
            "SELECT date, close FROM global_kline WHERE sym=? "
            "ORDER BY date DESC LIMIT ?", (sym, days)).fetchall()
        return [{"date": r[0], "close": r[1]} for r in rows][::-1]
    except Exception:
        return []

# ---------- 新浪（独占品种） ----------
def _pct_of(price, prev):
    """由现价/昨收（昨结）计算涨跌幅%；入参无效返回 None。"""
    try:
        price, prev = float(price), float(prev)
        if price > 0 and prev > 0:
            return round((price - prev) / prev * 100, 2)
    except Exception:
        pass
    return None


def _fill_pct_from_kline(item):
    """pct 缺失时用 global_kline 昨日收盘兜底计算。"""
    if item.get("pct") is not None or not item.get("price"):
        return item
    hist = load_history(item["code"], days=5)
    if len(hist) >= 2 and hist[-2].get("close"):
        item["pct"] = _pct_of(item["price"], hist[-2]["close"])
    return item


def _fetch_sina():
    url = "https://hq.sinajs.cn/list=fx_susdcnh,hf_GC,hf_CL,DINIW"
    raw = _request(url, "https://finance.sina.com.cn/", decode="gbk", src="sina")
    out = {}
    lines = raw.split(";")
    for line in lines:
        line = line.strip()
        if '"' not in line:
            continue
        val = line.split('"')[1].split(",")
        # ★实测字段校准：
        #   hf_ 期货：[0]=最新 [7]=昨结（结算价）[12]=日期
        #   fx_s 外汇：[1]=最新 [2]=昨收
        #   DINIW 美元指数：[1]=最新 [2]=昨收（实测常与现价相同→走 kline 兜底）
        try:
            if "hf_GC" in line:
                it = {"code": "GC", "name": "COMEX黄金",
                      "price": float(val[0] or 0),
                      "prev_close": float(val[7] or 0),
                      "market": "fx", "currency": "USD",
                      "kind": "commodity", "source": "sina"}
            elif "hf_CL" in line:
                it = {"code": "CL", "name": "WTI原油",
                      "price": float(val[0] or 0),
                      "prev_close": float(val[7] or 0),
                      "market": "fx", "currency": "USD",
                      "kind": "commodity", "source": "sina"}
            elif "fx_susdcnh" in line:
                it = {"code": "USDCNH", "name": "离岸人民币",
                      "price": float(val[1] or 0),
                      "prev_close": float(val[2] or 0),
                      "market": "fx", "currency": "CNH",
                      "kind": "fx", "source": "sina"}
            elif "DINIW" in line:
                price, prev = float(val[1] or 0), float(val[2] or 0)
                it = {"code": "DINIW", "name": "美元指数",
                      "price": price,
                      "prev_close": prev,
                      "market": "fx", "currency": "",
                      "kind": "fx", "source": "sina"}
                # 昨收字段与现价相同时不可信 → 置 0 走 kline 兜底
                if prev and abs(prev - price) < 1e-9:
                    it["prev_close"] = 0
            else:
                continue
            it["chg"] = None
            it["pct"] = _pct_of(it.get("price"), it.get("prev_close"))
            out[it["code"]] = it
            # 当日价落库（供次日 pct 的 global_kline 兜底）
            if it.get("price"):
                _save_kline(it["market"], it["code"], it["price"])
        except Exception:
            continue
    for it in out.values():
        _fill_pct_from_kline(it)
    return out

# ---------- 汇总入口 ----------
def all_quotes():
    """返回全部市场快照 {code: quote}。三源独立拉取，单源失败不影响其他。"""
    with _clk:
        _lock = _clk  # 复用锁
    out = {}
    now0 = time.time()
    # 腾讯
    q = _cache_get("quotes_tx")
    if q is None:
        try:
            q = _fetch_tx()
            # ★B-1：空结果简短缓存（10s）——此前空结果被缓存 300 秒，一次源抖动=界面冻结 5 分钟
            _cache_set("quotes_tx", _LIVE_TTL if q else 10, q)
            # 顺带落库：美国/港股指数当日收盘（若有收盘时间）
        except Exception:
            q = {}
    out.update(q)
    # 东财
    q2 = _cache_get("quotes_em")
    if q2 is None:
        try:
            q2 = _fetch_em()
            # ★B-1：空结果 10s 短缓存
            _cache_set("quotes_em", _LIVE_TTL if q2 else 10, q2)
        except Exception:
            q2 = {}
    out.update(q2)
    # Yahoo 备用源：仅补东财缺失的日韩欧指数（独立熔断，不影响东财恢复）
    _missing = {c for c in ("N225", "KS11", "DAX", "FTSE") if c not in q2}
    if _missing:
        qy = _cache_get("quotes_yahoo")
        if qy is None:
            try:
                qy = _fetch_yahoo(_missing)
                _cache_set("quotes_yahoo", _LIVE_TTL if qy else 10, qy)
            except Exception:
                qy = {}
        for c, it in qy.items():
            if c in _missing:
                out[c] = it
    # 新浪
    q3 = _cache_get("quotes_sina")
    if q3 is None:
        try:
            q3 = _fetch_sina()
            _cache_set("quotes_sina", _LIVE_TTL if q3 else 10, q3)
        except Exception:
            q3 = {}
    out.update(q3)
    # 交易状态标注 + 真实"数据时间"（★B-1：原实现对缓存值盖章当前时间，
    # 造成"时间在走、数值不动"的假实时观感——外围数据被误以为冻结）
    src_ts = {k: _cache_ts(k) for k in
              ("quotes_tx", "quotes_em", "quotes_yahoo", "quotes_sina")}
    for c, it in out.items():
        it["trading"] = _trading_now(it.get("market", "other"))
        ts = src_ts.get("quotes_%s" % it.get("source", "")) or now0
        it["data_ts"] = ts
        it["stale"] = (now0 - ts) > 600   # 数据 Older than 10 分钟 → 前端可如实标注
        it["updated"] = time.strftime("%H:%M:%S", time.localtime(ts))
    return out


def history(sym, days=60):
    """指数日K序列：先 push2his（探测可用），否则本地 global_kline 积累。"""
    seq = None
    try:
        seq = fetch_history_push2his(sym, days) if not _HIS_PROBED.get("force_local") else None
    except Exception:
        seq = None
    if seq is None:
        seq = load_history(sym, days)   # 本地轮询积累路径
    return seq or []


def movers():
    """★ 外盘个股异动：全部跟踪个股中 |涨跌幅|≥2% 降序 top8；
    不足 3 只时阈值降到 1.5% 并标注 lowered=True。"""
    q = all_quotes()
    pool = []
    for c in _STOCK_CODES:
        it = q.get(c)
        if not it or it.get("pct") is None:
            continue
        pool.append({
            "code": c, "name": it.get("name") or c,
            "market": it.get("market"), "currency": it.get("currency", ""),
            "price": it.get("price"), "pct": it.get("pct"),
        })

    def pick(thr):
        hit = [x for x in pool if abs(x["pct"]) >= thr]
        hit.sort(key=lambda x: abs(x["pct"]), reverse=True)
        return hit[:8]

    items = pick(2.0)
    lowered = False
    if len(items) < 3:
        items = pick(1.5)
        lowered = True
    return {
        "items": items,
        "threshold": 1.5 if lowered else 2.0,
        "lowered": lowered,
        "count": len(items),
        "pool_size": len(pool),
        "updated": time.strftime("%H:%M:%S"),
    }


def summary():
    """全球情绪摘要：上涨市场数 / VIX 分档 / 最强最弱市场"""
    q = all_quotes()
    # 只统计真正的"市场指数"（排除 VIX/汇率/商品/个股）
    mk = {c: it for c, it in q.items()
          if it.get("pct") is not None
          and it.get("market") in ("us", "hk", "jp", "kr", "eu")
          and c not in ("usVIX",)
          and SYMBOLS.get(c, {}).get("kind") != "stock"}
    pct_vals = {c: it["pct"] for c, it in mk.items()}
    up = sum(1 for v in pct_vals.values() if v > 0)
    down = sum(1 for v in pct_vals.values() if v < 0)
    strong = max(pct_vals.items(), key=lambda kv: kv[1]) if pct_vals else None
    weak = min(pct_vals.items(), key=lambda kv: kv[1]) if pct_vals else None
    # VIX 分档
    vix = q.get("usVIX", {}).get("price")
    if vix is None:
        vix_level = "数据不可用"
    elif vix < 18:
        vix_level = "低波动（<18，市场平静）"
    elif vix < 25:
        vix_level = "中等（18-25，正常波动）"
    else:
        vix_level = "高波动（≥25，恐慌/风险）"
    return {
        "markets_up": up, "markets_down": down, "total": len(pct_vals),
        "vix": vix, "vix_level": vix_level,
        "strongest": {"code": strong[0], "name": q[strong[0]]["name"], "pct": strong[1]} if strong else None,
        "weakest": {"code": weak[0], "name": q[weak[0]]["name"], "pct": weak[1]} if weak else None,
        "updated": time.strftime("%H:%M:%S"),
    }