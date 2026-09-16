# -*- coding: utf-8 -*-
"""★ Phase39：全球市场→A股映射规则的历史验证

对 app/global_market.py ASHARE_MAP_RULES 的 4 条可回溯规则做历史验证
（汇率规则依赖 USDCNH 可靠免费历史源，缺 → 跳过并在报告注明）：
  半导体链：纳指 ±1.5% 且 英伟达 ±3%
  港股科技：恒生科技 ±2%
  油气：WTI ±3%
  全球避险：VIX > 25（无方向提示，单独口径）
数据源（Yahoo 已对本机 403，实测改用）：腾讯 ifzq 分页（纳指/恒科）、
新浪美股日线（NVDA）、CBOE 官方 JSON（VIX）、东财 CL00Y 或新浪 USO（WTI）。

时序对齐（PIT）：外盘 T 日收盘全部发生在 A 股下一交易日开盘之前
（美股/原油/VIX 收于北京时间次日凌晨，港股 16:00 收），
统一取 t = 外盘日期 d 之后的首个 A 股交易日，只用 t 开盘前已知信息。

收益口径（规则统计与无条件基线同口径）：
  R1 = close_t / open_t - 1        （t 开盘入场、当日收盘）
  R5 = close_{t+4} / open_t - 1    （t 开盘入场持有 5 个交易日）
方向命中：利多触发=R>0 占比；利空触发=R<0 占比。
无条件基线 = 全部 A 股交易日同口径均值。

判定（事先写死）：n>=30 且 R5 命中率>=55% 且方向超额>=1pp → 保留建议；
方向一致但阈值不敏感（±50% 扰动下命中仍同向）→ 调阈值建议；否则下线建议；
全部不达标 → 如实写"无历史预测力，维持纯展示"。

代理组合（等权、逐日在场成员）：
  半导体链 → 板块 {半导体芯片, 半导体设备}
  港股科技 → 板块 {消费电子, AI算力, 软件信创}（A股科技成长最贴近恒科业务属性；
             恒科成分多为港上市中概，无直接 A 股影子，口径局限见报告）
  油气     → 名称含 石油/石化/油服/油田/海油/钻 的成员（classify_sector 真实板块优先）
所有外盘请求带 UA（东财另需 Referer），间隔 >=1 秒。只新增文件+读库。

输出: data/global_map_verify.json + 终端摘要
"""
import json
import os
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from app import config as C            # 只读：路径
from app import scoring                # 只读：classify_sector

OUT_JSON = os.path.join(BASE, "data", "global_map_verify.json")
STOCK_LIST_FILE = os.path.join(C.DATA_DIR, "stock_list.json")
START = "2019-01-01"
FULL_RATIO = 0.90      # 成员须覆盖 2019-07-01 后 >=90% 经验交易日（"库内有完整K线"）
SAMPLE_N = 12          # 报告逐次触发明细抽样条数

PROXIES = {
    "半导体链": {"sectors": ["半导体芯片", "半导体设备", "存储芯片", "先进封装"], "kw": None},
    "港股科技": {"sectors": ["消费电子", "AI算力", "软件信创"], "kw": None},
    "油气": {"sectors": [], "kw": ["石油", "石化", "油服", "油田", "海油", "钻探"]},
}

# 外盘品种键：pct 序列相对前一交易日收盘 %
# 数据源映射见 fetch 区注释（Yahoo 已不可用）
RULE_SERIES = {
    "半导体链": ["usIXIC", "usNVDA"],
    "港股科技": ["hkHSTECH"],
    "油气": ["CL"],
}


# Yahoo v8 chart 已对本机 403（cookie/crumb 均无效，实测），改用多源替代，
# 全部为只读 HTTP、带 UA/Referer、请求间隔 >=1 秒：
#   ^IXIC   → 腾讯 ifzq us.IXIC（640 根/页向前分页）
#   NVDA    → 新浪美股日线 US_MinKService（全史；自动检测并修正拆股跳变）
#   ^HSTECH → 腾讯 ifzq hkHSTECH（同上分页）
#   VIX     → CBOE 官方历史 JSON（cdn.cboe.com，_VIX.json）
#   WTI     → 首选东财 102.CL00Y（限流严重需重试轮换）；不可得时降级新浪 USO
#             （WTI 期货 ETF，日收益与 WTI 高相关但含展期损耗，口径差异见报告）


def _http(url, referer=None, timeout=20):
    h = {"User-Agent": "Mozilla/5.0"}
    if referer:
        h["Referer"] = referer
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _tx_kline(sym, end="2026-08-24", stop="2019-01-01"):
    """腾讯 ifzq 日线向前分页。返回 [(date, close)] 升序"""
    out = {}
    cur_end = end
    for _ in range(6):
        url = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
               "param=%s,day,2019-01-01,%s,640,qfq" % (sym, cur_end))
        js = json.loads(_http(url, referer="https://gu.qq.com/"))
        node = ((js.get("data") or {}).get(sym)) or {}
        arr = node.get("qfqday") or node.get("day") or []
        if not arr:
            break
        for r in arr:
            d = r[0]
            if d >= STOP_GUARD_LO:
                out[d] = float(r[2])          # 收盘（qfq）
        first = arr[0][0]
        if first <= stop or len(arr) < 2:
            break
        # 向前翻页：end=首日的前一天
        dt = datetime.strptime(first, "%Y-%m-%d") - timedelta(days=1)
        cur_end = dt.strftime("%Y-%m-%d")
        time.sleep(1.2)
    return sorted(out.items())


STOP_GUARD_LO = "2018-11-01"


def _sina_us_daily(sym):
    """新浪美股日线全史。返回 [(date, close)] 升序"""
    url = ("https://stock.finance.sina.com.cn/usstock/api/jsonp.php/"
           "var%20_=/US_MinKService.getDailyK?symbol=" + sym)
    text = _http(url, referer="https://finance.sina.com.cn")
    start = text.find("(")
    arr = json.loads(text[start + 1: text.rfind(")")])
    rows = sorted(((r["d"], float(r["c"])) for r in arr if isinstance(r, dict)))
    return rows


def _fix_splits(rows, max_jump=-0.5):
    """检测拆股跳变（收盘单日跌幅超 max_jump 且次日开盘比例近整数倍）并修正前段价格。
    简化实现：对相邻收盘比 < max_jump 的位置，把此前所有收盘乘以 round(prev/next) 的倒数。
    返回 (rows_fixed, fixes:list[(date, ratio)])"""
    fixes = []
    out = [list(r) for r in rows]
    for i in range(1, len(out)):
        pc, c = out[i - 1][1], out[i][1]
        if pc > 0 and c / pc - 1 < max_jump:
            ratio = pc / c
            k = max(2, int(round(ratio)))
            if abs(ratio - k) / k < 0.15:      # 近整数倍才认定拆股
                f = 1.0 / k
                for j in range(i):
                    out[j][1] *= f
                fixes.append((out[i][0], k))
    return [(d, c) for d, c in out], fixes


def fetch_cboe_vix():
    b = _http("https://cdn.cboe.com/api/global/delayed_quotes/charts/historical/_VIX.json",
              referer="https://www.cboe.com/")
    js = json.loads(b)
    data = js["data"]
    out = []
    for item in (data if isinstance(data, list) else []):
        if isinstance(item, dict) and "close" in item:
            d = str(item.get("date", ""))[:10]
            v = item.get("close")
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            d, v = str(item[0])[:10], item[1]
        else:
            continue
        try:
            out.append((datetime.strptime(d, "%Y-%m-%d").strftime("%Y-%m-%d"), float(v)))
        except (ValueError, TypeError):
            continue
    return sorted(out)


EM_HOSTS = ["https://push2his.eastmoney.com", "https://45.push2his.eastmoney.com",
            "https://21.push2his.eastmoney.com"]


def fetch_em_close(secid):
    """东财日线收盘（限流严重：主机轮换+退避重试）。返回 [(date, close)]"""
    path = ("/api/qt/stock/kline/get?secid=" + secid +
            "&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f53&klt=101&fqt=0"
            "&beg=20190101&end=20500101")
    last = None
    for a in range(6):
        host = EM_HOSTS[a % len(EM_HOSTS)]
        try:
            js = json.loads(_http(host + path, referer="https://quote.eastmoney.com/"))
            d = js.get("data") or {}
            kl = d.get("klines") or []
            if kl:
                return d.get("name"), sorted(
                    (s.split(",")[0], float(s.split(",")[1])) for s in kl)
            last = RuntimeError("empty klines")
        except Exception as e:
            last = e
        time.sleep(1.5 + a * 2)
    raise RuntimeError("eastmoney %s 失败: %s" % (secid, last))


def load_proxy_members():
    """stock_list.json + classify_sector 构建三组代理成员（限库内有K线且覆盖完整）"""
    conn = sqlite3.connect("file:%s?mode=ro" % C.DB_FILE, uri=True, timeout=15)
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT code FROM kline WHERE period='day'")
    in_db = set(r[0] for r in cur)
    cur.execute("SELECT MIN(date), MAX(date) FROM kline WHERE period='day'")
    lo, hi = cur.fetchone()
    cur.execute("SELECT COUNT(DISTINCT date) FROM kline WHERE period='day' AND date>=?",
                ("2019-07-01",))
    total_days = cur.fetchone()[0]
    cur.execute("SELECT code, COUNT(*) FROM kline WHERE period='day' AND date>=? "
                "GROUP BY code", ("2019-07-01",))
    cnt = {r[0]: r[1] for r in cur}
    conn.close()

    with open(STOCK_LIST_FILE, encoding="utf-8") as f:
        lst = json.load(f)
    groups = {k: [] for k in PROXIES}
    for row in lst:
        if not isinstance(row, (list, tuple)) or not row:
            continue
        code = str(row[0])
        name = str(row[1]) if len(row) > 1 else ""
        if code not in in_db:
            continue
        if cnt.get(code, 0) < total_days * FULL_RATIO:
            continue   # 库内K线不完整 → 不入代理
        # ★ 口径说明：classify_sector(name) 走名称关键词板块（半导体芯片/消费电子等）。
        #   带真实板块映射的 classify_sector(name, code) 返回的是行业名（如"电子器件"），
        #   粒度区分不出半导体/油气，故本验证统一用名称口径，报告注明。
        sec = scoring.classify_sector(name)
        for gname, cfg in PROXIES.items():
            if cfg["sectors"] and sec in cfg["sectors"]:
                groups[gname].append((code, name, sec))
                break
            if cfg["kw"] and any(k in name for k in cfg["kw"]):
                groups[gname].append((code, name, "名称匹配:" +
                                      next(k for k in cfg["kw"] if k in name)))
                break
    return groups, {"kline_codes": len(in_db), "range": [lo, hi],
                    "days_since_201907": total_days,
                    "full_ratio": FULL_RATIO}


def load_ashare_series(codes):
    """读成员日K → {code: [(date, open, close)]}（升序）与日期全集"""
    conn = sqlite3.connect("file:%s?mode=ro" % C.DB_FILE, uri=True, timeout=30)
    cur = conn.cursor()
    ph = ",".join("?" for _ in codes)
    cur.execute("SELECT code, date, open, close FROM kline WHERE period='day' "
                "AND date>=? AND code IN (%s) ORDER BY code, date" % ph,
                [START] + list(codes))
    data = {}
    for code, date, o, c in cur:
        if o and c and o > 0:
            data.setdefault(code, []).append((date, o, c))
    conn.close()
    all_dates = sorted({d for rows in data.values() for d, _, _ in rows})
    idx = {d: i for i, d in enumerate(all_dates)}
    return all_dates, idx, data


def build_portfolio(all_dates, idx, data):
    """预建每票全局索引映射后计算组合 R1/R5（等权、逐日在场成员）"""
    pos = {}
    for code, arr in data.items():
        m = {}
        for gi, o, c in ((idx[d], o, c) for d, o, c in arr):
            m[gi] = (o, c)
        pos[code] = m
    r1, r5 = {}, {}
    for t, d in enumerate(all_dates):
        v1, v5 = [], []
        for code, m in pos.items():
            oc = m.get(t)
            if not oc:
                continue
            o, c = oc
            v1.append(c / o - 1.0)
            f = m.get(t + 4)
            if f:
                v5.append(f[1] / o - 1.0)
        r1[d] = sum(v1) / len(v1) if v1 else None
        r5[d] = sum(v5) / len(v5) if v5 else None
    return r1, r5


def pct_series(bars):
    """[(date,close)] → {date: pct%}（相对前一交易日收盘）"""
    out = {}
    prev = None
    for d, c in bars:
        if prev and prev > 0:
            out[d] = (c / prev - 1.0) * 100.0
        prev = c
    return out


def next_ashare_day(d, ashare_dates_set, ashare_dates_sorted):
    import bisect
    i = bisect.bisect_right(ashare_dates_sorted, d)
    if i < len(ashare_dates_sorted):
        return ashare_dates_sorted[i]
    return None


# ============ 规则定义（阈值与 ASHARE_MAP_RULES 一致） ============
RULES = [
    {"name": "半导体链", "series": ["usIXIC", "usNVDA"], "logic": "and",
     "thr": {"usIXIC": 1.5, "usNVDA": 3.0},
     "proxy": "半导体链"},
    {"name": "港股科技", "series": ["hkHSTECH"], "logic": "or",
     "thr": {"hkHSTECH": 2.0}, "proxy": "港股科技"},
    {"name": "油气", "series": ["CL"], "logic": "or",
     "thr": {"CL": 3.0}, "proxy": "油气"},
]
VIX_RULE = {"name": "全球避险(VIX>25)", "thr": 25.0}


def collect_triggers(rule, pcts, ashare_sorted, ashare_set, mult=1.0):
    """返回 (bull_list, bear_list)：元素为映射到的 A 股交易日 d（去重）。
    mult: 阈值扰动系数（0.5/1.5 敏感性检验用）"""
    bulls, bears = [], []
    s0 = rule["series"][0]
    s1 = rule["series"][1] if len(rule["series"]) > 1 else None
    dates = sorted(set(pcts[s0]) & set(pcts[s1])) if s1 else sorted(pcts[s0])
    seen_b, seen_r = set(), set()
    for d in dates:
        t = next_ashare_day(d, ashare_set, ashare_sorted)
        if t is None:
            continue
        v0 = pcts[s0][d]
        th0 = rule["thr"][s0] * mult
        if s1:
            v1 = pcts[s1][d]
            th1 = rule["thr"][s1] * mult
            ok_up = v0 >= th0 and v1 >= th1
            ok_dn = v0 <= -th0 and v1 <= -th1
        else:
            ok_up = v0 >= th0
            ok_dn = v0 <= -th0
        if ok_up and t not in seen_b:
            seen_b.add(t)
            bulls.append(t)
        elif ok_dn and t not in seen_r:
            seen_r.add(t)
            bears.append(t)
    return bulls, bears


def stat_block(triggers, r1, r5, base_r1, base_r5, direction):
    """direction: +1 利多 / -1 利空。返回均值/命中/超额"""
    rows = []
    for t in triggers:
        a, b = r1.get(t), r5.get(t)
        if a is None or b is None:
            continue
        rows.append((t, a, b))
    n = len(rows)
    if n == 0:
        return {"count": 0}
    m1 = sum(x[1] for x in rows) / n
    m5 = sum(x[2] for x in rows) / n
    hit1 = sum(1 for x in rows if x[1] * direction > 0) / n
    hit5 = sum(1 for x in rows if x[2] * direction > 0) / n
    ex1 = (m1 - base_r1) * direction
    ex5 = (m5 - base_r5) * direction
    return {"count": n, "mean_r1": round(m1, 5), "hit_r1": round(hit1, 4),
            "mean_r5": round(m5, 5), "hit_r5": round(hit5, 4),
            "excess_r5_pp": round(ex5 * 100, 3),
            "baseline_r5_pp": round(base_r5 * 100, 3),
            "samples": [{"t": t, "r1": round(a, 5), "r5": round(b, 5)}
                        for t, a, b in rows[:SAMPLE_N]]}


def main():
    t0 = time.time()
    groups, meta = load_proxy_members()
    for g, mem in groups.items():
        print("代理[%s]: %d 只成员" % (g, len(mem)), flush=True)

    all_members = sorted({c for mem in groups.values() for c, _, _ in mem})
    all_dates, idx, data = load_ashare_series(all_members)
    r1, r5 = build_portfolio(all_dates, idx, data)
    valid = [d for d in all_dates if r1.get(d) is not None and r5.get(d) is not None]
    base_r1 = sum(r1[d] for d in valid) / len(valid)
    base_r5 = sum(r5[d] for d in valid) / len(valid)
    ashare_set = set(all_dates)
    print("A股组合序列: %d 天（%s~%s）；无条件基线 R1=%.3f%% R5=%.3f%%" % (
        len(valid), valid[0], valid[-1], base_r1 * 100, base_r5 * 100), flush=True)

    bars_map = {}
    print("拉取外盘序列（多源）...", flush=True)

    def put(key, bars):
        bars = [(d, c) for d, c in bars if d >= STOP_GUARD_LO]
        if not bars:
            raise RuntimeError("%s 空序列" % key)
        bars_map[key] = {"bars": bars, "pct": pct_series(bars)}
        print("  %-9s %d 根（%s~%s）" % (key, len(bars), bars[0][0], bars[-1][0]),
              flush=True)

    put("usIXIC", _tx_kline("us.IXIC"))
    time.sleep(1.2)
    nv_raw = _sina_us_daily("NVDA")
    nv, fixes = _fix_splits([r for r in nv_raw])
    if fixes:
        print("  NVDA 拆股修正:", fixes, flush=True)
    put("usNVDA", nv)
    time.sleep(1.2)
    put("hkHSTECH", _tx_kline("hkHSTECH"))
    time.sleep(1.2)
    vix_bars = fetch_cboe_vix()
    put("usVIX_close", vix_bars)          # 绝对价位（VIX>25 规则）
    time.sleep(1.2)
    # WTI：首选东财 CL00Y；不可得时降级新浪 USO（口径差异见报告）
    wti_src, wti_bars = None, None
    try:
        name, wti_bars = fetch_em_close("102.CL00Y")
        wti_src = "eastmoney %s(%s)" % (name, "102.CL00Y")
    except Exception as e:
        print("  东财 WTI 不可得(%s)，降级新浪 USO" % str(e)[:40], flush=True)
        uso = [r for r in _sina_us_daily("USO")]
        wti_bars, wti_src = uso, "sina USO(WTI期货ETF代理)"
    put("CL", wti_bars)
    wti_note = wti_src
    pcts = {k: v["pct"] for k, v in bars_map.items() if k != "usVIX_close"}

    out_rules = []

    def verdict_for(blk, direction, perturb):
        """事先写死的判定：保留/调阈值/下线"""
        n = blk.get("count", 0)
        if n < 30:
            return "样本不足(%d)，不下结论" % n
        hit5, ex5 = blk["hit_r5"], blk["excess_r5_pp"]
        if hit5 >= 0.55 and ex5 >= 1.0:
            return "建议保留"
        if hit5 >= 0.50 and ex5 > 0:
            ok = [p for p in perturb
                  if p.get("hit_r5") is not None and p["hit_r5"] >= 0.52
                  and p.get("excess_r5_pp", 0) > 0]
            if len(ok) == len([p for p in perturb if p.get("count", 0) >= 30]) \
                    and any(p.get("count", 0) >= 30 for p in perturb):
                return "建议调阈值（方向稳健但当前阈值不敏感）"
            return "建议下线（阈值扰动下不稳健）"
        return "建议下线（无方向信息）"

    for rule in RULES:
        entry = {"rule": rule["name"],
                 "thresholds": rule["thr"],
                 "proxy": rule["proxy"],
                 "members": len(groups.get(rule["proxy"], []))}
        bulls, bears = collect_triggers(rule, pcts, all_dates, ashare_set)
        for tag, lst, direction in [("bull", bulls, 1), ("bear", bears, -1)]:
            blk = stat_block(lst, r1, r5, base_r1, base_r5, direction)
            blk["direction"] = "利多" if direction > 0 else "利空"
            # 阈值 ±50% 扰动（敏感性）
            perturb = []
            for mult in (0.5, 1.5):
                pl, pr = collect_triggers(rule, pcts, all_dates, ashare_set, mult=mult)
                pb = stat_block(pl if direction > 0 else pr,
                                r1, r5, base_r1, base_r5, direction)
                perturb.append({"mult": mult, "count": pb.get("count", 0),
                                "hit_r5": pb.get("hit_r5"),
                                "excess_r5_pp": pb.get("excess_r5_pp")})
            blk["perturb"] = perturb
            blk["verdict"] = verdict_for(blk, direction, perturb)
            entry[tag] = blk
        out_rules.append(entry)
        print("%-6s 利多 n=%-4d[%s] 利空 n=%-4d[%s]" % (
            rule["name"], entry["bull"]["count"], entry["bull"]["verdict"][:8],
            entry["bear"]["count"], entry["bear"]["verdict"][:8]), flush=True)

    # VIX>25（无方向提示）：统计触发后 R1/R5 分布对照基线（绝对价位，非 pct）
    vix_close = {d: c for d, c in bars_map["usVIX_close"]["bars"]}
    vt = []
    seen_vt = set()
    for d in sorted(vix_close):
        if vix_close[d] > VIX_RULE["thr"]:
            t = next_ashare_day(d, ashare_set, all_dates)
            if t and t not in seen_vt and r1.get(t) is not None and r5.get(t) is not None:
                seen_vt.add(t)
                vt.append((t, r1[t], r5[t]))
    vix_blk = {"count": len(vt),
               "note": "VIX>25 为无方向避险提示，不设方向命中率；对照无条件基线看均值偏移"}
    if vt:
        m1 = sum(x[1] for x in vt) / len(vt)
        m5 = sum(x[2] for x in vt) / len(vt)
        ex5 = (m5 - base_r5) * 100
        vix_blk.update({
            "mean_r1": round(m1, 5), "mean_r5": round(m5, 5),
            "down_ratio_r1": round(sum(1 for x in vt if x[1] < 0) / len(vt), 4),
            "excess_r5_pp": round(ex5, 3),
            "verdict": ("建议保留（触发后显著走低）" if len(vt) >= 30 and ex5 <= -1.0
                        else "维持纯展示（触发后相对基线无避险性偏移）"),
            "samples": [{"t": t, "r1": round(a, 5), "r5": round(b, 5)}
                        for t, a, b in vt[:SAMPLE_N]],
        })
    else:
        vix_blk["verdict"] = "样本不足"
    out_rules.append({"rule": VIX_RULE["name"], "trigger": vix_blk})

    doc = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "phase": "39",
        "method": "外盘T收盘→A股T+1交易日（PIT）；R1=close/open-1, R5=close_{t+4}/open_t-1;"
                  "代理等权逐日在场成员；无条件基线=全样本同口径",
        "usdcnh_note": "汇率规则跳过：USDCNH 无可靠免费历史源（Phase35 实测新浪/东财仅有实时或替代序列）",
        "sources_note": "Yahoo v8 chart 已对本机 403（cookie/crumb 无效）。替代源："
                        "纳指/恒科=腾讯 ifzq 日线分页；NVDA/USO=新浪美股日线；"
                        "VIX=CBOE 官方历史 JSON；WTI=东财 CL00Y，不可得时降级新浪 USO 代理",
        "wti_source": wti_note,
        "proxies": {g: {"count": len(mem),
                        "sample": [(c, n) for c, n, _ in mem[:15]]}
                    for g, mem in groups.items()},
        "meta": meta,
        "baseline": {"r1_pp": round(base_r1 * 100, 3),
                     "r5_pp": round(base_r5 * 100, 3), "days": len(valid)},
        "rules": out_rules,
        "elapsed_sec": round(time.time() - t0, 1),
    }
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    print("\n已写出 %s（耗时 %.0fs）" % (OUT_JSON, time.time() - t0), flush=True)


if __name__ == "__main__":
    main()
