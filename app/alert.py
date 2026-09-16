# -*- coding: utf-8 -*-
"""监控告警增强（v3.5）—— 借鉴 vnpy 微信/邮件通知、Server酱/企微/钉钉推送事实标准
分级告警：异动(INFO) / 风控(WARN) / 成交(BUY|SELL) / 日结(DAILY) 四级
- 去重冷却：同类事件 N 秒内只发一次（防刷屏）
- 日频限流：每渠道每日最多 N 条（防封）
- webhook 多渠道：Server酱（默认）/ 企业微信 / 钉钉（webhook URL 配置）
- 心跳看门狗：进程心跳记录，异常掉线可检测
"""
import threading
import time

from . import config as C
from . import state as st

_lock = threading.Lock()
_last_sent = {}       # (channel, key) -> ts
_daily_count = {}     # (channel, date) -> count


def _notify_http(url, title, message, extra_headers=None):
    import urllib.parse
    import urllib.request
    if "sctapi.ftqq.com" in url:
        data = urllib.parse.urlencode({"title": title, "desp": message}).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={
            "User-Agent": "Mozilla/5.0",
            "Content-Type": "application/x-application/x-www-form-urlencoded"})
    else:
        # 通用 webhook（企业微信/钉钉）：JSON body
        import json as _json
        data = _json.dumps({"msgtype": "text", "text": {"content": f"{title}\n{message}"}}).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={
            "User-Agent": "Mozilla/5.0",
            "Content-Type": "application/json",
            **(extra_headers or {})})
    with urllib.request.urlopen(req, timeout=10) as resp:
        resp.read()
    return True


def send(channel, title, message):
    """发送一条 webhook 通知。channel: serverchan / wecom / dingtalk"""
    cfg = st.load_notify()
    url = {"serverchan": f"https://sctapi.ftqq.com/{cfg.get('sendkey')}.send" if cfg.get("sendkey") else "",
           "wecom": cfg.get("wecom_url", ""),
           "dingtalk": cfg.get("dingtalk_url", "")}.get(channel, "")
    if not url:
        return False
    try:
        _notify_http(url, title, message)
        return True
    except Exception:
        return False


def notify(level, key, title, message, cooldown=60):
    """分级告警（带去重冷却 + 日频限流）。
    level: INFO/WARN/BUY/SELL/DAILY
    key: 去重键（同 key 冷却期内不重复推送）
    """
    cfg = st.load_notify()
    if not cfg.get("enabled"):
        return False
    now = time.time()
    today = time.strftime("%Y-%m-%d")
    with _lock:
        # 去重冷却
        last = _last_sent.get((level, key), 0)
        if now - last < cooldown:
            return False
        # 日频限流
        cnt = _daily_count.get((level, today), 0)
        if cnt >= C.ALERT_DAILY_LIMIT:
            return False
    # 主渠道推送（Server酱默认）
    ok = send("serverchan", title, message)
    if not ok:
        # 备选 webhook
        ok = send("wecom", title, message) or send("dingtalk", title, message)
    if ok:
        with _lock:
            _last_sent[(level, key)] = now
            _daily_count[(level, today)] = _daily_count.get((level, today), 0) + 1
    return ok


def heartbeat(scope="trading"):
    """进程心跳：写入审计日志，供看门狗检测"""
    from . import audit
    audit.record("alert", "heartbeat", level="INFO", scope=scope,
                 ts=time.strftime("%H:%M:%S"))


def daily_report(day=None):
    """每日收盘日结推送：当日交易+盈亏+持仓摘要"""
    from . import audit
    from . import state as st
    from . import datafeed as df
    day = day or time.strftime("%Y-%m-%d")
    summ = audit.daily_summary(day)
    acct = st.load_account()
    positions = acct.get("positions", {})
    pos_lines = []
    mv = 0.0
    codes = list(positions.keys())
    quotes = df.fetch_quotes(codes) if codes else {}
    for code, p in positions.items():
        q = quotes.get(code, {})
        cp = q.get("price", 0) or p["entry_price"]
        pnl = (cp - p["entry_price"]) * p["qty"]
        mv += cp * p["qty"]
        pos_lines.append(f"{p.get('name', code)}({code}) {cp:.2f}x{p['qty']} "
                         f"{'+' if pnl >= 0 else ''}{pnl:+,.0f}")
    total = acct["cash"] + mv
    lines = [
        f"📅 日结 {day}",
        f"交易: 买{summ['buys']} 卖{summ['sells']} | 风控拦截{summ['risk_blocks']} | 异动告警{summ['alerts']}",
        f"资产: ¥{total:,.0f}（现金¥{acct['cash']:,.0f} + 市值¥{mv:,.0f}）",
    ]
    if pos_lines:
        lines.append("持仓: " + " | ".join(pos_lines[:5]))
    else:
        lines.append("持仓: 空仓")
    notify("DAILY", f"daily-{day}", "📊 模拟盘日结", "\n".join(lines), cooldown=3600)
    return summ
