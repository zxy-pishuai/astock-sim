#!/usr/bin/env python3
"""
A股模拟盘交易系统 v2.0 — 策略升级版
======================================
核心改进（基于GitHub优质量化项目研究）：
  1. RSRS 阻力支撑相对强度择时（光大证券，胜率62%）
  2. 多维度综合打分系统（替代简单信号计数）
  3. 主力资金行为识别（区分洗盘 vs 出货）
  4. Mask-first 涨跌停过滤（ml-quant-trading）
  5. 并行数据获取 + K线缓存加速

用法：
  python paper_trader.py              # 单次扫描
  python paper_trader.py --loop       # 持续模式
  python paper_trader.py --status     # 查看持仓
  python paper_trader.py --backtest   # 信号扫描（不交易）
  python paper_trader.py --quick      # 快速评分Top30
"""

import argparse
import base64
import json
import math
import os
import subprocess
import sys
import time
import urllib.request
import urllib.parse
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

# Windows UTF-8
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ============================================================
# 配置
# ============================================================
CONFIG = {
    # --- 资金与仓位 ---
    "initial_capital": 100000,
    "max_positions": 3,
    "position_pct": 0.30,

    # --- 风控参数 ---
    "stop_loss_pct": -0.05,
    "take_profit_pct": 0.08,
    "trailing_activate_pct": 0.04,
    "trailing_stop_pct": -0.03,
    "max_hold_days": 10,
    "time_stop_days": 3,          # ★新增：持仓N天仍亏损则强制退出
    "intraday_high_trigger": 0.03,   # 日内冲高触发线（浮盈曾达+3%才监控高抛）
    "intraday_pullback": 0.02,       # 从日内高点回落2%则高抛止盈

    # --- 市场择时 (RSRS) ---
    "rsrs_window": 18,
    "rsrs_zscore_window": 600,
    "rsrs_buy_threshold": 0.7,
    "rsrs_sell_threshold": -0.7,

    # --- ★多维度评分权重（替代简单信号计数）---
    "score_volume_price": 20,      # 量价齐升
    "score_breakout": 20,          # 放量突破20日新高
    "score_ma_bullish": 15,        # 均线多头排列
    "score_rsrs_bullish": 15,      # RSRS看涨
    "score_macd_golden": 10,       # MACD金叉
    "score_rsi_oversold": 10,      # RSI超卖反弹
    "score_volume_shrink": 5,      # 缩量回踩
    "score_pullback_ma": 5,        # 回踩均线企稳
    "buy_score_threshold": 25,     # ★最低买入总分（25=2个强信号/3个弱信号共振）

    # --- ★自适应市场环境（弱势切抱团，分期切核心）---
    "market_breadth_min": 0.40,
    # 强势市场(≥50%): 正常3仓/25分
    # 中性市场(35-50%): 谨慎2仓/30分
    # 弱势市场(20-35%): 抱团模式1仓/40分/必须板块前5强
    # 崩溃市场(<20%): 只平仓
    "limit_up_threshold": 9.5,
    "limit_down_threshold": -9.5,

    # --- ★板块抱团参数---
    "sector_top_n": 5,              # 弱势市场只在TopN强板块中选股
    "sector_min_rise_pct": 0.5,     # 板块平均涨幅>0.5%才算强
    "herding_min_stocks": 3,        # 板块至少N只股才纳入

    # --- 技术指标 ---
    "ma_short": 5,
    "ma_mid": 10,
    "ma_long": 20,
    "volume_ratio_high": 1.8,
    "volume_ratio_breakout": 1.5,
    "volume_shrink_ratio": 0.7,
    "rsi_period": 14,
    "rsi_oversold": 30,
    "rsi_overbought": 70,
    "macd_fast": 12,
    "macd_slow": 26,
    "macd_signal": 9,

    # --- 循环模式 ---
    "loop_interval": 120,
    "trade_only_hours": True,

    # --- 数据获取 ---
    "request_timeout": 15,
    "max_retries": 3,
    "parallel_workers": 8,         # ★并行K线获取
    "kline_cache_seconds": 300,    # ★K线缓存有效期(秒)
    # 自动交易
    "auto_trade": False,           # GUI自动交易模式（交易时段自动运行）

    # --- ★ 打板策略（1/4仓位扫板+回封）---
    "board_trade_enabled": True,       # 是否启用打板
    "board_position_ratio": 0.25,      # 总资金1/4用于打板
    "board_max_positions": 2,          # 最多同时打板2只
    "board_per_stock_pct": 0.50,       # 打板资金池内每只占50%（即总资金12.5%）
    "board_score_threshold": 40,       # 打板买入最低评分
    "board_min_pct": 7.0,              # 打板最低监控涨幅
    "board_max_pct": 9.8,              # 打板最高涨幅（排除已封死涨停的）
    "board_volume_ratio_min": 1.5,     # 最小放量倍数
    "board_turnover_min": 3.0,         # 最小换手率%
    "board_turnover_max": 25.0,        # 最大换手率%
    "board_break_loss_pct": -0.02,     # 炸板止损线（从涨停价回落2%）
    "board_next_day_high": 0.03,       # 次日高开止盈阈值
    "board_next_day_low": -0.03,       # 次日低开止损阈值
    "board_scan_interval": 5,          # 打板轮询间隔(秒)
    "board_limit_up_pct": 9.5,         # 涨停判断阈值

    # --- ★ 竞价打板策略（9:25-9:30 集合竞价高开抢筹，公示不自动交易）---
    "auction_enabled": True,           # 是否启用竞价打板
    "auction_scan_interval": 5,        # 竞价窗口内刷新间隔(秒)
    "auction_full_rescan_seconds": 30, # 全市场重扫间隔(秒)
    "auction_min_pct": 1.0,            # 最低竞价高开幅度%
    "auction_max_pct": 9.8,            # 最高竞价高开幅度%
    "auction_score_threshold": 40,     # 竞价打板公示最低评分
    "auction_top_n": 15,               # 公示候选数量
    "auction_min_amount": 5000000,     # 最低竞价成交额(500万)
    "auction_vol_ratio_min": 0.015,    # 竞价量/昨日量 最低(1.5%)
    "auction_reserve_pct": 0.25,       # 预留仓位比例(总资金1/4)
    "auction_per_stock_pct": 0.33,     # 预留池内每只占比(约8%总资金)

    # --- ★ 自选股池 + 记忆好票 + 桌面提醒 ---
    "memory_score_threshold": 60,      # 记忆好票最低评分（≥此分才记录）
    "focus_bonus": 10,                 # 自选股/记忆好票评分加分（着重关注）
    "alerts_enabled": True,            # 是否启用桌面提醒
    "alert_big_drop": -5.0,            # 日内大跌提醒阈值(%)
    "alert_drop_vol_ratio": 1.5,       # 放量下跌的量比阈值
    "alert_high_reversal": 3.0,        # 冲高回落幅度阈值(相对昨收，%)
}

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(SCRIPT_DIR, "paper_trades.json")
WATCHLIST_FILE = os.path.join(SCRIPT_DIR, "watchlist.json")
NOTIFY_FILE = os.path.join(SCRIPT_DIR, "notify.json")

# ============================================================
# 工具函数
# ============================================================

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

def _fetch_url(url, timeout=None, decode="utf-8", ref=None):
    """通用HTTP GET（返回字符串）"""
    if timeout is None:
        timeout = CONFIG["request_timeout"]
    headers = {"User-Agent": _UA}
    if ref:
        headers["Referer"] = ref
    for attempt in range(CONFIG["max_retries"]):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                # 尝试用指定编码解码
                try:
                    return raw.decode(decode)
                except (UnicodeDecodeError, LookupError):
                    return raw.decode("gbk", errors="replace")
        except Exception as e:
            if attempt == CONFIG["max_retries"] - 1:
                raise
            time.sleep(0.3 * (attempt + 1))

def fmt_time(dt=None):
    return (dt or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")

def fmt_date(dt=None):
    return (dt or datetime.now()).strftime("%Y-%m-%d")

def log(msg, level="INFO"):
    emoji = {"INFO": "  ", "OK": "✅", "WARN": "⚠️", "ERROR": "❌", "BUY": "📈", "SELL": "📉", "ALERT": "🔔"}
    print(f"[{fmt_time()}] {emoji.get(level, '  ')} {msg}")

def is_trading_time(now=None):
    if now is None:
        now = datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.strftime("%H:%M")
    return ("09:30" <= t <= "11:30") or ("13:00" <= t <= "15:00")

def is_auction_time(now=None):
    """是否在集合竞价打板窗口 9:25:00-9:29:59（开盘价已撮合确定）"""
    if now is None:
        now = datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.strftime("%H:%M:%S")
    return "09:25:00" <= t <= "09:29:59"

# ============================================================
# ★ 数据源：新浪财经（实时行情）+ 腾讯财经（K线）
# ============================================================
_kline_cache = {}
_stock_list_cache = None
_stock_list_cache_time = 0

def _code_prefix(code):
    """sh600000 / sz000001"""
    return "sh" + code if code.startswith("6") else "sz" + code


def is_main_board(code):
    """是否主板（排除创业板30xxxx和科创板68xxxx）"""
    return not (code.startswith("30") or code.startswith("68"))


def detect_board_streak(klines):
    """检测连板数：近N日连续涨停天数（不含今天）"""
    if len(klines) < 3:
        return 0
    streak = 0
    limit_pct = CONFIG["board_limit_up_pct"] / 100  # 9.5% → 0.095
    # 从倒数第二根K线往前数（倒数第一根是今天，不参与连板统计）
    for i in range(len(klines) - 2, -1, -1):
        k = klines[i]
        prev_k = klines[i - 1] if i > 0 else None
        if prev_k is None:
            break  # 第一根K线，无法比较
        prev_close = prev_k["close"]
        if prev_close > 0 and k["close"] >= prev_close * (1 + limit_pct - 0.005):
            streak += 1
        else:
            break
    return streak

def _get_stock_list():
    """获取全A股代码列表（多重来源 + 缓存1小时）"""
    global _stock_list_cache, _stock_list_cache_time
    now = time.time()
    if _stock_list_cache and (now - _stock_list_cache_time) < 3600:
        return _stock_list_cache

    all_codes = []

    # ★ 来源1: 新浪（可能被限流封IP）
    try:
        for page in range(1, 80):
            url = (f"http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
                   f"Market_Center.getHQNodeData?page={page}&num=80&sort=symbol&asc=1&node=hs_a")
            text = _fetch_url(url, timeout=10, ref="https://finance.sina.com.cn/")
            data = json.loads(text)
            if not data:
                break
            for item in data:
                code = item.get("symbol", "")
                name = item.get("name", "")
                price = float(item.get("trade", 0) or 0)
                raw = code
                if raw.startswith("sh"): code = raw[2:]
                elif raw.startswith("sz"): code = raw[2:]
                if code and name and not raw.startswith("bj") and "ST" not in name:
                    all_codes.append((code, name, price))
        if all_codes:
            log(f"股票列表(新浪): {len(all_codes)}只", "OK")
    except Exception as e:
        log(f"新浪列表失败: {e}", "WARN")

    # ★ 来源2: 东方财富（新浪被封时的备用）
    if not all_codes:
        try:
            url = ("http://80.push2.eastmoney.com/api/qt/clist/get?"
                   "pn=1&pz=6000&po=1&np=1&ut=bd1d9ddb04089700cf9c27f6f7426281"
                   "&fltt=2&invt=2&fid=f3&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
                   "&fields=f12,f14,f2")
            text = _fetch_url(url, timeout=15, ref="https://quote.eastmoney.com/")
            data = json.loads(text)
            items = data.get("data", {}).get("diff", [])
            for item in items:
                code = item.get("f12", "")
                name = item.get("f14", "")
                price = float(item.get("f2", 0) or 0)
                if code and name and "ST" not in name:
                    all_codes.append((code, name, price))
            if all_codes:
                log(f"股票列表(东财): {len(all_codes)}只", "OK")
        except Exception as e:
            log(f"东财列表失败: {e}", "WARN")

    # ★ 来源3: 本地缓存兜底
    if not all_codes:
        cache_file = os.path.join(SCRIPT_DIR, "stock_list_cache.json")
        if os.path.exists(cache_file):
            try:
                with open(cache_file, "r", encoding="utf-8") as f:
                    cached = json.load(f)
                all_codes = [(c, n, 0) for c, n in cached]
                log(f"股票列表(缓存): {len(all_codes)}只", "WARN")
            except Exception:
                pass

    # 保存缓存
    if all_codes and len(all_codes) > 1000:
        cache_file = os.path.join(SCRIPT_DIR, "stock_list_cache.json")
        try:
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump([(c, n) for c, n, _ in all_codes], f, ensure_ascii=False)
        except Exception:
            pass

    _stock_list_cache = all_codes
    _stock_list_cache_time = now
    return all_codes


def fetch_quotes(codes):
    """
    批量获取指定代码列表实时行情（新浪 batch API）
    返回: {code: {code, name, price, pct_chg, high, low, volume, amount, open_price, yest_close}}
    """
    if not codes:
        return {}

    stocks = {}
    batch_size = 400  # 新浪一次最多~800只

    for start in range(0, len(codes), batch_size):
        batch = codes[start:start + batch_size]
        symbols = ",".join(_code_prefix(c) for c in batch)
        url = f"http://hq.sinajs.cn/list={symbols}"

        try:
            text = _fetch_url(url, timeout=20, ref="https://finance.sina.com.cn/")
        except Exception:
            continue

        for line in text.strip().split("\n"):
            if "=" not in line or '"' not in line:
                continue
            try:
                code_part = line.split("=")[0].strip()
                # hq_str_sh600000 → sh600000 → 600000
                raw_code = code_part.replace("var hq_str_", "").strip()
                code = raw_code[2:]  # 去掉 sh/sz 前缀

                data_str = line.split('"')[1]
                parts = data_str.split(",")
                if len(parts) < 10:
                    continue

                name = parts[0]
                open_price = float(parts[1]) if parts[1] else 0
                yest_close = float(parts[2]) if parts[2] else 0
                price = float(parts[3]) if parts[3] else 0
                high = float(parts[4]) if parts[4] else 0
                low = float(parts[5]) if parts[5] else 0
                volume = float(parts[8]) if len(parts) > 8 and parts[8] else 0
                amount = float(parts[9]) if len(parts) > 9 and parts[9] else 0

                if price <= 0:
                    continue

                pct_chg = round((price - yest_close) / yest_close * 100, 2) if yest_close else 0

                stocks[code] = {
                    "code": code, "name": name,
                    "price": price, "pct_chg": pct_chg,
                    "high": high, "low": low,
                    "volume": volume, "amount": amount,
                    "open_price": open_price,      # 今开（竞价打板用）
                    "yest_close": yest_close,      # 昨收（竞价打板用）
                }
            except (ValueError, IndexError):
                continue

    return stocks


def fetch_all_stocks():
    """全A股实时行情（复用 fetch_quotes）"""
    codes = _get_stock_list()
    if not codes:
        return {}
    code_list = [c for c, _, _ in codes]
    return fetch_quotes(code_list)


def fetch_kline_cached(code, days=250):
    """带缓存的K线获取（腾讯优先，新浪备用）"""
    now = time.time()
    cache_key = f"{code}:{days}"
    if cache_key in _kline_cache:
        cached_time, cached_data = _kline_cache[cache_key]
        if now - cached_time < CONFIG["kline_cache_seconds"]:
            return cached_data
    klines = _fetch_kline_raw(code, days)
    if klines:
        _kline_cache[cache_key] = (now, klines)  # 空结果不缓存，下次重试
    return klines


def _fetch_kline_raw(code, days=250):
    """日K线：优先腾讯(前复权)，失败自动回退新浪"""
    klines = _fetch_kline_tencent(code, days)
    if klines:
        return klines
    return _fetch_kline_sina(code, days)


def _fetch_kline_tencent(code, days=250):
    """腾讯财经日K线（前复权）"""
    prefix = _code_prefix(code)
    url = (f"http://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
           f"param={prefix},day,,,{days},qfq")

    try:
        text = _fetch_url(url, timeout=15)
        data = json.loads(text)
    except Exception:
        return []

    stock_data = data.get("data", {}).get(prefix, {})
    raw_klines = stock_data.get("qfqday", []) or stock_data.get("day", [])

    klines = []
    for entry in raw_klines:
        try:
            if len(entry) < 6:
                continue
            klines.append({
                "date": entry[0],
                "open": float(entry[1]),
                "close": float(entry[2]),
                "high": float(entry[3]),
                "low": float(entry[4]),
                # 腾讯K线volume单位是"手"，×100统一为"股"（与新浪实时一致）
                "volume": float(entry[5]) * 100,
            })
        except (ValueError, TypeError, IndexError):
            continue
    return klines


def _fetch_kline_sina(code, days=250):
    """新浪财经日K线（备用；不复权，volume单位已是股）"""
    symbol = _code_prefix(code)
    url = (f"https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           f"CN_MarketData.getKLineData?symbol={symbol}&scale=240&ma=no&datalen={days}")

    try:
        text = _fetch_url(url, timeout=15)
        data = json.loads(text)
    except Exception:
        return []

    if not isinstance(data, list):
        return []
    klines = []
    for entry in data:
        try:
            klines.append({
                "date": entry["day"],
                "open": float(entry["open"]),
                "close": float(entry["close"]),
                "high": float(entry["high"]),
                "low": float(entry["low"]),
                "volume": float(entry["volume"]),  # 新浪已是"股"
            })
        except (KeyError, ValueError, TypeError):
            continue
    return klines

# ============================================================
# 技术指标（向量化计算）
# ============================================================

def sma(series, period):
    if len(series) < period:
        return [None] * len(series)
    result = [None] * (period - 1)
    window_sum = sum(series[:period])
    result.append(window_sum / period)
    for i in range(period, len(series)):
        window_sum += series[i] - series[i - period]
        result.append(window_sum / period)
    return result

def ema(series, period):
    if len(series) < period:
        return [None] * len(series)
    result = [None] * (period - 1)
    mult = 2 / (period + 1)
    val = sum(series[:period]) / period
    result.append(val)
    for i in range(period, len(series)):
        val = (series[i] - val) * mult + val
        result.append(val)
    return result

def calc_macd(closes):
    fast_ema = ema(closes, CONFIG["macd_fast"])
    slow_ema = ema(closes, CONFIG["macd_slow"])
    dif = [None] * len(closes)
    for i in range(len(closes)):
        if fast_ema[i] is not None and slow_ema[i] is not None:
            dif[i] = fast_ema[i] - slow_ema[i]
    dea = ema([d if d is not None else 0 for d in dif], CONFIG["macd_signal"])
    for i in range(len(closes)):
        if dif[i] is None or dea[i] is None:
            dea[i] = None
    hist = [None] * len(closes)
    for i in range(len(closes)):
        if dif[i] is not None and dea[i] is not None:
            hist[i] = (dif[i] - dea[i]) * 2
    return dif, dea, hist

def calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return [None] * len(closes)
    result = [None] * period
    gains = deque(maxlen=period)
    losses = deque(maxlen=period)
    for i in range(1, period + 1):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    for i in range(period, len(closes)):
        if i > period:
            diff = closes[i] - closes[i - 1]
            gains.append(max(diff, 0))
            losses.append(max(-diff, 0))
        avg_gain = sum(gains) / period
        avg_loss = sum(losses) / period
        if avg_loss == 0:
            result.append(100)
        else:
            result.append(100 - 100 / (1 + avg_gain / avg_loss))
    return result

def rolling_max(series, period=20):
    if len(series) < period:
        return [None] * len(series)
    result = [None] * (period - 1)
    window = deque(series[:period], maxlen=period)
    result.append(max(window))
    for i in range(period, len(series)):
        window.append(series[i])
        result.append(max(window))
    return result

# ============================================================
# ★ RSRS 阻力支撑相对强度（光大证券 金牌择时）
# ============================================================

def calc_rsrs(klines, window=18, zscore_window=600):
    """
    RSRS 右偏标准分
    high = α + β*low + ε → β越大支撑越强
    右偏标准分 = Z-Score × R² × β
    比原始斜率更稳定，回撤更小
    """
    if len(klines) < window + 2:
        return [None] * len(klines)

    highs = [k["high"] for k in klines]
    lows = [k["low"] for k in klines]
    n = len(klines)

    # 滚动OLS β（协方差/方差法，比statsmodels快100倍）
    betas = [None] * (window - 1)
    r2s = [None] * (window - 1)

    sum_low = sum(lows[:window])
    sum_high = sum(highs[:window])
    sum_low2 = sum(l * l for l in lows[:window])
    sum_low_high = sum(l * h for l, h in zip(lows[:window], highs[:window]))

    for i in range(window - 1, n):
        if i >= window:
            old_l, new_l = lows[i - window], lows[i]
            old_h, new_h = highs[i - window], highs[i]
            sum_low += new_l - old_l
            sum_high += new_h - old_h
            sum_low2 += new_l * new_l - old_l * old_l
            sum_low_high += new_l * new_h - old_l * old_h

        mean_low = sum_low / window
        mean_high = sum_high / window
        var_low = sum_low2 / window - mean_low * mean_low

        if var_low > 1e-10:
            beta = (sum_low_high / window - mean_low * mean_high) / var_low
            cov = sum_low_high / window - mean_low * mean_high
            var_high = sum(h * h for h in highs[i - window + 1:i + 1]) / window - mean_high * mean_high
            r2 = min(1.0, max(0.0, (cov * cov) / (var_low * var_high))) if var_high > 1e-10 else 0
        else:
            beta = 1.0
            r2 = 0

        betas.append(beta)
        r2s.append(r2)

    # Z-Score标准化（滑动窗口）
    zscores = [None] * len(betas)
    beta_vals = [b for b in betas if b is not None]
    if len(beta_vals) < zscore_window + window:
        return zscores

    sum_b = sum(beta_vals[:zscore_window])
    sum_b2 = sum(b * b for b in beta_vals[:zscore_window])

    for t in range(zscore_window, len(beta_vals)):
        idx = t + (window - 1)
        if idx >= len(betas):
            break
        mean_b = sum_b / zscore_window
        var_b = max(1e-10, sum_b2 / zscore_window - mean_b * mean_b)
        std_b = math.sqrt(var_b)

        zscore = (beta_vals[t] - mean_b) / std_b
        r2_val = r2s[idx] if idx < len(r2s) and r2s[idx] is not None else 0
        corrected = zscore * r2_val          # 修正标准分
        right_biased = corrected * beta_vals[t]  # 右偏标准分
        zscores[idx] = right_biased

        sum_b += beta_vals[t] - beta_vals[t - zscore_window]
        sum_b2 += beta_vals[t] ** 2 - beta_vals[t - zscore_window] ** 2

    return zscores


# ============================================================
# ★ 多维度综合评分系统
# ============================================================

def score_stock(klines, quote):
    """
    多维度评分替代简单信号计数
    返回: (total_score, signals_detail_list)
    """
    if len(klines) < 60:
        return 0, ["数据不足"]

    closes = [k["close"] for k in klines]
    volumes = [k["volume"] for k in klines]
    highs = [k["high"] for k in klines]
    lows = [k["low"] for k in klines]
    opens = [k["open"] for k in klines]

    i = len(closes) - 1
    today_close = closes[i]
    today_open = opens[i]
    today_high = highs[i]

    # 均线
    ma5 = sma(closes, 5)
    ma10 = sma(closes, 10)
    ma20 = sma(closes, 20)
    vol_ma5 = sma(volumes, 5)

    # MACD / RSI
    dif, dea, hist = calc_macd(closes)
    rsi = calc_rsi(closes, CONFIG["rsi_period"])

    # 突破
    high20 = rolling_max(highs, 20)

    # ★ RSRS
    rsrs = calc_rsrs(klines, CONFIG["rsrs_window"], CONFIG["rsrs_zscore_window"])

    total = 0
    signals = []

    today_vol = volumes[i]
    pct_chg = (today_close - closes[i - 1]) / closes[i - 1] if closes[i - 1] else 0
    vol_ratio = today_vol / vol_ma5[i] if vol_ma5[i] and vol_ma5[i] > 0 else 1.0

    # ① 量价齐升 [20分]
    if (today_close > today_open and pct_chg >= 0.02 and
            vol_ratio >= CONFIG["volume_ratio_high"]):
        total += CONFIG["score_volume_price"]
        signals.append(f"量价齐升(+{CONFIG['score_volume_price']})")

    # ② 放量突破20日新高 [20分]
    if high20[i] and today_high >= high20[i] and vol_ratio >= CONFIG["volume_ratio_breakout"]:
        total += CONFIG["score_breakout"]
        signals.append(f"放量突破20日新高(+{CONFIG['score_breakout']})")

    # ③ 均线多头排列 [15分]
    if ma5[i] and ma10[i] and ma20[i] and ma5[i] > ma10[i] > ma20[i]:
        total += CONFIG["score_ma_bullish"]
        signals.append(f"均线多头排列(+{CONFIG['score_ma_bullish']})")

    # ④ RSRS看涨 [15分]
    if rsrs[i] is not None and rsrs[i] > CONFIG["rsrs_buy_threshold"]:
        total += CONFIG["score_rsrs_bullish"]
        signals.append(f"RSRS看涨({rsrs[i]:.2f},+{CONFIG['score_rsrs_bullish']})")

    # ⑤ MACD金叉 [10分]
    if (dif[i] and dea[i] and dif[i - 1] and dea[i - 1] and
            dif[i - 1] <= dea[i - 1] and dif[i] > dea[i]):
        total += CONFIG["score_macd_golden"]
        signals.append(f"MACD金叉(+{CONFIG['score_macd_golden']})")

    # ⑥ RSI超卖反弹 [10分]
    if (rsi[i] and rsi[i - 1] and
            rsi[i - 1] < CONFIG["rsi_oversold"] and rsi[i] > rsi[i - 1]):
        total += CONFIG["score_rsi_oversold"]
        signals.append(f"RSI超卖反弹({rsi[i-1]:.0f}->{rsi[i]:.0f},+{CONFIG['score_rsi_oversold']})")

    # ⑦ 缩量回踩 [5分]
    if (ma5[i] and ma10[i] and ma5[i] > ma10[i] and
            vol_ratio <= CONFIG["volume_shrink_ratio"] and
            today_close >= ma10[i]):
        total += CONFIG["score_volume_shrink"]
        signals.append(f"缩量回踩(+{CONFIG['score_volume_shrink']})")

    # ⑧ 回踩20日线企稳 [5分]
    if (ma20[i] and 0 < today_close - ma20[i] <= ma20[i] * 0.02 and
            closes[i - 1] < closes[i]):
        total += CONFIG["score_pullback_ma"]
        signals.append(f"回踩20日线企稳(+{CONFIG['score_pullback_ma']})")

    # ⑨ 均线金叉 [5分]
    if (ma5[i] and ma10[i] and ma5[i - 1] and ma10[i - 1] and
            ma5[i - 1] <= ma10[i - 1] and ma5[i] > ma10[i]):
        total += 5
        signals.append("MA5/10金叉(+5)")

    # ⑩ 海龟突破 [10分]
    if high20[i] and today_close >= high20[i] * 0.995:
        total += 10
        signals.append("海龟20日突破(+10)")

    # ⑪ RSRS趋势加速 [5分]
    if rsrs[i] is not None and rsrs[i - 1] is not None and rsrs[i] > rsrs[i - 1] > 0:
        total += 5
        signals.append("RSRS趋势加速(+5)")

    # ⑫ 扣分：高位放量滞涨 [-15分]
    if pct_chg > 0.05 and vol_ratio > 3.0 and today_close < today_high * 0.98:
        total -= 15
        signals.append("⚠高位放量滞涨(-15)")

    # ⑬ 扣分：RSI超买 [-10分]
    if rsi[i] and rsi[i] > CONFIG["rsi_overbought"]:
        total -= 10
        signals.append(f"⚠RSI超买({rsi[i]:.0f},-10)")

    return max(0, total), signals


# ============================================================
# ============================================================
# ★ 打板策略评分（扫板+回封）
# ============================================================

def score_board_candidate(klines, quote):
    """
    打板专项评分：针对涨幅≥7%接近涨停的主板股票
    维度：涨停动量 + 放量配合 + 换手率 + 首板优先 + 板块助攻 + 早盘 + 大盘 + 盘口
    返回: (total_score, signals_list)
    """
    if len(klines) < 20:
        return 0, ["数据不足"]

    closes = [k["close"] for k in klines]
    volumes = [k["volume"] for k in klines]
    highs = [k["high"] for k in klines]
    l = len(closes)

    pct_chg = quote.get("pct_chg", 0) or 0
    price = quote.get("price", 0) or 0
    high = quote.get("high", 0) or 0
    low = quote.get("low", 0) or 0
    volume_today = quote.get("volume", 0) or 0
    turnover = quote.get("turnover", 0) or 0

    total = 0
    signals = []

    # ① 涨停动量（pct_chg越接近涨停分越高）
    if pct_chg >= 9.0:
        total += 25
        signals.append(f"涨停动量极强(+25,{pct_chg:.1f}%)")
    elif pct_chg >= 8.5:
        total += 20
        signals.append(f"涨停动量很强(+20,{pct_chg:.1f}%)")
    elif pct_chg >= 8.0:
        total += 15
        signals.append(f"涨停动量较强(+15,{pct_chg:.1f}%)")
    elif pct_chg >= 7.0:
        total += 10
        signals.append(f"涨停动量启动(+10,{pct_chg:.1f}%)")

    # ② 放量配合（当日量 vs 5日均量）
    vol_ma5 = sum(volumes[-6:-1]) / 5 if len(volumes) >= 6 else sum(volumes[:-1]) / max(len(volumes) - 1, 1)
    vol_ratio = volume_today / vol_ma5 if vol_ma5 > 0 else 1.0
    if vol_ratio >= 3.0:
        total += 20
        signals.append(f"巨量封板(+20,量比{vol_ratio:.1f})")
    elif vol_ratio >= 2.0:
        total += 15
        signals.append(f"放量扫板(+15,量比{vol_ratio:.1f})")
    elif vol_ratio >= 1.5:
        total += 10
        signals.append(f"温和放量(+10,量比{vol_ratio:.1f})")

    # ③ 换手率
    if 5 <= turnover <= 15:
        total += 15
        signals.append(f"换手适中(+15,{turnover:.1f}%)")
    elif 3 <= turnover < 5 or 15 < turnover <= 20:
        total += 10
        signals.append(f"换手可接受(+10,{turnover:.1f}%)")

    # ④ 首板优先（连板越多风险越高）
    streak = detect_board_streak(klines)
    if streak == 0:
        total += 20
        signals.append("首板优先(+20)")
    elif streak == 1:
        total += 10
        signals.append("二板接力(+10)")
    elif streak == 2:
        total += 5
        signals.append("三板博弈(+5)")

    # ⑤ 盘口形态：现价≈今日最高 → 买方强势
    if high > 0 and price >= high * 0.995:
        total += 5
        signals.append("买方强势(+5)")

    # ⑥ 剔除尾盘拉板（14:00后启动的涨停不可靠）
    now = datetime.now()
    hour = now.hour + now.minute / 60
    if hour < 10.5:  # 10:30前
        total += 10
        signals.append("早盘封板(+10)")
    elif hour < 11.5:  # 11:30前
        total += 5
        signals.append("午前封板(+5)")
    elif hour >= 14.0:  # 14:00后
        total -= 10
        signals.append("尾盘慎入(-10)")

    # ⑦ 振幅过滤（振幅过大说明分歧严重）
    if high > 0 and low > 0:
        amplitude = (high - low) / (high + low) * 200  # 日内振幅%
        if amplitude > 8:
            total -= 10
            signals.append(f"振幅过大(-10,{amplitude:.1f}%)")

    return max(0, total), signals


# ★ 主力资金行为识别（洗盘 vs 出货）
# ============================================================

def detect_washout_vs_distribution(klines):
    """
    判断最近走势是洗盘还是出货
    ★ 盘中时最后一根K线是未完成的当日K线（量价失真），必须排除，
      否则会把早盘正常的冲高回落误判成「放量冲高回落出货」
    """
    if is_trading_time() and klines:
        klines = klines[:-1]  # 排除当日未完成K线
    if len(klines) < 20:
        return "neutral", 0

    closes = [k["close"] for k in klines]
    volumes = [k["volume"] for k in klines]
    highs = [k["high"] for k in klines]
    i = len(klines) - 1

    recent_vol = volumes[-5:]
    avg_vol_20 = sum(volumes[-20:]) / 20

    vol_spike = max(recent_vol) / avg_vol_20 if avg_vol_20 > 0 else 1.0
    vol_declining = (len(recent_vol) >= 3 and
                     recent_vol[-1] < recent_vol[-2] < recent_vol[-3])
    price_5d = (closes[i] - closes[i - 5]) / closes[i - 5] if closes[i - 5] else 0
    high_to_close = (highs[i] - closes[i]) / highs[i] if highs[i] else 0

    washout_score = 0
    dist_score = 0

    # 洗盘特征：缩量、价格守住、收在高点
    if vol_declining and -0.03 < price_5d < 0.01:
        washout_score += 3
    if vol_spike < 1.5 and price_5d > -0.02:
        washout_score += 2
    if high_to_close < 0.02:
        washout_score += 1

    # 出货特征：天量、冲高回落、长上影
    if vol_spike > 3.0 and high_to_close > 0.03:
        dist_score += 3
    if vol_spike > 2.0 and price_5d < -0.03:
        dist_score += 2
    if max(recent_vol) > 5 * avg_vol_20:
        dist_score += 2

    if washout_score > dist_score and washout_score >= 3:
        return "washout", washout_score / (washout_score + dist_score + 1)
    elif dist_score > washout_score and dist_score >= 3:
        return "distribution", dist_score / (washout_score + dist_score + 1)
    return "neutral", 0



def calc_market_breadth(stocks):
    if not stocks:
        return 0
    up = sum(1 for s in stocks.values() if s.get("pct_chg", 0) > 0)
    return up / len(stocks)

# ============================================================
# ★ 板块分类与强弱分析（弱势切抱团）
# ============================================================

# 板块关键词映射（股票名称包含这些词的归入对应板块）
SECTOR_KEYWORDS = {
    "银行": ["银行"],
    "证券保险": ["证券", "保险", "信托", "期货"],
    "半导体芯片": ["芯片", "半导体", "微电子", "集成电路", "晶圆", "封测", "光刻"],
    "AI算力": ["智能", "数据", "软件", "信息", "互联", "网络", "通信", "算力", "云", "数科"],
    "消费电子": ["光电", "电子", "电路", "PCB", "面板", "显示", "触控", "传感器", "元件"],
    "新能源": ["新能源", "光伏", "风电", "储能", "电池", "锂", "钴", "镍", "氢能", "充电"],
    "汽车产业链": ["汽车", "汽配", "轮胎", "模具", "动力", "电机", "电控", "驾驶"],
    "医药生物": ["医药", "药业", "生物", "制药", "医疗", "基因", "疫苗", "诊断", "器械"],
    "军工防务": ["军工", "防务", "航天", "航空", "导航", "雷达", "军", "兵器", "船舶"],
    "食品饮料": ["食品", "饮料", "酒", "乳", "肉", "调味", "糖", "粮", "油", "啤酒"],
    "有色资源": ["有色", "矿业", "黄金", "白银", "铜", "铝", "稀土", "钢铁", "煤炭", "资源"],
    "化工材料": ["化工", "化学", "材料", "玻纤", "碳纤维", "氟", "硅", "磷", "膜"],
    "电力能源": ["电力", "能源", "燃气", "水务", "供热", "发电", "电网"],
    "地产基建": ["地产", "房产", "建设", "建筑", "建材", "装修", "工程", "园林"],
    "机械装备": ["重工", "机械", "装备", "设备", "机床", "工具", "精密", "制造"],
    "交通物流": ["交通", "物流", "港口", "高速", "铁路", "航空", "机场", "运输", "快递"],
    "传媒游戏": ["传媒", "影视", "游戏", "出版", "广告", "文化", "动漫", "娱乐"],
    "商贸零售": ["百货", "零售", "贸易", "超市", "商业", "电商", "免税"],
    "环保": ["环保", "生态", "节能", "污水", "固废"],
    "农林牧渔": ["农业", "种业", "畜牧", "渔业", "饲料", "农药", "化肥"],
    "家电家居": ["电器", "家电", "家居", "家装", "照明", "卫浴"],
}

# ★ 精确名称→板块映射（覆盖名称不含行业关键词的股票）
STOCK_NAME_SECTOR = {
    "通威股份": "新能源",        # 光伏硅料龙头
    "太极实业": "半导体芯片",     # 半导体封测
    "华天科技": "半导体芯片",     # 半导体封测
}

def classify_sector(name):
    """根据股票名称判断所属板块（先查精确名称映射，再查关键词）"""
    name = (name or "").strip()
    if name in STOCK_NAME_SECTOR:
        return STOCK_NAME_SECTOR[name]
    for sector, keywords in SECTOR_KEYWORDS.items():
        for kw in keywords:
            if kw in name:
                return sector
    return "其他"


def calc_sector_strength(stocks):
    """
    计算各板块强弱
    返回: {sector: {avg_pct, up_ratio, count, leaders: [(code,name,pct)]}}
    """
    sectors = {}
    for code, s in stocks.items():
        name = s.get("name", "")
        pct = s.get("pct_chg", 0) or 0
        sector = classify_sector(name)
        if sector not in sectors:
            sectors[sector] = {"total_pct": 0, "up_count": 0, "stocks": []}
        sectors[sector]["total_pct"] += pct
        sectors[sector]["up_count"] += 1 if pct > 0 else 0
        sectors[sector]["stocks"].append((code, name, pct))

    result = {}
    for sector, data in sectors.items():
        count = len(data["stocks"])
        if count < CONFIG["herding_min_stocks"]:
            continue
        avg_pct = data["total_pct"] / count
        up_ratio = data["up_count"] / count
        # 板块龙头：涨幅Top5
        leaders = sorted(data["stocks"], key=lambda x: x[2], reverse=True)[:5]
        result[sector] = {
            "avg_pct": round(avg_pct, 2),
            "up_ratio": round(up_ratio, 3),
            "count": count,
            "leaders": leaders,
        }
    return result


def get_top_sectors(sector_strength, top_n=5):
    """获取最强TopN板块（排除"其他"这个杂项）"""
    filtered = {k: v for k, v in sector_strength.items() if k != "其他"}
    sorted_sectors = sorted(
        filtered.items(),
        key=lambda x: (x[1]["avg_pct"], x[1]["up_ratio"]),
        reverse=True
    )
    return sorted_sectors[:top_n]


def get_market_regime(breadth):
    """
    市场环境分级
    返回: (regime_name, max_positions, score_threshold, position_pct)
    """
    if breadth >= 0.50:
        return "强势", 3, 25, 0.30
    elif breadth >= 0.35:
        return "中性", 2, 30, 0.20
    elif breadth >= 0.20:
        return "弱势(抱团)", 1, 40, 0.15
    else:
        return "崩溃(防守)", 0, 99, 0


# ============================================================
# 状态管理
# ============================================================

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"cash": CONFIG["initial_capital"], "positions": {}, "trades": []}

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)

def _get_close_on_date(code, date_str):
    """获取某股在指定日期的收盘价；当天用实时价，历史用日K收盘价"""
    # 当天（或未来）：用实时现价
    if date_str >= fmt_date():
        try:
            q = fetch_quotes([code])
            if code in q and q[code].get("price"):
                return q[code]["price"]
        except Exception:
            pass
    # 历史：从日K取收盘价
    try:
        klines = fetch_kline_cached(code, 400)
        for k in klines:
            if k.get("date") == date_str:
                return k.get("close")
    except Exception:
        pass
    return None


def get_daily_trades():
    """按天统计交割单，返回 [(日期, [交易...], 当日买入浮盈), ...] 按日期升序。
    每笔 buy 交易附带 intraday_pnl 字段（当日收盘/现价相对买入价的浮盈浮亏）"""
    state = load_state()
    trades = state.get("trades", [])
    daily = {}
    for t in trades:
        date = (t.get("time", "") or "")[:10]
        if not date:
            continue
        daily.setdefault(date, []).append(dict(t))
    result = []
    for d in sorted(daily.keys()):
        trades_d = daily[d]
        buy_intraday = 0.0
        for t in trades_d:
            if t["side"] == "buy":
                close = _get_close_on_date(t["code"], d)
                if close is not None:
                    pnl = (close - t["price"]) * t["qty"]
                    t["intraday_pnl"] = pnl
                    buy_intraday += pnl
        result.append((d, trades_d, buy_intraday))
    return result

# ============================================================
# ★ 自选股池 + 记忆好票 + 桌面通知 + 提醒
# ============================================================
_watchlist_cache = None

def load_watchlist():
    """加载自选股池 + 记忆好票 {watchlist:[{code,name,added}], memory_stocks:[{...}]}"""
    global _watchlist_cache
    if _watchlist_cache is not None:
        return _watchlist_cache
    if os.path.exists(WATCHLIST_FILE):
        try:
            with open(WATCHLIST_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if "watchlist" not in data:
                    data["watchlist"] = []
                if "memory_stocks" not in data:
                    data["memory_stocks"] = []
                _watchlist_cache = data
                return data
        except Exception:
            pass
    _watchlist_cache = {"watchlist": [], "memory_stocks": []}
    return _watchlist_cache

def save_watchlist(data=None):
    global _watchlist_cache
    if data is None:
        data = _watchlist_cache
    if data is None:
        return
    try:
        with open(WATCHLIST_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        _watchlist_cache = data
    except Exception:
        pass

def add_to_watchlist(code, name=""):
    """添加自选股，返回 (是否新增, 消息)"""
    data = load_watchlist()
    if any(w["code"] == code for w in data["watchlist"]):
        return False, "已在自选股池中"
    data["watchlist"].append({"code": code, "name": name, "added": fmt_date()})
    save_watchlist(data)
    return True, "已加入自选"

def remove_from_watchlist(code):
    """移除自选股"""
    data = load_watchlist()
    data["watchlist"] = [w for w in data["watchlist"] if w["code"] != code]
    save_watchlist(data)
    return True

def remember_stock(code, name, score, reason=""):
    """记忆好票：评分≥记忆阈值的票累计命中次数，下次扫描着重关注"""
    if score < CONFIG.get("memory_score_threshold", 60):
        return
    data = load_watchlist()
    now = fmt_date()
    for m in data["memory_stocks"]:
        if m["code"] == code:
            m["hits"] = m.get("hits", 0) + 1
            m["last_seen"] = now
            m["max_score"] = max(m.get("max_score", 0), score)
            m["last_score"] = score
            m["name"] = name
            if reason:
                m["last_reason"] = reason
            save_watchlist(data)
            return
    data["memory_stocks"].append({
        "code": code, "name": name, "score": score,
        "first_seen": now, "last_seen": now,
        "hits": 1, "max_score": score, "last_score": score,
        "reason": reason,
    })
    save_watchlist(data)

def get_focus_codes():
    """返回着重扫描的代码集合（自选股 ∪ 记忆好票）"""
    data = load_watchlist()
    codes = set()
    for w in data["watchlist"]:
        codes.add(w["code"])
    for m in data["memory_stocks"]:
        codes.add(m["code"])
    return codes

def desktop_notify(title, message):
    """Windows 桌面 toast 通知（PowerShell，无第三方依赖）"""
    if sys.platform != "win32":
        return
    try:
        # 转义特殊字符，避免破坏 PowerShell 脚本
        title = title.replace('"', "'").replace("\\", "/").replace("\n", " ")
        message = message.replace('"', "'").replace("\\", "/").replace("\n", " ")
        ps = (
            '[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null;'
            '$tpl = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02);'
            '$nodes = $tpl.GetElementsByTagName("text");'
            f'$nodes.Item(0).AppendChild($tpl.CreateTextNode("{title}")) | Out-Null;'
            f'$nodes.Item(1).AppendChild($tpl.CreateTextNode("{message}")) | Out-Null;'
            '$toast = [Windows.UI.Notifications.ToastNotification]::new($tpl);'
            '[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("A股模拟盘").Show($toast)'
        )
        encoded = base64.b64encode(ps.encode("utf-16-le")).decode("ascii")
        subprocess.run(
            ["powershell", "-NoProfile", "-EncodedCommand", encoded],
            capture_output=True, timeout=10)
    except Exception:
        pass


# ============================================================
# ★ 微信推送（Server酱）
# ============================================================
_notify_cache = None

def load_notify_config():
    """加载通知配置 {sendkey, enabled}，存 notify.json"""
    global _notify_cache
    if _notify_cache is not None:
        return _notify_cache
    cfg = {"sendkey": "", "enabled": False}
    if os.path.exists(NOTIFY_FILE):
        try:
            with open(NOTIFY_FILE, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception:
            pass
    _notify_cache = cfg
    return cfg

def save_notify_config(cfg=None):
    global _notify_cache
    if cfg is None:
        cfg = _notify_cache or {}
    try:
        with open(NOTIFY_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=1)
        _notify_cache = cfg
    except Exception:
        pass

def set_wechat(key, enabled=True):
    """设置 Server酱 SendKey 并保存，返回是否成功启用"""
    cfg = load_notify_config()
    cfg["sendkey"] = (key or "").strip()
    cfg["enabled"] = bool(enabled and cfg["sendkey"])
    save_notify_config(cfg)
    return cfg["enabled"]

def wechat_notify(title, message):
    """通过 Server酱 推送到微信（需先在 notify.json 配置 SendKey）。
    成功返回 True，未配置或失败返回 False"""
    cfg = load_notify_config()
    if not cfg.get("enabled") or not cfg.get("sendkey"):
        return False
    try:
        sendkey = cfg["sendkey"]
        url = f"https://sctapi.ftqq.com/{sendkey}.send"
        data = urllib.parse.urlencode(
            {"title": title, "desp": message}).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={
            "User-Agent": _UA,
            "Content-Type": "application/x-www-form-urlencoded",
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        return True
    except Exception as e:
        log(f"微信推送失败: {e}", "WARN")
        return False

def _kline_metrics(code, q, klines):
    """
    从K线+实时行情计算风险指标。
    返回 {vol_ratio, ma10, ma20}，K线不足返回 None
    """
    if len(klines) < 20:
        return None
    volumes = [k["volume"] for k in klines]
    closes = [k["close"] for k in klines]

    # 量比 = 当日成交量 / 前5日均量（排除当日K线）
    day_vol = q.get("volume", 0) or 0
    hist_volumes = volumes
    if klines and klines[-1].get("date") == fmt_date():
        hist_volumes = volumes[:-1]
    if len(hist_volumes) < 5:
        return None
    avg5 = sum(hist_volumes[-5:]) / 5
    vol_ratio = day_vol / avg5 if avg5 > 0 else None

    ma10 = sma(closes, 10)[-1]
    ma20 = sma(closes, 20)[-1]
    return {"vol_ratio": vol_ratio, "ma10": ma10, "ma20": ma20}


def _stock_risk_alerts(code, q, klines):
    """
    单只股票的通用风险信号（下跌类，对持仓股和自选股都适用）。
    返回 [(级别, 标题, 内容), ...]
    """
    res = []
    name = q.get("name", code)
    price = q.get("price", 0) or 0
    pct = q.get("pct_chg", 0) or 0
    high = q.get("high", 0) or 0
    yest_close = q.get("yest_close", 0) or 0
    if not price:
        return res

    metrics = _kline_metrics(code, q, klines)
    vol_ratio = metrics["vol_ratio"] if metrics else None

    # 1. 日内大跌（危险）
    if pct <= CONFIG["alert_big_drop"]:
        res.append(("danger", f"🚨 大跌预警：{name}",
                    f"日内跌幅 {pct:+.1f}%，已超大跌阈值，注意风险"))

    # 2. 冲高回落：从日内高点大幅回撤且收绿
    if yest_close > 0 and high > price:
        reversal = (high - price) / yest_close * 100
        if reversal >= CONFIG["alert_high_reversal"] and pct < 0:
            res.append(("warn", f"⚠️ 冲高回落：{name}",
                        f"从日内高点回落 {reversal:.1f}%，现价 {pct:+.1f}%，上影线抛压重"))

    # 3. 放量下跌：跌幅大且量比放大
    if vol_ratio is not None and pct <= -3.0 and vol_ratio >= CONFIG["alert_drop_vol_ratio"]:
        res.append(("warn", f"📉 放量下跌：{name}",
                    f"跌幅 {pct:+.1f}% 且量比 {vol_ratio:.1f}，资金出逃嫌疑"))

    # 4. 跌破20日线
    if metrics and metrics["ma20"] and price < metrics["ma20"]:
        res.append(("warn", f"📉 跌破均线：{name}",
                    f"现价 {price:.2f} 跌破20日线 {metrics['ma20']:.2f}，趋势转弱"))

    # 5. 主力出货（放量滞涨/冲高回落历史形态）
    try:
        behavior, conf = detect_washout_vs_distribution(klines)
        if behavior == "distribution" and conf > 0.6:
            res.append(("danger", f"🚨 主力出货：{name}",
                        f"检测到出货信号(置信{conf:.0%})，建议减仓/清仓"))
    except Exception:
        pass

    return res


def check_alerts(state, stocks):
    """
    检查提醒信号（持仓股 + 自选股/好票）
    返回: [(级别, 标题, 内容), ...]，级别: danger/warn/info
    """
    alerts = []
    focus_codes = get_focus_codes()

    # --- 持仓股提醒 ---
    for code, pos in state["positions"].items():
        q = stocks.get(code, {})
        cp = q.get("price", 0) or pos["entry_price"]
        pnl_pct = (cp - pos["entry_price"]) / pos["entry_price"] if pos["entry_price"] else 0
        name = pos.get("name", code)

        # 清仓预警（最优先）
        if pnl_pct <= CONFIG["stop_loss_pct"]:
            alerts.append(("danger", f"⚠️ 止损预警：{name}",
                           f"浮亏{pnl_pct:+.1%}，跌破止损线{CONFIG['stop_loss_pct']:.0%}，建议清仓"))
            continue
        # 通用风险信号（大跌/冲高回落/放量下跌/跌破均线/出货）
        try:
            klines = fetch_kline_cached(code, 60)
        except Exception:
            klines = []
        alerts.extend(_stock_risk_alerts(code, q, klines))
        # 止盈/做T
        if pnl_pct >= 0.05:
            high = q.get("high", 0) or 0
            low = q.get("low", 0) or 0
            if high > 0 and low > 0 and (high - low) / cp * 100 >= 3:
                alerts.append(("info", f"💹 可做T：{name}",
                               f"浮盈{pnl_pct:+.1%}且日内振幅{(high-low)/cp*100:.1f}%，可高抛低吸"))
            else:
                alerts.append(("info", f"💹 浮盈止盈：{name}",
                               f"浮盈{pnl_pct:+.1%}，可考虑部分止盈/加仓"))

    # --- 自选股/记忆好票提醒 ---
    for code in focus_codes:
        if code in state["positions"]:
            continue  # 已持仓，上面处理过
        q = stocks.get(code, {})
        if not q:
            continue
        name = q.get("name", code)
        pct = q.get("pct_chg", 0) or 0
        # 异动拉升（上涨提醒）
        if pct >= 7.0:
            alerts.append(("info", f"🔥 异动拉升：{name}",
                           f"自选/关注股涨幅{pct:+.1f}%，接近涨停，关注打板机会"))
        elif pct >= 5.0:
            alerts.append(("info", f"📈 快速拉升：{name}",
                           f"自选/关注股涨幅{pct:+.1f}%，量价齐升可关注"))
        # 通用风险信号（下跌类，新增）
        try:
            klines = fetch_kline_cached(code, 60)
        except Exception:
            klines = []
        alerts.extend(_stock_risk_alerts(code, q, klines))

    return alerts


_last_alert_time = {}   # 提醒标题 -> 上次触发时间戳（用于冷却，避免刷屏）

def emit_alerts(state, stocks):
    """检查并触发桌面提醒（带冷却时间，返回实际触发的提醒列表）"""
    if not CONFIG.get("alerts_enabled", True):
        return []
    try:
        alerts = check_alerts(state, stocks)
    except Exception as e:
        log(f"提醒检查异常: {e}", "WARN")
        return []
    now = time.time()
    fired = []
    for level, title, content in alerts:
        last = _last_alert_time.get(title, 0)
        cooldown = 600 if level == "info" else 300  # 提示类10分钟，危险/预警5分钟
        if now - last < cooldown:
            continue
        _last_alert_time[title] = now
        log(f"{title} — {content}", "ALERT")
        desktop_notify(title, content)
        wechat_notify(title, content)
        fired.append((level, title, content))
    return fired


# ============================================================
# ★ 自选股深度分析（纯本地算法，无额外API）
# ============================================================

def _gen_advice(a):
    """基于分析指标生成操作建议（规则引擎）"""
    advices = []
    if a["behavior"] == "distribution" and a["conf"] > 0.6:
        advices.append("⚠️ 检测到主力出货信号，建议减仓/清仓")
    if a["rsi"] is not None and a["rsi"] > 75:
        advices.append("RSI严重超买，切勿追高")
    elif a["rsi"] is not None and a["rsi"] > CONFIG["rsi_overbought"]:
        advices.append("RSI超买，短线防回调")
    if a["ma_state"] == "多头排列":
        if a["behavior"] == "washout":
            advices.append("多头趋势+洗盘(筹码稳)，持有为主")
        elif a["dist_high"] is not None and a["dist_high"] <= 0.02:
            advices.append("接近20日新高，放量突破则加速")
        else:
            advices.append("多头趋势，持有为主，回踩均线可加仓")
    elif a["ma_state"] == "空头排列":
        advices.append("空头排列，趋势未反转，谨慎")
    if a["chg20"] is not None and a["chg20"] < -0.15 and a["chg5"] is not None and a["chg5"] > 0.08:
        advices.append("超跌反弹形态，看量能持续性")
    if a["vol_ratio"] is not None and a["vol_ratio"] < 0.8:
        advices.append("缩量(抛压轻)，但需放量确认")
    if not advices:
        advices.append("信号中性，观望为主")
    return "；".join(advices)


def analyze_watchlist(include_market=True):
    """
    深度分析自选股池（纯本地算法，无额外API）。
    返回: {"market": {...} 或 None, "stocks": [...], "message": str}
    """
    data = load_watchlist()
    watchlist = data.get("watchlist", [])
    if not watchlist:
        return {"market": None, "stocks": [], "message": "自选股池为空，请先在「⭐自选股池」添加"}

    codes = [w["code"] for w in watchlist]

    # 1. 市场环境 + 板块强弱（全市场，较慢）
    market = None
    top_names = {}
    if include_market:
        try:
            stocks = fetch_all_stocks()
            breadth = calc_market_breadth(stocks)
            regime, max_pos, threshold, pos_pct = get_market_regime(breadth)
            sector_strength = calc_sector_strength(stocks)
            top_sectors = get_top_sectors(sector_strength, CONFIG["sector_top_n"])
            top_names = {s[0]: s[1] for s in top_sectors}
            market = {
                "regime": regime, "breadth": breadth, "max_pos": max_pos,
                "threshold": threshold,
                "top_sectors": [(s[0], s[1]["avg_pct"], s[1]["up_ratio"]) for s in top_sectors],
            }
        except Exception as e:
            market = {"regime": "获取失败", "breadth": 0, "max_pos": 0,
                      "threshold": 0, "top_sectors": [], "error": str(e)}

    # 2. 实时行情
    try:
        quotes = fetch_quotes(codes)
    except Exception:
        quotes = {}

    # 3. 逐只深度分析
    results = []
    for code in codes:
        q = quotes.get(code, {})
        name = q.get("name", code)
        sector = classify_sector(name)
        try:
            klines = fetch_kline_cached(code, 250)
        except Exception:
            klines = []
        if len(klines) < 60:
            results.append({"code": code, "name": name, "sector": sector,
                            "error": "K线数据不足"})
            continue

        closes = [k["close"] for k in klines]
        highs = [k["high"] for k in klines]
        volumes = [k["volume"] for k in klines]
        i = len(closes) - 1
        c = closes[i]

        ma5 = sma(closes, 5); ma10 = sma(closes, 10); ma20 = sma(closes, 20)
        dif, dea, hist = calc_macd(closes)
        rsi = calc_rsi(closes, CONFIG["rsi_period"])
        rsrs = calc_rsrs(klines, CONFIG["rsrs_window"], CONFIG["rsrs_zscore_window"])
        high20 = rolling_max(highs, 20)
        vol_ma5 = sma(volumes, 5)

        if ma5[i] and ma10[i] and ma20[i]:
            ma_state = ("多头排列" if ma5[i] > ma10[i] > ma20[i]
                        else "空头排列" if ma5[i] < ma10[i] < ma20[i] else "纠缠")
        else:
            ma_state = "—"
        macd_state = "—"
        if dif[i] is not None and dea[i] is not None:
            macd_state = "金叉(红柱)" if dif[i] > dea[i] else "死叉(绿柱)"

        chg5 = (c - closes[i - 5]) / closes[i - 5] if closes[i - 5] else None
        chg20 = (c - closes[i - 20]) / closes[i - 20] if i >= 20 and closes[i - 20] else None
        vol_ratio = volumes[i] / vol_ma5[i] if vol_ma5[i] and vol_ma5[i] > 0 else None
        dist_high = (high20[i] - c) / high20[i] if high20[i] else None

        score, signals = score_stock(klines, q if q else {"name": name, "price": c})
        behavior, conf = detect_washout_vs_distribution(klines)
        streak = detect_board_streak(klines)

        rsi_state = "—"
        if rsi[i] is not None:
            rsi_state = "超买" if rsi[i] > CONFIG["rsi_overbought"] else \
                        ("超卖" if rsi[i] < CONFIG["rsi_oversold"] else "中性")

        a = {
            "code": code, "name": name, "sector": sector,
            "price": q.get("price"), "pct_chg": q.get("pct_chg"),
            "amount": q.get("amount"),
            "ma_state": ma_state, "ma5": ma5[i], "ma10": ma10[i], "ma20": ma20[i],
            "macd_state": macd_state, "dif": dif[i], "dea": dea[i],
            "rsi": rsi[i], "rsi_state": rsi_state, "rsrs": rsrs[i],
            "vol_ratio": vol_ratio, "chg5": chg5, "chg20": chg20,
            "dist_high": dist_high, "score": score, "signals": signals,
            "behavior": behavior, "conf": conf, "streak": streak,
        }
        # 板块强弱
        if sector in top_names:
            a["sector_avg_pct"] = top_names[sector]["avg_pct"]
            a["sector_in_top"] = True
        else:
            a["sector_avg_pct"] = None
            a["sector_in_top"] = False
        a["advice"] = _gen_advice(a)
        results.append(a)

    return {"market": market, "stocks": results, "message": ""}


# ============================================================
# 退出检查（改进版：含时间止损 + 出货检测）
# ============================================================

def check_exits(state, stocks):
    exited = []
    for code, pos in list(state["positions"].items()):
        quote = stocks.get(code, {})
        current_price = quote.get("price", 0) or 0
        if not current_price:
            current_price = pos["entry_price"]

        entry_price = pos["entry_price"]
        pnl_pct = (current_price - entry_price) / entry_price
        # ★ 日内最高价（新浪行情 parts[4]=high 为当日累计最高，能捕捉轮询漏掉的瞬时冲高）
        intraday_high = quote.get("high", 0) or 0
        peak = pos.get("peak", entry_price)
        if intraday_high > peak:
            peak = intraday_high
        elif current_price > peak:
            peak = current_price
        state["positions"][code]["peak"] = peak

        entry_date = datetime.strptime(pos["entry_date"], "%Y-%m-%d")
        days_held = (datetime.now() - entry_date).days
        state["positions"][code]["days"] = days_held

        reason = None
        is_board = pos.get("board_trade", False)

        # ★ 打板专属退出规则（优先级高于常规规则）
        if is_board:
            # 1. 炸板检测：买入当日从涨停附近大幅回落
            entry_pct = pos.get("entry_pct", 0)
            current_pct = quote.get("pct_chg", 0) or 0
            if days_held == 0 and entry_pct >= 8.0 and current_pct < entry_pct - 3:
                reason = f"炸板(买+{entry_pct:.1f}%→现+{current_pct:.1f}%)"

            # 2. 次日低开止损 / 高开止盈
            if not reason and days_held >= 1:
                if pnl_pct <= CONFIG["board_next_day_low"]:
                    reason = f"打板次日低开止损({pnl_pct:+.1%})"
                elif pnl_pct >= 0.05:
                    # 次日涨超5%：卖一半落袋
                    half_qty = pos["qty"] // 200 * 100  # 一半，整百股
                    if half_qty >= 100:
                        half_pnl = round((current_price - entry_price) * half_qty, 2)
                        state["cash"] += current_price * half_qty
                        pos["qty"] -= half_qty
                        state["trades"].append({
                            "code": code, "name": pos["name"], "side": "sell",
                            "price": round(current_price, 2), "qty": half_qty,
                            "time": fmt_time(),
                            "reason": f"打板半仓止盈(+{pnl_pct:+.1%})",
                            "pnl": half_pnl,
                        })
                        log(f"🎯 {pos['name']}({code}) 打板半仓止盈: "
                            f"卖{half_qty}股, 盈亏{half_pnl:+.2f}", "SELL")
                        state["positions"][code] = pos
                        # 不删持仓，剩余一半继续持有

            # 3. 次日过后转普通持仓（用常规退出规则）
            if not reason and days_held >= 2:
                state["positions"][code]["board_trade"] = False
                is_board = False

        # 常规退出规则
        if not reason:
            if pnl_pct <= CONFIG["stop_loss_pct"]:
                reason = "止损"
            elif pnl_pct >= CONFIG["take_profit_pct"]:
                reason = "止盈"
            # ★ 冲高回落止盈（"冲高就跑"）：日内曾冲高到触发线，现已回落明显且仍有浮盈
            elif (days_held >= 1 and intraday_high > 0 and
                  intraday_high >= entry_price * (1 + CONFIG["intraday_high_trigger"]) and
                  (intraday_high - current_price) / intraday_high >= CONFIG["intraday_pullback"] and
                  pnl_pct >= 0):
                reason = f"冲高回落止盈(高{intraday_high:.2f}→现{current_price:.2f})"
            elif peak >= entry_price * (1 + CONFIG["trailing_activate_pct"]):
                if current_price <= peak * (1 + CONFIG["trailing_stop_pct"]):
                    reason = f"移动止损(峰值{peak:.2f})"
            elif days_held >= CONFIG["time_stop_days"] and pnl_pct < 0:
                reason = f"时间止损(持有{days_held}天亏{pnl_pct:+.1%})"
            elif days_held >= CONFIG["max_hold_days"]:
                reason = f"超时退出(持有{days_held}天)"

        # ★ 主力出货检测
        if not reason and pnl_pct < 0.05:
            try:
                k = fetch_kline_cached(code, 60)
                behavior, conf = detect_washout_vs_distribution(k)
                if behavior == "distribution" and conf > 0.6:
                    reason = f"主力出货(置信{conf:.0%})"
            except Exception:
                pass

        if reason:
            qty = pos["qty"]
            pnl = round((current_price - entry_price) * qty, 2)
            log(f"{pos['name']}({code}) {reason}: "
                f"{entry_price:.2f} -> {current_price:.2f}, 盈亏{pnl:+.2f}({pnl_pct:+.2%})", "SELL")
            state["cash"] += current_price * qty
            state["trades"].append({
                "code": code, "name": pos["name"], "side": "sell",
                "price": round(current_price, 2), "qty": qty,
                "time": fmt_time(), "reason": reason, "pnl": pnl,
            })
            exited.append(code)

    for code in exited:
        del state["positions"][code]
    return len(exited)


# ============================================================
# ★ 并行扫描 + 评分买入
# ============================================================

def scan_candidate(code, quote):
    """扫描单只候选股 → 返回评分或None"""
    try:
        klines = fetch_kline_cached(code, 250)
        if len(klines) < 60:
            return None
        score, signals = score_stock(klines, quote)
        if score < CONFIG["buy_score_threshold"]:
            return None
        # 主力出货过滤
        behavior, conf = detect_washout_vs_distribution(klines)
        if behavior == "distribution" and conf > 0.7:
            return None
        return {
            "code": code, "name": quote["name"],
            "price": quote["price"], "pct_chg": quote.get("pct_chg", 0),
            "score": score, "signals": signals,
        }
    except Exception:
        return None


def scan_and_trade(stocks, state, verbose=True):
    # 先检查退出
    check_exits(state, stocks)

    # ★ 市场环境分级
    breadth = calc_market_breadth(stocks)
    regime, max_pos, score_threshold, pos_pct = get_market_regime(breadth)

    # ★ 板块强弱分析
    sector_strength = calc_sector_strength(stocks)
    top_sectors = get_top_sectors(sector_strength, CONFIG["sector_top_n"])
    strong_sector_names = {s[0] for s in top_sectors}
    strong_sector_leaders = set()
    for _, info in top_sectors:
        for code, name, pct in info["leaders"]:
            strong_sector_leaders.add(code)

    if verbose:
        log(f"市场环境: {regime} | 上涨{breadth:.1%} | 仓位{max_pos}只 | 阈值{score_threshold}分")
        if top_sectors:
            ts = ", ".join(f"{s[0]}(+{s[1]['avg_pct']:.1f}%)" for s in top_sectors[:5])
            log(f"最强板块: {ts}", "OK")

    # 崩溃市场：只平仓
    if max_pos == 0:
        if verbose:
            log("崩溃市场(上涨<20%)，只平仓不开仓", "WARN")
        return 0

    used = len(state["positions"])
    if used >= max_pos:
        if verbose:
            log(f"已达{regime}模式最大持仓{max_pos}只，跳过买入")
        return 0
    slots = max_pos - used
    position_cash = state["cash"] * pos_pct

    # ★ 着重关注集合（自选股 ∪ 记忆好票）
    focus_codes = get_focus_codes()

    # ★ 快速预过滤（仅用实时数据）
    candidates = []
    for code, q in stocks.items():
        if code in state["positions"]:
            continue
        # 排除创业板/科创板
        if not is_main_board(code):
            continue
        price = q.get("price", 0) or 0
        if price < 5 or price > 150:
            continue
        if "ST" in q.get("name", ""):
            continue
        pct = q.get("pct_chg", 0) or 0
        if pct >= CONFIG["limit_up_threshold"] or pct <= CONFIG["limit_down_threshold"]:
            continue
        amount = q.get("amount", 0) or 0
        is_focus = code in focus_codes
        # 自选/关注股跳过流动性门槛，始终纳入扫描（着重关注）
        if amount < 80000000 and not is_focus:
            continue
        candidates.append((code, q))

    # 按成交额排序，Top 600
    candidates.sort(key=lambda x: x[1].get("amount", 0), reverse=True)
    candidates = candidates[:600]

    if verbose:
        log(f"候选池: {len(candidates)} 只（流动性前600）")

    # ★ 并行扫描评分
    buy_list = []
    with ThreadPoolExecutor(max_workers=CONFIG["parallel_workers"]) as ex:
        futures = {ex.submit(scan_candidate, c, q): c for c, q in candidates}
        done = 0
        for f in as_completed(futures):
            done += 1
            if verbose and done % 200 == 0:
                log(f"  扫描: {done}/{len(candidates)}")
            r = f.result()
            if r:
                # ★ 自选股/记忆好票加分（着重关注）
                if r["code"] in focus_codes:
                    r["score"] += CONFIG["focus_bonus"]
                    r["signals"].append(f"重点关注(+{CONFIG['focus_bonus']})")
                # ★ 板块抱团加分：属于最强板块的股票 +10分
                sector = classify_sector(r["name"])
                if sector in strong_sector_names:
                    r["score"] += 10
                    r["signals"].append(f"板块抱团:{sector}(+10)")
                # ★ 龙头加分：板块内涨幅Top5 +5分
                if r["code"] in strong_sector_leaders:
                    r["score"] += 5
                    r["signals"].append(f"板块龙头(+5)")
                # ★ 记忆好票：高分候选自动标记，下次扫描着重关注
                remember_stock(r["code"], r["name"], r["score"],
                               "、".join(r["signals"][:2]))
                buy_list.append(r)

    # 过滤：按市场环境调整的阈值
    buy_list = [b for b in buy_list if b["score"] >= score_threshold]
    buy_list.sort(key=lambda x: x["score"], reverse=True)

    if verbose:
        log(f"达标(≥{score_threshold}分, {regime}模式): {len(buy_list)} 只",
            "OK" if buy_list else "INFO")
        for b in buy_list[:8]:
            log(f"  {b['name']}({b['code']}) 评分{b['score']}分 | {'、'.join(b['signals'][:4])}")

    # ★ 弱势市场下：进一步限制——必须在最强板块内
    if regime == "弱势(抱团)" and strong_sector_names:
        buy_list = [b for b in buy_list if classify_sector(b["name"]) in strong_sector_names]
        if verbose:
            log(f"抱团过滤后(仅Top{CONFIG['sector_top_n']}板块): {len(buy_list)} 只",
                "OK" if buy_list else "WARN")

    # 执行买入
    bought = 0
    for b in buy_list[:slots]:
        if state["cash"] < position_cash * 0.5:
            log("现金不足", "WARN")
            break
        qty = int(position_cash / b["price"] / 100) * 100
        if qty < 100:
            continue
        cost = qty * b["price"]
        if cost > state["cash"]:
            qty = int(state["cash"] * 0.95 / b["price"] / 100) * 100
            if qty < 100:
                continue
            cost = qty * b["price"]

        state["cash"] -= cost
        state["positions"][b["code"]] = {
            "name": b["name"], "qty": qty,
            "entry_price": b["price"], "entry_time": fmt_time(),
            "entry_date": fmt_date(), "peak": b["price"],
            "days": 0, "score": b["score"],
        }
        state["trades"].append({
            "code": b["code"], "name": b["name"], "side": "buy",
            "price": b["price"], "qty": qty, "time": fmt_time(),
            "reason": f"[{regime}]评分{b['score']}分: " + "、".join(b["signals"][:4]),
        })
        log(f"{b['name']}({b['code']}) {b['price']:.2f}x{qty}股 "
            f"~{cost:.0f}元 | [{regime}]评分{b['score']}分", "BUY")
        bought += 1
        if len(state["positions"]) >= max_pos:
            break
    return bought


# ============================================================
# ★ 打板策略 — 秒级快速扫描
# ============================================================

def board_scan_and_trade(stocks, state, verbose=False):
    """
    打板快速扫描+交易：
    1. 从已有行情中筛选涨幅7%~9.8%的主板股票
    2. 快速K线+打板评分
    3. 执行买入（使用打板资金池）
    返回: 买入笔数
    """
    if not CONFIG.get("board_trade_enabled", True):
        return 0

    # 打板持仓计数
    board_positions = {c: p for c, p in state["positions"].items()
                       if p.get("board_trade")}
    if len(board_positions) >= CONFIG["board_max_positions"]:
        return 0

    # 打板资金池
    board_pool = state["cash"] * CONFIG["board_position_ratio"]
    per_stock_cash = board_pool * CONFIG["board_per_stock_pct"]
    if board_pool < 2000:
        return 0

    # 筛选打板候选：主板 + 涨幅7%~9.8% + 非持仓
    candidates = []
    for code, q in stocks.items():
        if code in state["positions"]:
            continue
        if not is_main_board(code):
            continue
        price = q.get("price", 0) or 0
        if price < 3 or price > 100:
            continue
        if "ST" in q.get("name", ""):
            continue
        pct = q.get("pct_chg", 0) or 0
        if pct < CONFIG["board_min_pct"] or pct >= CONFIG["board_max_pct"]:
            continue
        amount = q.get("amount", 0) or 0
        if amount < 50000000:
            continue
        candidates.append((code, q))

    if not candidates:
        return 0

    # 按涨幅排序（越接近涨停越优先）
    candidates.sort(key=lambda x: x[1].get("pct_chg", 0), reverse=True)

    # 板块助攻统计
    sector_burst = {}  # {sector: count of ≥7% stocks}
    for code, q in candidates:
        sector = classify_sector(q.get("name", ""))
        sector_burst[sector] = sector_burst.get(sector, 0) + 1

    # 快速K线+评分
    board_list = []
    for code, q in candidates:
        try:
            klines = fetch_kline_cached(code, 250)
            if len(klines) < 60:
                continue
            score, signals = score_board_candidate(klines, q)

            # 板块助攻加分
            sector = classify_sector(q.get("name", ""))
            burst_count = sector_burst.get(sector, 0)
            if burst_count >= 2:
                score += 15
                signals.append(f"板块助攻:{sector}(+15,{burst_count}只)")
            elif burst_count >= 1:
                score += 8
                signals.append(f"板块同涨:{sector}(+8)")

            # 大盘配合
            breadth = calc_market_breadth(stocks)
            if breadth >= 0.50:
                score += 10
                signals.append("大盘强势(+10)")
            elif breadth >= 0.35:
                score += 5
                signals.append("大盘中性(+5)")
            elif breadth < 0.20:
                score -= 15
                signals.append("大盘弱势(-15)")

            if score >= CONFIG["board_score_threshold"]:
                board_list.append({
                    "code": code, "name": q["name"],
                    "price": q.get("price", 0),
                    "pct_chg": q.get("pct_chg", 0),
                    "score": score, "signals": signals,
                    "sector": sector,
                })
        except Exception:
            continue

    if not board_list:
        return 0

    board_list.sort(key=lambda x: x["score"], reverse=True)

    # 执行打板买入
    bought = 0
    slots = CONFIG["board_max_positions"] - len(board_positions)

    for b in board_list[:slots]:
        if state["cash"] < per_stock_cash * 0.5:
            break
        qty = int(per_stock_cash / b["price"] / 100) * 100
        if qty < 100:
            continue
        cost = qty * b["price"]
        if cost > state["cash"]:
            qty = int(state["cash"] * 0.95 / b["price"] / 100) * 100
            if qty < 100:
                continue
            cost = qty * b["price"]

        state["cash"] -= cost
        state["positions"][b["code"]] = {
            "name": b["name"], "qty": qty,
            "entry_price": b["price"], "entry_time": fmt_time(),
            "entry_date": fmt_date(), "peak": b["price"],
            "days": 0, "score": b["score"],
            "board_trade": True,  # ★ 标记为打板持仓
            "entry_pct": b.get("pct_chg", 0),  # 入场涨幅（炸板检测用）
            "sector": b.get("sector", ""),
        }
        state["trades"].append({
            "code": b["code"], "name": b["name"], "side": "buy",
            "price": b["price"], "qty": qty, "time": fmt_time(),
            "reason": f"[打板]评分{b['score']}分: " + "、".join(b["signals"][:3]),
        })
        log(f"🎯 {b['name']}({b['code']}) {b['price']:.2f}x{qty}股 "
            f"~{cost:.0f}元 | [打板]评分{b['score']}分 {b.get('sector','')}", "BUY")
        bought += 1

    return bought


def run_board_scan():
    """秒级打板轮询入口（供主循环调用）"""
    if not CONFIG.get("board_trade_enabled", True):
        return 0

    # 交易时间检查
    if CONFIG.get("trade_only_hours", True) and not is_trading_time():
        return 0

    try:
        # 只拉流动性Top300的行情（快速，~1-2秒）
        codes = _get_stock_list()
        if not codes:
            return 0

        # 主板Top300流动性
        mb_codes = [(c, n, p) for c, n, p in codes if is_main_board(c) and p > 0]
        # 按之前记忆的价格排序（近似流动性）
        mb_codes.sort(key=lambda x: x[2], reverse=True)
        top_codes = mb_codes[:300]

        # 批量获取行情
        stocks = {}
        batch_size = 300
        symbols = ",".join(_code_prefix(c) for c, _, _ in top_codes[:batch_size])
        url = f"http://hq.sinajs.cn/list={symbols}"

        try:
            text = _fetch_url(url, timeout=10, ref="https://finance.sina.com.cn/")
            for line in text.strip().split("\n"):
                if "hq_str_" not in line:
                    continue
                try:
                    raw = line.split('"')[1]
                    parts = raw.split(",")
                    if len(parts) < 10:
                        continue
                    code_full = line.split("hq_str_")[1].split("=")[0]
                    code = code_full[2:]
                    name = parts[0]
                    open_price = float(parts[1]) if parts[1] else 0
                    yest = float(parts[2]) if parts[2] else 0
                    price = float(parts[3]) if parts[3] else 0
                    high = float(parts[4]) if parts[4] else 0
                    low = float(parts[5]) if parts[5] else 0
                    volume = float(parts[8]) if len(parts) > 8 and parts[8] else 0
                    amount = float(parts[9]) if len(parts) > 9 and parts[9] else 0
                    if price <= 0:
                        continue
                    pct_chg = (price - yest) / yest * 100 if yest > 0 else 0
                    stocks[code] = {
                        "code": code, "name": name, "price": price,
                        "pct_chg": pct_chg, "high": high, "low": low,
                        "volume": volume, "amount": amount,
                        "open_price": open_price, "yest_close": yest,
                    }
                except (ValueError, IndexError):
                    continue
        except Exception:
            return 0

        if not stocks:
            return 0

        state = load_state()
        bought = board_scan_and_trade(stocks, state, verbose=True)
        if bought:
            save_state(state)
        return bought
    except Exception:
        return 0


# ============================================================
# ★ 竞价打板策略（9:25-9:30 集合竞价高开抢筹）
# ============================================================
_auction_state = {
    "last_full_scan": 0,       # 上次全市场扫描时间戳
    "candidate_codes": [],     # 高开候选池（用于增量刷新）
    "results": [],             # 最新公示结果
    "scan_time": "",           # 扫描时间
}

def score_auction_candidate(q, klines, sector_burst, auction_breadth):
    """
    竞价打板专项评分：针对集合竞价高开的主板股票
    维度：竞价高开幅度 + 竞价量比 + 昨日涨停基因 + 连板高度 + 竞价额 + 板块助攻 + 大盘情绪 + 低价基因
    满分约 135，公示阈值 40
    """
    if len(klines) < 20:
        return 0, ["数据不足"]

    open_price = q.get("open_price", 0) or 0
    yest_close = q.get("yest_close", 0) or 0
    auction_vol = q.get("volume", 0) or 0
    amount = q.get("amount", 0) or 0
    price = q.get("price", 0) or 0

    # 竞价高开幅度（用今开价，比现价更准——9:25-9:30现价=开盘价）
    auction_pct = (open_price - yest_close) / yest_close * 100 if yest_close > 0 else 0
    # 昨日成交量（K线最后一根）
    yest_vol = klines[-1]["volume"] if klines else 0
    # 竞价量比 = 竞价成交量 / 昨日全天成交量
    vol_ratio = auction_vol / yest_vol if yest_vol > 0 else 0

    total = 0
    signals = []

    # ① 竞价高开幅度（黄金区 3%~7%）
    if 3.0 <= auction_pct <= 7.0:
        total += 20
        signals.append(f"竞价高开黄金区(+20,{auction_pct:.1f}%)")
    elif 2.0 <= auction_pct < 3.0 or 7.0 < auction_pct <= 9.0:
        total += 12
        signals.append(f"竞价高开较强(+12,{auction_pct:.1f}%)")
    elif 1.0 <= auction_pct < 2.0:
        total += 6
        signals.append(f"竞价温和高开(+6,{auction_pct:.1f}%)")
    elif 9.0 < auction_pct <= 9.8:
        total += 10
        signals.append(f"竞价逼近涨停(+10,{auction_pct:.1f}%)")

    # ② 竞价量比（抢筹强度）
    if vol_ratio >= 0.10:
        total += 25
        signals.append(f"竞价爆量抢筹(+25,量比{vol_ratio:.0%})")
    elif vol_ratio >= 0.05:
        total += 18
        signals.append(f"竞价放量(+18,量比{vol_ratio:.0%})")
    elif vol_ratio >= 0.03:
        total += 12
        signals.append(f"竞价温和放量(+12,量比{vol_ratio:.0%})")
    elif vol_ratio >= 0.015:
        total += 6
        signals.append(f"竞价略放量(+6,量比{vol_ratio:.0%})")

    # ③ 昨日涨停基因（一进二/接力）
    if len(klines) >= 2:
        yest_close_2 = klines[-1]["close"]
        prev_close_2 = klines[-2]["close"] if len(klines) >= 2 else 0
        if prev_close_2 > 0 and yest_close_2 >= prev_close_2 * 1.095:
            total += 15
            signals.append("昨日涨停(+15)")
            # 一字/秒板特征：昨日振幅小（开盘≈收盘）
            if klines[-1]["open"] > 0 and abs(klines[-1]["open"] - yest_close_2) / yest_close_2 < 0.01:
                total += 5
                signals.append("昨日一字板(+5)")

    # ④ 连板高度（首板优先，高位板回避）
    streak = detect_board_streak(klines)
    if streak == 0:
        total += 20
        signals.append("首板基因(+20)")
    elif streak == 1:
        total += 12
        signals.append("二板接力(+12)")
    elif streak == 2:
        total += 5
        signals.append("三板博弈(+5)")
    elif streak >= 3:
        total -= 10
        signals.append(f"高位板慎入(-10,{streak}板)")

    # ⑤ 竞价成交额（过滤交投不活跃小票）
    if amount >= 30000000:
        total += 10
        signals.append(f"竞价额充足(+10,{amount/1e4:.0f}万)")
    elif amount >= 10000000:
        total += 5
        signals.append(f"竞价额一般(+5,{amount/1e4:.0f}万)")

    # ⑥ 板块助攻（同板块竞价高开家数）
    sector = classify_sector(q.get("name", ""))
    burst_count = sector_burst.get(sector, 0)
    if burst_count >= 3:
        total += 15
        signals.append(f"板块竞价共振(+15,{sector}×{burst_count})")
    elif burst_count >= 1:
        total += 8
        signals.append(f"板块竞价同涨(+8,{sector})")

    # ⑦ 大盘竞价情绪（全市场竞价高开占比）
    if auction_breadth >= 0.60:
        total += 10
        signals.append(f"大盘竞价强势(+10,{auction_breadth:.0%})")
    elif auction_breadth >= 0.40:
        total += 5
        signals.append(f"大盘竞价中性(+5,{auction_breadth:.0%})")
    elif auction_breadth < 0.30:
        total -= 10
        signals.append(f"大盘竞价弱(-10,{auction_breadth:.0%})")

    # ⑧ 低价基因（低价小盘更易连板）
    if 0 < price < 20:
        total += 5
        signals.append("低价基因(+5)")
    elif price > 50:
        total -= 5
        signals.append("高价股减分(-5)")

    return max(0, total), signals


def run_auction_scan(force=False, verbose=False):
    """
    竞价打板扫描入口（供主循环 9:25-9:30 调用）
    返回: 公示候选列表 [{code,name,price,auction_pct,vol_ratio,score,signals,sector}]
    注意：只公示不自动买入（实盘参考）
    """
    if not CONFIG.get("auction_enabled", True):
        return []
    if not is_auction_time():
        _auction_state["results"] = []
        return []

    now = time.time()
    # 首次或超过全市场重扫间隔，才重新拉全市场行情（省时间）
    if force or (now - _auction_state["last_full_scan"]) > CONFIG["auction_full_rescan_seconds"]:
        try:
            stocks = fetch_all_stocks()
        except Exception:
            return _auction_state["results"]
        if not stocks:
            return _auction_state["results"]
        _auction_state["last_full_scan"] = now

        # 过滤竞价高开候选池（主板 + 高开区间 + 非ST + 成交额门槛）
        candidates = []
        for code, q in stocks.items():
            if not is_main_board(code):
                continue
            if "ST" in q.get("name", ""):
                continue
            open_price = q.get("open_price", 0) or 0
            yest_close = q.get("yest_close", 0) or 0
            if open_price <= 0 or yest_close <= 0:
                continue
            auction_pct = (open_price - yest_close) / yest_close * 100
            if auction_pct < CONFIG["auction_min_pct"] or auction_pct >= CONFIG["auction_max_pct"]:
                continue
            if (q.get("amount", 0) or 0) < CONFIG["auction_min_amount"]:
                continue
            candidates.append((code, q))

        # 缓存候选池代码（用于后续增量刷新）
        _auction_state["candidate_codes"] = [c for c, _ in candidates]

        # 大盘竞价情绪（全市场竞价高开>0占比）
        up_cnt = sum(1 for s in stocks.values()
                     if (s.get("open_price", 0) or 0) > 0 and (s.get("yest_close", 0) or 0) > 0
                     and (s.get("open_price", 0) - s.get("yest_close", 0)) / s.get("yest_close", 0) > 0)
        total_cnt = sum(1 for s in stocks.values()
                        if (s.get("open_price", 0) or 0) > 0 and (s.get("yest_close", 0) or 0) > 0)
        auction_breadth = up_cnt / total_cnt if total_cnt > 0 else 0.5
    else:
        # 增量模式：候选池已缓存，直接复用（候选池为空则返回空）
        if not _auction_state["candidate_codes"]:
            return []
        # 重新拉候选池行情（快，几十只）
        candidates = []
        codes = _auction_state["candidate_codes"]
        for start in range(0, len(codes), 400):
            batch = codes[start:start + 400]
            symbols = ",".join(_code_prefix(c) for c in batch)
            url = f"http://hq.sinajs.cn/list={symbols}"
            try:
                text = _fetch_url(url, timeout=10, ref="https://finance.sina.com.cn/")
                for line in text.strip().split("\n"):
                    if "hq_str_" not in line:
                        continue
                    try:
                        raw = line.split('"')[1]
                        parts = raw.split(",")
                        if len(parts) < 10:
                            continue
                        code = line.split("hq_str_")[1].split("=")[0][2:]
                        name = parts[0]
                        open_price = float(parts[1]) if parts[1] else 0
                        yest = float(parts[2]) if parts[2] else 0
                        price = float(parts[3]) if parts[3] else 0
                        volume = float(parts[8]) if len(parts) > 8 and parts[8] else 0
                        amount = float(parts[9]) if len(parts) > 9 and parts[9] else 0
                        if price <= 0:
                            continue
                        pct_chg = (price - yest) / yest * 100 if yest > 0 else 0
                        candidates.append((code, {
                            "code": code, "name": name, "price": price,
                            "pct_chg": pct_chg, "volume": volume, "amount": amount,
                            "open_price": open_price, "yest_close": yest,
                        }))
                    except (ValueError, IndexError):
                        continue
            except Exception:
                continue
        # 用已有数据近似大盘情绪（候选池高开占比不准确，沿用上次的或取0.5）
        auction_breadth = 0.5

    if not candidates:
        return _auction_state["results"]

    # 板块竞价助攻统计
    sector_burst = {}
    for code, q in candidates:
        sector = classify_sector(q.get("name", ""))
        sector_burst[sector] = sector_burst.get(sector, 0) + 1

    # 逐个评分
    results = []
    for code, q in candidates:
        try:
            klines = fetch_kline_cached(code, 250)
            if len(klines) < 20:
                continue
            score, signals = score_auction_candidate(q, klines, sector_burst, auction_breadth)
            if score >= CONFIG["auction_score_threshold"]:
                open_price = q.get("open_price", 0) or 0
                yest_close = q.get("yest_close", 0) or 0
                auction_pct = (open_price - yest_close) / yest_close * 100 if yest_close > 0 else 0
                yest_vol = klines[-1]["volume"] if klines else 0
                vol_ratio = (q.get("volume", 0) or 0) / yest_vol if yest_vol > 0 else 0
                results.append({
                    "code": code, "name": q.get("name", ""),
                    "price": q.get("price", 0),
                    "auction_pct": round(auction_pct, 2),
                    "vol_ratio": round(vol_ratio, 3),
                    "amount": q.get("amount", 0),
                    "score": score, "signals": signals,
                    "sector": classify_sector(q.get("name", "")),
                    "streak": detect_board_streak(klines),
                })
        except Exception:
            continue

    results.sort(key=lambda x: x["score"], reverse=True)
    results = results[:CONFIG["auction_top_n"]]
    _auction_state["results"] = results
    _auction_state["scan_time"] = fmt_time()

    if verbose and results:
        log(f"🎯 竞价打板扫描: {len(results)}只候选 (大盘竞价高开占比{auction_breadth:.0%})", "OK")
    return results


def get_auction_results():
    """供GUI读取最新竞价打板公示结果"""
    return _auction_state["results"], _auction_state["scan_time"]


# ============================================================
# 状态显示
# ============================================================

def show_status(state, stocks):
    print("\n" + "=" * 65)
    print("  A股模拟盘 v2.0 — 策略升级版")
    print("=" * 65)

    for code, pos in state["positions"].items():
        cp = stocks.get(code, {}).get("price", 0) or pos["entry_price"]
        pos["current_price"] = cp
        pos["pnl_pct"] = (cp - pos["entry_price"]) / pos["entry_price"]
        pos["pnl_value"] = (cp - pos["entry_price"]) * pos["qty"]

    total_market = sum(
        p.get("current_price", p["entry_price"]) * p["qty"]
        for p in state["positions"].values()
    )
    total_asset = state["cash"] + total_market
    total_pnl = total_asset - CONFIG["initial_capital"]

    print(f"  初始资金: ¥{CONFIG['initial_capital']:,.0f}")
    print(f"  总资产:   ¥{total_asset:,.0f}  "
          f"(盈亏 {total_pnl:+,.0f} / {total_pnl/CONFIG['initial_capital']:+.2%})")
    print(f"  现金余额: ¥{state['cash']:,.0f}")
    print(f"  持仓市值: ¥{total_market:,.0f}")
    print(f"  持仓数量: {len(state['positions'])}/{CONFIG['max_positions']}")

    # 市场环境
    if stocks:
        breadth = calc_market_breadth(stocks)
        regime, max_pos, threshold, pos_pct = get_market_regime(breadth)
        regime_icons = {"强势": "🟢", "中性": "🟡", "弱势(抱团)": "🟠", "崩溃(防守)": "🔴"}
        icon = regime_icons.get(regime, "⚪")
        print(f"\n  市场环境: {icon} {regime} | 上涨占比: {breadth:.1%} | "
              f"允许{max_pos}仓 | 阈值{threshold}分 | 仓位{pos_pct:.0%}")

        # 板块分析
        sector_strength = calc_sector_strength(stocks)
        top_sectors = get_top_sectors(sector_strength, CONFIG["sector_top_n"])
        if top_sectors:
            print(f"  强势板块:", end="")
            for s_name, s_info in top_sectors:
                leaders = s_info.get("leaders", [])
                leader_str = f" 龙头:{','.join(name[:4] for _,name,_ in leaders[:3])}" if leaders else ""
                print(f" {s_name}(+{s_info['avg_pct']:.1%} {s_info['up_ratio']:.0%}涨{leader_str})", end="")
            print()

    sell_trades = [t for t in state.get("trades", []) if t["side"] == "sell"]
    win_trades = [t for t in sell_trades if t.get("pnl", 0) > 0]
    if sell_trades:
        wr = len(win_trades) / len(sell_trades)
        realized = sum(t.get("pnl", 0) for t in sell_trades)
        print(f"  已平仓: {len(sell_trades)}笔 | 胜率: {wr:.0%} | "
              f"已实现盈亏: ¥{realized:+,.0f}")

    if state["positions"]:
        print(f"\n  {'代码':<8}{'名称':<8}{'持仓':>6}{'成本':>8}{'现价':>8}"
              f"{'盈亏':>10}{'盈亏%':>8}{'最高':>8}{'天数':>4}")
        print("  " + "-" * 66)
        for code, pos in state["positions"].items():
            cp = pos.get("current_price", pos["entry_price"])
            print(f"  {code:<8}{pos['name']:<8}{pos['qty']:>6}"
                  f"{pos['entry_price']:>8.2f}{cp:>8.2f}"
                  f"{pos.get('pnl_value', 0):>+10.0f}"
                  f"{pos.get('pnl_pct', 0):>+7.2%}"
                  f"{pos.get('peak', pos['entry_price']):>8.2f}"
                  f"{pos.get('days', 0):>4}")

    if state.get("trades"):
        print(f"\n  最近交易:")
        for t in state["trades"][-8:]:
            e = "📈" if t["side"] == "buy" else "📉"
            pnl_s = f" 盈亏{t['pnl']:+,.0f}" if "pnl" in t else ""
            print(f"  {e} {t['time']} {t['side']} {t['name']}({t['code']}) "
                  f"{t['price']:.2f}x{t['qty']}股 {t['reason'][:40]}{pnl_s}")

    print("=" * 65 + "\n")


# ============================================================
# 回测/快速扫描模式
# ============================================================

def run_backtest():
    """扫描全市场信号（不交易，纯统计）"""
    log("📊 信号扫描模式")

    try:
        stocks = fetch_all_stocks()
    except Exception as e:
        log(f"获取行情失败: {e}", "ERROR")
        return

    breadth = calc_market_breadth(stocks)
    regime, max_pos, threshold, pos_pct = get_market_regime(breadth)
    regime_icons = {"强势": "🟢", "中性": "🟡", "弱势(抱团)": "🟠", "崩溃(防守)": "🔴"}
    icon = regime_icons.get(regime, "⚪")
    log(f"市场上涨占比: {breadth:.1%} → {icon} {regime} ({max_pos}仓/阈值{threshold}分/仓位{pos_pct:.0%})")

    # 板块分析
    sector_strength = calc_sector_strength(stocks)
    top_sectors = get_top_sectors(sector_strength, CONFIG["sector_top_n"])
    if top_sectors:
        log(f"\n  强势板块 Top{len(top_sectors)}:")
        for i, (s_name, s_info) in enumerate(top_sectors):
            leaders = s_info.get("leaders", [])
            leader_str = f" | 龙头: {', '.join(f'{name[:4]}({pct:+.1%})' for _,name,pct in leaders[:3])}" if leaders else ""
            log(f"  {i+1}. {s_name}: 均涨{s_info['avg_pct']:+.1%} "
                f"上涨{s_info['up_ratio']:.0%}({int(s_info['up_ratio']*s_info['count'])}/{s_info['count']}){leader_str}")

    candidates = []
    for code, q in stocks.items():
        price = q.get("price", 0) or 0
        if price <= 0 or price > 200:
            continue
        if "ST" in q.get("name", ""):
            continue
        if abs(q.get("pct_chg", 0) or 0) >= 9.5:
            continue
        if (q.get("amount", 0) or 0) < 50000000:
            continue
        candidates.append((code, q))

    log(f"候选池: {len(candidates)} 只")

    # 构建板块/龙头集合
    strong_sector_names = {s[0] for s in top_sectors} if top_sectors else set()
    strong_sector_leaders = set()
    for _, info in (top_sectors or []):
        for code, name, pct in info["leaders"]:
            strong_sector_leaders.add(code)

    results = []
    with ThreadPoolExecutor(max_workers=CONFIG["parallel_workers"]) as ex:
        futures = {ex.submit(scan_candidate, c, q): c for c, q in candidates}
        for f in as_completed(futures):
            r = f.result()
            if r:
                # ★ 板块抱团加分 +10
                sector = classify_sector(r["name"])
                if sector in strong_sector_names:
                    r["score"] += 10
                    r["signals"].append(f"板块抱团:{sector}(+10)")
                # ★ 板块龙头加分 +5
                if r["code"] in strong_sector_leaders:
                    r["score"] += 5
                    r["signals"].append(f"板块龙头(+5)")
                results.append(r)

    results.sort(key=lambda x: x["score"], reverse=True)

    log(f"\n{'='*70}")
    log(f"  Top 20 评分信号 (阈值{CONFIG['buy_score_threshold']}分)")
    log(f"{'='*70}")
    for i, r in enumerate(results[:20]):
        # 优先显示板块相关信号
        bonus_signals = [s for s in r["signals"] if "抱团" in s or "龙头" in s]
        other_signals = [s for s in r["signals"] if "抱团" not in s and "龙头" not in s]
        display_sigs = other_signals[:3] + bonus_signals
        log(f"  #{i+1:2d} {r['name']:<8s} {r['code']:<8s} "
            f"¥{r['price']:<8.2f} 评分:{r['score']:>3d}分 "
            f"涨跌:{r['pct_chg']:>+6.2f}% | {'; '.join(display_sigs)}")

    bins = {"80+": 0, "60-79": 0, "50-59": 0, "35-49": 0}
    for r in results:
        s = r["score"]
        if s >= 80: bins["80+"] += 1
        elif s >= 60: bins["60-79"] += 1
        elif s >= 50: bins["50-59"] += 1
        elif s >= 35: bins["35-49"] += 1
    log(f"\n  评分分布: " + " | ".join(f"{k}:{v}只" for k, v in bins.items()))


# ============================================================
# ★ 交互式菜单
# ============================================================

def clear_screen():
    os.system("cls" if sys.platform == "win32" else "clear")


def press_any_key():
    input("\n  按 Enter 返回主菜单...")


def menu_header():
    clear_screen()
    state = load_state()
    print()
    print("  ╔══════════════════════════════════════════════════════╗")
    print("  ║        A股模拟盘交易系统 v2.0                        ║")
    print("  ╠══════════════════════════════════════════════════════╣")
    print(f"  ║  现金: ¥{state['cash']:>10,.0f}    持仓: {len(state['positions'])}只"
             f"    已平仓: {sum(1 for t in state.get('trades',[]) if t['side']=='sell')}笔"
             f"  ║")
    print("  ╠══════════════════════════════════════════════════════╣")
    print("  ║  [1] 查看持仓状态                                   ║")
    print("  ║  [2] 单次扫描交易（检查退出+寻找买入）              ║")
    print("  ║  [3] 信号扫描（只看不买）                           ║")
    print("  ║  [4] 持续循环模式（交易时段自动运行）               ║")
    print("  ║  [5] 查看/修改配置                                  ║")
    print("  ║  [0] 退出                                           ║")
    print("  ╚══════════════════════════════════════════════════════╝")
    return state


def menu_status():
    """菜单：查看持仓"""
    clear_screen()
    state = load_state()
    try:
        stocks = fetch_all_stocks()
        log("行情已刷新", "OK")
    except Exception:
        log("获取行情失败，显示缓存数据", "WARN")
        stocks = {}
    show_status(state, stocks)
    press_any_key()


def menu_scan_once():
    """菜单：单次扫描"""
    clear_screen()
    log("🚀 开始单次扫描交易...")
    try:
        run_once(verbose=True)
    except Exception as e:
        log(f"扫描出错: {e}", "ERROR")
    press_any_key()


def menu_signal_scan():
    """菜单：信号扫描"""
    clear_screen()
    run_backtest()
    press_any_key()


def menu_loop():
    """菜单：持续循环"""
    clear_screen()
    log("🔄 进入持续循环模式")
    log("   Ctrl+C 可随时停止返回菜单")
    print()
    try:
        run_loop()
    except KeyboardInterrupt:
        log("用户停止，返回菜单", "INFO")
        press_any_key()


def menu_config():
    """菜单：查看/修改配置"""
    clear_screen()
    print("\n  " + "=" * 55)
    print("  当前配置（只读，修改请编辑脚本 CONFIG 字典）")
    print("  " + "=" * 55)
    for k, v in CONFIG.items():
        print(f"  {k:<30s} = {v}")
    print("  " + "=" * 55)
    print("\n  提示：用记事本打开 paper_trader.py，修改 CONFIG 字典即可")
    press_any_key()


def interactive_menu():
    """主菜单循环"""
    while True:
        menu_header()
        choice = input("\n  请选择 [0-5]: ").strip()
        if choice == "1":
            menu_status()
        elif choice == "2":
            menu_scan_once()
        elif choice == "3":
            menu_signal_scan()
        elif choice == "4":
            menu_loop()
        elif choice == "5":
            menu_config()
        elif choice == "0":
            clear_screen()
            print("\n  再见！\n")
            break
        else:
            print("  无效选择，重试...")
            time.sleep(0.5)


# ============================================================
# 主程序
# ============================================================

def run_once(verbose=True):
    if verbose:
        log("🚀 A股模拟盘 v2.0 启动...")
    state = load_state()
    if "trades" not in state:
        state["trades"] = []

    try:
        stocks = fetch_all_stocks()
    except Exception as e:
        log(f"获取行情失败: {e}", "ERROR")
        return state

    if not stocks:
        log("未获取到行情数据", "ERROR")
        return state

    if verbose:
        log(f"获取到 {len(stocks)} 只股票行情")
        show_status(state, stocks)
        log("🔍 多维度评分扫描中...")

    bought = scan_and_trade(stocks, state, verbose)
    save_state(state)

    # ★ 桌面提醒（自选股/持仓预警：清仓、加仓、主力出货、做T）
    emit_alerts(state, stocks)

    if verbose:
        log(f"✅ 完成: 买入{bought}笔 | 现金¥{state['cash']:,.0f} | 持仓{len(state['positions'])}只")
    return state


def run_loop():
    log("🔄 持续模式 v2.0 | Ctrl+C 停止 | 竞价打板+打板轮询已启用")
    while True:
        now = datetime.now()

        # ★★ 竞价打板窗口（9:25-9:30）优先处理
        if is_auction_time(now):
            try:
                results = run_auction_scan(verbose=True)
                if results:
                    top = results[0]
                    log(f"🎯 竞价打板: {len(results)}只候选, 首位 {top['name']}({top['code']}) "
                        f"高开{top['auction_pct']:+.1f}% 评分{top['score']}分", "OK")
            except Exception as e:
                log(f"竞价打板异常: {e}", "WARN")
            time.sleep(CONFIG["auction_scan_interval"])
            continue

        if CONFIG["trade_only_hours"] and not is_trading_time(now):
            t = now.strftime("%H:%M")
            msg = ("距开盘 09:30" if t < "09:30" and now.weekday() < 5 else
                   "距下午开盘 13:00" if t < "13:00" and now.weekday() < 5 else
                   "非交易日")
            log(f"⏸️ 非交易时段({msg})，等待...")
            time.sleep(60)
            continue
        try:
            # 完整扫描（~30s）
            run_once(verbose=True)
        except Exception as e:
            log(f"扫描异常: {e}", "ERROR")

        # ★ 打板秒级轮询（全扫描之间插空）
        board_rounds = CONFIG["loop_interval"] // CONFIG["board_scan_interval"]
        for _ in range(board_rounds):
            now2 = datetime.now()
            if not is_trading_time(now2):
                break
            try:
                result = run_board_scan()
                if result:
                    log(f"🎯 打板轮询: 买入{result}笔", "OK")
            except Exception as e:
                log(f"打板轮询异常: {e}", "WARN")
            time.sleep(CONFIG["board_scan_interval"])


def main():
    parser = argparse.ArgumentParser(description="A股模拟盘交易系统 v2.0")
    parser.add_argument("--loop", action="store_true", help="持续循环模式")
    parser.add_argument("--status", action="store_true", help="查看持仓状态")
    parser.add_argument("--reset", action="store_true", help="重置模拟盘")
    parser.add_argument("--config", action="store_true", help="显示配置")
    parser.add_argument("--backtest", action="store_true", help="扫描信号统计（不交易）")
    parser.add_argument("--quick", action="store_true", help="快速模式（同--backtest）")
    parser.add_argument("--menu", action="store_true", help="交互式菜单（推荐）")
    args = parser.parse_args()

    if args.menu:
        interactive_menu()
        return
    if args.config:
        print(json.dumps(CONFIG, ensure_ascii=False, indent=2))
        return
    if args.reset:
        ok = input("⚠️ 确认重置？清空所有数据。(yes/no): ")
        if ok.lower() == "yes":
            save_state({"cash": CONFIG["initial_capital"], "positions": {}, "trades": []})
            log("✅ 已重置")
        return
    if args.backtest or args.quick:
        run_backtest()
        return
    if args.status:
        try:
            stocks = fetch_all_stocks()
        except Exception:
            stocks = {}
        show_status(load_state(), stocks)
        return
    if args.loop:
        run_loop()
    else:
        # 默认进入交互式菜单
        interactive_menu()


if __name__ == "__main__":
    main()
