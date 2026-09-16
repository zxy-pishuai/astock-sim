# -*- coding: utf-8 -*-
"""★ 4.6 板块资金流向（可视化数据源，多源容错）
供"板块资金流"可视化页（双侧连线图）：左侧=主力净流出板块，右侧=净流入板块，连线表示资金流。
- 主源：新浪 MoneyFlow（★ 4.7：实时无延时，~110ms；仅主力净额）
- 备用源：东方财富 push2 系（push2delay 延时主机，字段最全：主力/超大/大/中/小单；新浪异常时自动切换）
- 零新第三方依赖（urllib），复用 moneyflow._http_json（东财熔断/经济模式）
- 开关 SECTOR_FLOW_ENABLED 默认关闭；ttl 缓存；多源自动降级

返回结构：
  {fetched_at, total, left:[净流出], right:[净流入], links:[{source,target,value,out}], source, degraded?}
"""
import json
import threading
import time
import urllib.parse
import urllib.request

from . import config as C
from . import moneyflow as mf  # 复用 _http_json（熔断/经济模式/UA/Referer）

# ---------- 东财（★ 4.7 多主机容错：push2/92.push2 被 IP 风控断连，
#   push2delay 为官方延时行情主机，实测未被封且资金流字段完整，作为首选） ----------
_EA_HOSTS = ["push2delay.eastmoney.com", "push2.eastmoney.com", "92.push2.eastmoney.com"]
_EA_STICKY = {"host": None}   # 记住最近一次成功的主机，优先使用
_EA_UT = "b2884a393a59ad64002292a3e90d46a5"
_FIELDS = "f12,f14,f3,f62,f184,f66,f72,f78,f84"

# ---------- 新浪板块资金流 ----------
_SINA_TMPL = ("http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
              "MoneyFlow.ssl_bkzj_bk?page={p}&num={n}&sort=netamount&asc={a}&fenlei={f}")
# fenlei: 0=行业, 1=概念；asc: 0=净流入降序 1=净流出升序（★ 4.7 双向拉取，防单侧为空）

_cache = {}   # (type, top) -> (ts, result)
_lock = threading.Lock()
_TTL = 5
_FALLBACK = {}  # type -> 上次成功结果

_UA = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 "
                   "Safari/537.36"),
    "Referer": "https://finance.sina.com.cn/",
    "Accept": "text/javascript, application/javascript, */*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Connection": "keep-alive",
}


def _f(v):
    """安全转 float：可能返回 '-'/空/None"""
    try:
        if v in (None, "", "-"):
            return 0.0
        return float(v)
    except Exception:
        return 0.0


def _build(rows, top, total):
    """把板块行数据组织成可视化结构（净流出 top 左 / 净流入 top 右 + 连线）"""
    rows.sort(key=lambda r: r["net"], reverse=True)
    right = [r for r in rows if r["net"] > 0][:top]
    left = [r for r in reversed(rows) if r["net"] < 0][:top]
    links = []
    n = min(len(left), len(right))
    for i in range(n):
        links.append({
            "source": i,
            "target": i,
            "value": right[i]["net"] if right[i]["net"] else 0,
            "out": left[i]["net"] if left[i]["net"] else 0,
        })
    return {
        "fetched_at": time.strftime("%H:%M:%S"),
        "total": total or len(rows),
        "left": left, "right": right, "links": links,
    }


def _fetch_eastmoney(type_, top):
    """东财板块资金流（★ 4.7 多主机容错 + 双向拉取）。
    净流入降序 + 净流出升序各拉一页合并（单侧排行会导致另一侧为空）。
    返回 (rows, total, host)；失败抛异常。"""
    fs = "m:90+t:2" if type_ == "industry" else "m:90+t:3"
    pz = max(20, top * 3)
    hosts = list(_EA_HOSTS)
    if _EA_STICKY["host"] in hosts:
        hosts.remove(_EA_STICKY["host"])
        hosts.insert(0, _EA_STICKY["host"])
    last_err = None
    for host in hosts:
        try:
            rows_by, total = {}, 0
            for po in ("1", "0"):   # 1=净流入降序 / 0=净流出升序
                params = {
                    "pn": "1", "pz": str(pz), "po": po, "np": "1", "ut": _EA_UT,
                    "fltt": "2", "invt": "2", "fid": "f62", "fs": fs,
                    "fields": _FIELDS, "_": str(int(time.time() * 1000)),
                }
                url = "https://" + host + "/api/qt/clist/get?" + urllib.parse.urlencode(params)
                d = mf._http_json(url)
                data = d.get("data") or {}
                total = data.get("total", 0) or total
                for it in (data.get("diff") or []):
                    try:
                        row = {
                            "code": str(it.get("f12", "")),
                            "name": str(it.get("f14", "")),
                            "pct": _f(it.get("f3")),
                            "net": _f(it.get("f62")),
                            "net_pct": _f(it.get("f184")),
                            "super_net": _f(it.get("f66")),
                            "big_net": _f(it.get("f72")),
                            "mid_net": _f(it.get("f78")),
                            "small_net": _f(it.get("f84")),
                        }
                        rows_by[row["code"] or row["name"]] = row
                    except Exception:
                        continue
            rows = list(rows_by.values())
            if len(rows) < 2:
                raise RuntimeError("东财板块资金流数据不足")
            _EA_STICKY["host"] = host
            return rows, total, host
        except Exception as e:
            last_err = e
            continue
    raise last_err or RuntimeError("东财全部主机不可用")


def _fetch_sina(type_, top):
    """新浪板块资金流（★ 4.7 双向拉取）。返回 (rows, total)；失败抛异常。
    字段：category/name/netamount 净流入(元)/ratioamount/r0_net 主力净额。
    fenlei: 0=行业 1=概念。含轻量重试（456 频限退避）。"""
    fenlei = "0" if type_ == "industry" else "1"
    n = max(40, top * 4)
    rows_by = {}
    last_err = None
    for asc in ("0", "1"):   # 0=净流入降序 / 1=净流出升序
        url = _SINA_TMPL.format(p=1, n=n, f=fenlei, a=asc)
        raw = None
        for attempt in range(3):
            try:
                req = urllib.request.Request(url, headers=_UA)
                with urllib.request.urlopen(req, timeout=8) as r:
                    raw = r.read().decode("gbk", errors="replace")
                if raw and len(raw) > 10:
                    break
                raise RuntimeError("新浪返回为空")
            except Exception as e2:
                last_err = e2
                if attempt < 2:
                    time.sleep(1.0 * (attempt + 1))
        if not raw or len(raw) <= 10:
            continue
        try:
            arr = json.loads(raw) if raw.startswith("[") else json.loads("[" + raw + "]")
        except Exception as e3:
            last_err = e3
            continue
        for it in arr:
            try:
                key = str(it.get("category", "")) or str(it.get("name", ""))
                rows_by[key] = {
                    "code": str(it.get("category", "")),
                    "name": str(it.get("name", "")),
                    "pct": _f(it.get("avg_changeratio")) * 100,
                    "net": _f(it.get("netamount")),
                    "net_pct": _f(it.get("ratioamount")),
                    "super_net": _f(it.get("r0_net")),
                    "big_net": 0.0, "mid_net": 0.0, "small_net": 0.0,
                }
            except Exception:
                continue
    rows = list(rows_by.values())
    if len(rows) < 2:
        raise last_err or RuntimeError("新浪板块资金流数据不足")
    return rows, len(rows)


def sector_flow(type_="industry", top=6, force=False):
    """对外入口：多源容错（东财主源 → 失败切新浪），带缓存降级。"""
    type_ = type_ if type_ in ("industry", "concept") else "industry"
    top = max(3, min(30, int(top or 6)))
    now = time.time()
    with _lock:
        hit = _cache.get((type_, top))
        if hit and not force and now - hit[0] < _TTL:
            return hit[1]
        if not C.SECTOR_FLOW_ENABLED:
            return _FALLBACK.get(type_) or {
                "fetched_at": "", "total": 0, "left": [], "right": [],
                "links": [], "disabled": True,
            }
    # ★ 4.7 主源新浪（实时无延时） → 备用东财（push2delay 延时主机，字段全）
    res = None
    err = None
    try:
        rows, total = _fetch_sina(type_, top)
        res = _build(rows, top, total or len(rows))
        res["source"] = "sina"
    except Exception as e:
        err = e
        try:
            rows, total, ea_host = _fetch_eastmoney(type_, top)
            res = _build(rows, top, total or len(rows))
            res["source"] = "eastmoney(delay)" if "delay" in ea_host else "eastmoney"
            res["degraded"] = f"新浪不可用已切东财({ea_host})，数据有延时: {err}"
        except Exception as e2:
            err = f"{err}; 东财备用也失败: {e2}"
    if res:
        with _lock:
            _cache[(type_, top)] = (now, res)
            _FALLBACK[type_] = res
        return res
    # 全部失败：降级到上次缓存/空
    prev = _FALLBACK.get(type_)
    if prev is None:
        prev = {"fetched_at": "", "total": 0, "left": [], "right": [],
                "links": [], "error": str(err)}
    else:
        prev = dict(prev)
        prev["degraded"] = str(err)[:80]
        prev["fetch_err"] = True
    return prev
