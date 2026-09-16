# -*- coding: utf-8 -*-
"""★ 通达信协议加速层（可选，基于 mootdx）
当 mootdx 可用时：批量行情/分钟K 比腾讯 HTTP 快 10~20 倍（实测 100 只 55ms），
且不走 HTTP 绕过东财 IP 风控。未安装/连接失败时自动回退腾讯/新浪（不影响主流程）。

设计：
  1. 单例长连接（重连 3.8s 太慢，连接保持复用）
  2. 字段映射：通达信 raw → 与 datafeed.fetch_quotes 相同的输出结构
  3. 失败自动降级：任何异常 → 返回 None/[]，调用方走原腾讯路径
  4. 零侵入：datafeed 里 try-import，未安装则静默跳过
"""
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

_log = logging.getLogger("tdx")   # ★ Z6-P1：整体超时计数用既有 logging（不新建日志文件）
_TDX_TIMEOUT_COUNT = 0            # ★ Z6-P1：TDX 批量行情整体超时次数（进程内计数）

_tls = threading.local()   # ★ 线程本地连接：单 TCP 连接不可并发，多线程各持一条长连接
_TTL = 60 * 30             # 连接 30 分钟保活

# ★ 4.5：常驻线程池 —— 每次调用新建线程池会让线程退出时泄漏 TDX socket，
#   连接数累积触发服务器限流（实测 2 次后 2.8s → 30s）。常驻线程连接稳定复用。
#   ★ 实测 8 线程反而慢（服务器并发连接阈值：8 连接 4.3~5.7s vs 6 连接 1.7~2s），保持 6
_POOL_SIZE = 6
_pool = ThreadPoolExecutor(max_workers=_POOL_SIZE, thread_name_prefix="tdx")


def _close_client(c):
    """★ P2-1：安全关闭旧连接（mootdx/pytdx client 有 close()），防 30min×线程累积泄漏"""
    try:
        close = getattr(c, "close", None)
        if callable(close):
            close()
    except Exception:
        pass


def _get_client():
    """获取（或重建）本线程的通达信连接。返回 client 或 None"""
    c = getattr(_tls, "client", None)
    now = time.time()
    if c is not None and now - getattr(_tls, "ts", 0.0) < _TTL:
        return c
    if c is not None:                    # ★ P2-1：TTL 过期重建前关闭旧连接
        _close_client(c)
        _tls.client = None
    try:
        from mootdx.quotes import Quotes
        c = Quotes.factory(market="std")
        _tls.client = c
        _tls.ts = now
        _tls.fails = 0
        return c
    except Exception:
        _tls.client = None
        return None


def _mark_fail():
    """单次调用整体失败 → 让本线程下次重建连接（自愈，避免带病长连）"""
    c = getattr(_tls, "client", None)
    if c is not None:                    # ★ P2-1：失效前先关闭，不留泄漏
        _close_client(c)
    _tls.client = None
    _tls.fails = getattr(_tls, "fails", 0) + 1


def _is_market_hours():
    """★ P2-2：交易时段判断（9:15-15:05，含竞价与收盘缓冲）。
    盘前/收盘后 TDX 空返回属正常（无行情），不应视为连接失败触发重连风暴。"""
    now = time.localtime()
    if now.tm_wday >= 5:
        return False
    hm = now.tm_hour * 100 + now.tm_min
    return 915 <= hm <= 1505


def available():
    """mootdx 是否可用（快速探测，不实际连接）"""
    try:
        import mootdx  # noqa
        return True
    except ImportError:
        return False


def _map_quote(row, code):
    """通达信 raw 行 → 与腾讯 quote 相同结构（datafeed 兼容）"""
    try:
        price = float(row.get("price", 0) or 0)
        yc = float(row.get("last_close", 0) or 0)
        return {
            "code": code,
            "name": "",   # 通达信批量行情无名称（列表用新浪/缓存）
            "price": price,
            "yest_close": yc,
            "open": float(row.get("open", 0) or 0),
            "high": float(row.get("high", 0) or 0),
            "low": float(row.get("low", 0) or 0),
            "volume": float(row.get("vol", 0) or 0) * 100,   # 手→股
            "amount": float(row.get("amount", 0) or 0),
            "pct_chg": round((price - yc) / yc * 100, 2) if yc > 0 else 0.0,
            "turnover": 0.0,
            "vol_ratio": 0.0,
            "limit_up": 0.0, "limit_down": 0.0,
            # ★ 盘口（打板核心）
            "bid1_price": float(row.get("bid1", 0) or 0),
            "bid1_vol": float(row.get("bid_vol1", 0) or 0) * 100,
            "ask1_price": float(row.get("ask1", 0) or 0),
            "ask1_vol": float(row.get("ask_vol1", 0) or 0) * 100,
            "outer_vol": float(row.get("b_vol", 0) or 0) * 100,   # 主动买(外盘)
            "inner_vol": float(row.get("s_vol", 0) or 0) * 100,   # 主动卖(内盘)
            # ★D-S1（2026-09-16，验收方）：五档全集——"分歧转一致"哨兵的原料
            "bid2_price": float(row.get("bid2", 0) or 0),
            "bid2_vol": float(row.get("bid_vol2", 0) or 0) * 100,
            "bid3_price": float(row.get("bid3", 0) or 0),
            "bid3_vol": float(row.get("bid_vol3", 0) or 0) * 100,
            "bid4_price": float(row.get("bid4", 0) or 0),
            "bid4_vol": float(row.get("bid_vol4", 0) or 0) * 100,
            "bid5_price": float(row.get("bid5", 0) or 0),
            "bid5_vol": float(row.get("bid_vol5", 0) or 0) * 100,
            "ask2_price": float(row.get("ask2", 0) or 0),
            "ask2_vol": float(row.get("ask_vol2", 0) or 0) * 100,
            "ask3_price": float(row.get("ask3", 0) or 0),
            "ask3_vol": float(row.get("ask_vol3", 0) or 0) * 100,
            "ask4_price": float(row.get("ask4", 0) or 0),
            "ask4_vol": float(row.get("ask_vol4", 0) or 0) * 100,
            "ask5_price": float(row.get("ask5", 0) or 0),
            "ask5_vol": float(row.get("ask_vol5", 0) or 0) * 100,
            "float_mktcap": 0.0,
            "total_mktcap": 0.0,
            "time": "",
        }
    except Exception:
        return None


def fetch_quotes_fast(codes):
    """批量行情（通达信，★ 并发：每线程一条独立长连接）。
    返回 {code: quote} 或 None（不可用/失败）。
    ★ 提速：5000 只 ≈1.0s（HTTP 并发 ≈1.7s）
    """
    if not codes:
        return {}
    try:
        # 通达信单次最多约 80 只，分批并发（线程本地连接，协议安全）
        BATCH = 80
        batches = [codes[i:i + BATCH] for i in range(0, len(codes), BATCH)]

        def _one(batch):
            out = {}
            try:
                c = _get_client()
                if c is None:
                    return out
                df = c.quotes(symbol=batch)
                if df is None or len(df) == 0:
                    return out
                for idx in range(len(df)):
                    row = df.iloc[idx].to_dict()
                    code = str(row.get("code", ""))
                    q = _map_quote(row, code)
                    if q and q["price"] > 0:
                        out[code] = q
            except Exception:
                pass
            return out

        out = {}
        try:
            # ★ Z6-P1(③)：整体 8s 上限——mootdx socket 15s > watchdog 5s 探活，
            #   TDX 卡顿时单次 overview 最坏 ~25s（TDX 15s + 腾讯 10s）必被 watchdog 判死。
            #   整体超时 = 整批降级为空结果，自然落进既有 腾讯→新浪 补缺链（不新造分支）。
            #   _pool.map(timeout=8)：超过即抛 TimeoutError，已提交线程继续后台跑完（池常驻可复用）。
            for part in _pool.map(_one, batches, timeout=8):
                out.update(part)
        except TimeoutError:
            global _TDX_TIMEOUT_COUNT
            _TDX_TIMEOUT_COUNT += 1
            _log.warning("TDX fetch_quotes_fast 整体超时(8s) 批数=%d → 降级腾讯/新浪补缺", len(batches))
        except Exception:
            pass
        # 整体失败 → 各线程下次重建连接（自愈）
        # ★ P2-2：仅交易时段才判定失败；盘前/收盘后空返回属正常（无行情），不触发重连风暴
        if not out and _is_market_hours():
            for _i in range(_POOL_SIZE):
                try:
                    _pool.submit(_mark_fail)
                except Exception:
                    pass
        return out if out else None
    except Exception:
        return None


def _bars_one(code, freq, count, adjust):
    """（池线程内执行）单只 K 线拉取"""
    try:
        c = _get_client()
        if c is None:
            return None
        kw = {}
        if adjust in ("qfq", "hfq"):
            kw["adjust"] = adjust
        df = c.bars(symbol=code, frequency=freq, offset=count, **kw)
        if df is None or len(df) == 0:
            return None
        out = []
        for idx in range(len(df)):
            r = df.iloc[idx]
            try:
                dt = str(r.get("datetime", ""))
                if freq == 9:                 # ★ P0-1 日K：mootdx 返回 "YYYY-MM-DD HH:MM"，
                    if len(dt) >= 10:         #   截断为 10 位与腾讯日K一致（防污染 kline 表）
                        dt = dt[:10]
                elif len(dt) == 16:           # 分钟K "YYYY-MM-DD HH:MM" → 补秒，与腾讯 mkline 一致
                    dt = dt + ":00"
                # ★ P0-1b：无效日期（mootdx 个别行 datetime 为 NaN → str 得 "nan"）直接丢弃，
                #   杜绝 'nan' 垃圾行再入 kline 表（会顶坏 MAX(date) 新鲜度判定）
                if len(dt) != (10 if freq == 9 else 19):
                    continue
                out.append({
                    "date": dt,
                    "open": float(r["open"]), "close": float(r["close"]),
                    "high": float(r["high"]), "low": float(r["low"]),
                    "volume": float(r.get("vol", 0) or 0) * 100,
                    "amount": float(r.get("amount", 0) or 0),
                })
            except (ValueError, KeyError, TypeError):
                continue
        return out if out else None
    except Exception:
        return None


def fetch_kline_fast(code, period="day", count=250, adjust=None):
    """K线（通达信，★ 常驻池执行，连接复用不泄漏）。
    period: day=9 / min5=0 / min15=1 / min30=2 / min60=3。
    adjust: None/raw=不复权, 'qfq'=前复权, 'hfq'=后复权（mootdx 本地复权）。
    返回 [{date,open,high,low,close,volume,amount}] 或 None。
    """
    freq_map = {"day": 9, "min5": 0, "min15": 1, "min30": 2, "min60": 3}
    freq = freq_map.get(period)
    if freq is None:
        return None
    try:
        fut = _pool.submit(_bars_one, code, freq, min(count, 800), adjust)
        return fut.result(timeout=30)
    except Exception:
        return None
