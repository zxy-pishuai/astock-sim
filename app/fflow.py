# -*- coding: utf-8 -*-
"""资金流模块（4.0 实战体系）—— 东财 push2 fflow 免费接口（纯 urllib，已实测）
  1. 个股分钟/日资金流：主力/超大单/大单/中单/小单净流入
  2. 主力净流入趋势：近 N 日主力净额（建仓/出货判断）
注意：东财接口需串行限流 ≥1s（内置）。
"""
import json
import threading
import time
import urllib.request

from . import config as C

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
_REF = "https://quote.eastmoney.com/"
_lock = threading.Lock()
_mem = {}          # key -> (ts, value)
_last_req = 0.0    # 限流时间戳

# ★ 4.5：熔断器 —— push2his 被 WAF 封禁时快速失败（原实现 12s 超时×3重试×2.5s串行限流，
#   每只卡 ~40s → 扫描利差面池 10 只 ≈ 400s 灾难）
_breaker = {"fails": 0, "until": 0.0}
_BREAK_AFTER = 2
_BREAK_COOLDOWN = 600


def _get(url, timeout=5, retries=2):
    global _last_req
    now = time.time()
    if now < _breaker["until"]:
        raise RuntimeError("fflow circuit open")
    last_err = None
    for attempt in range(retries):
        with _lock:
            # 串行限流（东财 WAF）；★ 4.5 2.5s→0.8s：扫描利差面池十几次调用不再排队几十秒
            wait = 0.8 - (time.time() - _last_req)
            if wait > 0:
                time.sleep(wait)
            _last_req = time.time()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _UA, "Referer": _REF})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                text = r.read().decode("utf-8", errors="replace")
            _breaker["fails"] = 0
            return text
        except Exception as e:
            last_err = e
            _breaker["fails"] += 1
            if _breaker["fails"] >= _BREAK_AFTER:
                _breaker["until"] = now + _BREAK_COOLDOWN
            time.sleep(0.5 * (attempt + 1))
    raise last_err


def _secid(code):
    """code → secid（1=沪, 0=深）"""
    return ("1." if code.startswith(("6", "5", "9")) else "0.") + code


def _parse_klines(text):
    """解析 fflow klines 数组 → [{date, main, s1(超大), s2(大), m(中), s(小), ...}]"""
    try:
        d = json.loads(text)
        klines = ((d.get("data") or {}).get("klines") or [])
    except Exception:
        return []
    out = []
    for line in klines:
        p = line.split(",")
        if len(p) < 7:
            continue
        try:
            out.append({
                "date": p[0],
                "main": float(p[1]),      # 主力净流入
                "s1": float(p[2]),        # 超大单净
                "s2": float(p[3]),        # 大单净
                "m": float(p[4]),         # 中单净
                "s": float(p[5]),         # 小单净
                "pct": float(p[6]) if len(p) > 6 and p[6] else None,
            })
        except (ValueError, IndexError):
            continue
    return out


def daily_fflow(code, days=10, force=False):
    """个股近 N 日资金流（主力/超大/大/中/小单净流入，元）。
    返回 [{date, main, s1, s2, m, s, pct}] 最新在前。
    数据源：东财 push2his（可能被 WAF 临时封禁）；失败时降级为本地 K 线量价近似。
    """
    key = ("fflow_d", code)
    now = time.time()
    if not force:
        hit = _mem.get(key)
        if hit and now - hit[0] < 300:
            return hit[1]
    url = ("https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get?"
           f"lmt=0&klt=101&secid={_secid(code)}"
           "&fields1=f1,f2,f3,f7&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65")
    try:
        rows = _parse_klines(_get(url))
        if rows:
            rows.reverse()  # 最新在前
            _mem[key] = (now, rows[:days])
            return rows[:days]
    except Exception:
        pass
    # ★ 降级：本地 K 线量价近似（主力方向估算）
    rows = _local_approx(code, days)
    _mem[key] = (now, rows)
    return rows


def _local_approx(code, days=10):
    """本地量价资金近似：主力净额 ≈ 当日成交额 × (涨跌幅方向 × 量能修正)。
    参考常用量价资金流近似：主力净 ≈ amount × 0.3 × sign(pct) × min(1, |pct|/5)。
    返回 [{date, main, s1, s2, m, s, pct}]（main 为估算主力净额，其余为 0）。
    ★ 4.5：原实现循环内 kl.index(k) O(n²)，数据量大时卡顿，改为一次遍历。
    """
    from . import datafeed as df
    try:
        kl = df.fetch_kline(code, "day", max(days + 5, 30))
        if len(kl) < 3:
            return []
        out = []
        prev_close = None
        for k in kl[-days:]:
            pct = None
            main_est = 0.0
            amount = k.get("amount", 0) or (k["close"] * k.get("volume", 0))
            if prev_close and prev_close > 0:
                pct = (k["close"] - prev_close) / prev_close * 100
                factor = min(1.0, abs(pct) / 5.0) * 0.3
                main_est = amount * factor * (1 if pct >= 0 else -1)
            out.append({"date": k["date"], "main": main_est,
                        "s1": 0.0, "s2": 0.0, "m": 0.0, "s": 0.0, "pct": pct})
            prev_close = k["close"]
        out.reverse()  # 最新在前
        return out
    except Exception:
        return []


def minute_fflow(code, force=False):
    """个股当日分钟资金流（最新一条 = 当前累计）。返回最新 {date, main, ...}
    数据源 push2 可能被 WAF 封禁；失败返回 None（调用方忽略）。
    """
    key = ("fflow_m", code)
    now = time.time()
    if not force:
        hit = _mem.get(key)
        if hit and now - hit[0] < 60:
            return hit[1]
    url = ("https://push2.eastmoney.com/api/qt/stock/fflow/kline/get?"
           f"lmt=0&klt=1&secid={_secid(code)}"
           "&fields1=f1,f2,f3,f7&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65")
    try:
        rows = _parse_klines(_get(url))
        latest = rows[-1] if rows else None
        _mem[key] = (now, latest)
        return latest
    except Exception:
        return None


def main_inflow_trend(code, days=5, force=False):
    """主力净流入趋势判断：
    返回 (trend, score, desc)
      trend: 流入/流出/震荡
      score: -3 ~ +3（连续流入为正）
    """
    rows = daily_fflow(code, days, force)
    if len(rows) < 2:
        return "未知", 0, "数据不足"
    mains = [r["main"] for r in rows[:days]]
    pos = sum(1 for m in mains if m > 0)
    total = sum(mains)
    if pos >= days - 1 and total > 0:
        return "连续流入", min(3, pos), "主力连续%d日净流入" % pos
    if pos == 0 and total < 0:
        return "连续流出", -3, "主力连续%d日净流出" % days
    if total > 0:
        return "流入", 1, "近%d日主力净流入 %.0f万" % (days, total / 1e4)
    return "流出", -1, "近%d日主力净流出 %.0f万" % (days, total / 1e4)
