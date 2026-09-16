# -*- coding: utf-8 -*-
"""★D-S6 ELTDX 数据通道（2026-09-16，验收方接入 GitHub 源 eltdx 3.2.2）：
- 竞价撤单率（真口径）：A股集合竞价 09:15-09:25 未撮合挂单序列，
  撤单率 ≈ 1 − 竞价完成时剩余挂单 / 峰值挂单（峰值方向 = 主动撤单方向）。
- 该数据来自交易所原始字段（unmatched_signed_raw ±方向），非快照近似。
盘中高频调用自节制：每代码当日结果缓存 60s（竞价后数据不再变化）。"""
import threading
import time

_lock = threading.Lock()
_mem = {}          # (code, date) -> payload
_client = None


def available():
    try:
        import eltdx  # noqa
        return True
    except ImportError:
        return False


def _get_client():
    global _client
    if _client is not None:
        return _client
    with _lock:
        if _client is None:
            try:
                import eltdx
                _client = eltdx.Client(timeout=5.0, server_count=1)
                _client.connect()
            except Exception:
                _client = None
    return _client


def auction_cancel(code, date=None, force=False):
    """{cancel_ratio, peak_vol, peak_dir(+1=买撤 -1=卖撤), final_vol, open_price, date}
    失败返回 None（不抛）。竞价数据当日不变 → 日内缓存；force=True 绕缓存首查。"""
    if not available():
        return None
    code6 = str(code or "")[-6:]
    if not code6.isdigit():
        return None
    today = time.strftime("%Y%m%d")
    key = (code6, date or today)
    with _lock:
        hit = _mem.get(key)
    if hit and time.time() - hit[0] < 60 and not force:
        return hit[1]
    c = _get_client()
    if c is None:
        return None
    try:
        s = c.auctions.series(code6, date=date)
        pts = [p for p in s.points if p.time_label <= "09:25:00"] if s.points else []
        if not pts:
            payload = {"date": date or today, "no_data": True}
        else:
            peak = max(pts, key=lambda p: p.unmatched_volume)
            fin = pts[-1]
            ratio = (1 - fin.unmatched_volume / peak.unmatched_volume
                     if peak.unmatched_volume else 0.0)
            payload = {"date": date or today,
                       "peak_vol": peak.unmatched_volume,
                       "peak_dir": peak.unmatched_direction_raw,
                       "final_vol": fin.unmatched_volume,
                       "open_price": round(fin.price, 3),
                       "cancel_ratio": round(ratio, 3)}
        with _lock:
            _mem[key] = (time.time(), payload)
        return payload
    except Exception:
        return None


def close():
    global _client
    try:
        if _client:
            _client.close()
    except Exception:
        pass


def main_flow_1m(code, threshold=300000.0):
    """★D-S6b：逐笔级"最近1分钟大单净额"（eltdx 逐笔成交通道）。
    口径：单笔成交金额 ≥threshold(默认30万) 视为大单；buyer side=+，sell side=-。
    返回 {"m1_net": 净额, "m1_count": 大单笔数, "last_min": 'HH:MM'}；失败 None。
    竞价撤单率接口同 client、日内缓存 60s（盘中成交在变 → 活口 55s 节流由调用方控制）。
    """
    if not available():
        return None
    code6 = str(code or "")[-6:]
    if not code6.isdigit():
        return None
    c = _get_client()
    if c is None:
        return None
    try:
        b = c.trades.today(code6)
        ticks = list(b.ticks)
        if not ticks:
            return None
        # 时间标签分钟聚合：['09:30','09:31'...]
        def _mk(t):
            return t.time_label or ""
        mins = sorted({_mk(t) for t in ticks if _mk(t)})
        if len(mins) < 2:
            return None
        big = [{"m": _mk(t), "net": (t.volume * t.price
                  * (1 if (t.side or "") in ("buy", "b") else -1)
                  if t.price and t.volume else 0)}
               for t in ticks
               if _mk(t) and t.volume * t.price >= threshold]
        if not big:
            return {"m1_net": 0.0, "m1_count": 0, "last_min": mins[-2]}
        last_m = mins[-2]
        m1 = [x["net"] for x in big if x["m"] == last_m]
        return {"m1_net": round(sum(m1), 0), "m1_count": len(m1),
                "last_min": last_m}
    except Exception:
        return None
