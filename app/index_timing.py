# -*- coding: utf-8 -*-
"""★ 4.6 指数择时闸门（阶段2，参考 QuantsPlaybook 择时研报思路）
在情绪周期第一层 get_regime 输出之后叠加第二层"指数择时闸门"：
只调仓位/阈值，不改变情绪周期逻辑。
三层因子（全部基于上证指数日K，PIT 安全、纯标准库）：
  1) 上证指数 MA20/MA60 多空关系：多头 ×1.0 / 均线纠缠 ×0.9 / 空头 ×0.8
  2) 指数 20 日波动率分位（近 250 日滚动分位）：>80% 高波动 ×0.6 / 50~80% ×0.8 / <50% ×1.0
  3) 北向资金 5 日趋势：官方每日净流入披露自 2024-08 停更（接口返回 NULL），
     有本地缓存 data/northbound_flow.json（可选；官方披露已停更，系统未内置抓取工具，需外部维护）
     则按 5 日累计净流入方向给 ×1.0 / ×0.85；否则中性 ×1.0
输出：仓位乘数（三腿乘积，clamp 到 [INDEX_TIMING_MIN, 1.0]）+ 择时理由
开关：config.INDEX_TIMING_ENABLED（默认 False；开启前须先回测验证，见 docs/reports/phase2_index_timing.md）
调用方：engine.Backtest(params.index_timing=True)（回测）/ trader 仓位分配（实盘，开关控制）
"""
import json
import os
import sqlite3
import threading
import time

from . import config as C

_IDX_CODE = "sh000001"           # 上证指数（市场温度计）
_cache_lock = threading.Lock()
_idx_cache = None                # (ts, klines)
_idx_cache_ts = 0


# ==================== 数据（本地 market.db，PIT 安全） ====================
def _sane_rows(rows):
    """★ F1（2026-09-08）：通用 K 线行断言（个股/指数共用）。
    校验 9 列元组 (code,period,date,open,high,low,close,volume,amount)：
    high>=low、high>=max(open,close)、low<=min(open,close)、volume>=0，
    拒绝 0<volume<100（<1 手）与 0<amount<1000（<1 千元）的非物理值。
    断言失败的行不写库并打印 WARN。返回过滤后的合法行。"""
    ok = []
    for r in rows:
        try:
            _, _, d, o, h, l, c, v, a = r
            if not d or o is None:
                continue
            bad = None
            if h < l:
                bad = f"high({h})<low({l})"
            elif h < max(o, c) or l > min(o, c):
                bad = f"high/low 与 open/close 越界(h={h},l={l},o={o},c={c})"
            elif v is not None and v < 0:
                bad = f"volume<0({v})"
            elif v is not None and 0 < v < 100:
                bad = f"volume 非物理值({v})"
            elif a is not None and 0 < a < 1000:
                bad = f"amount 非物理值({a})"
            if bad:
                print(f"[WARN] _sane_rows 拒绝 {r[0]} {d}: {bad}", flush=True)
                continue
            ok.append(r)
        except Exception:
            continue
    return ok


def _idx_stale(rows):
    """指数日K是否陈旧：最新日期距今 >5 自然日且今天为工作日（周末/节假日无新数据不补拉）。"""
    if not rows:
        return True
    try:
        from datetime import datetime as _dt
        last = _dt.strptime(rows[-1][0], "%Y-%m-%d")
        if (last - _dt.now()).days <= -5:
            return _dt.now().weekday() < 5
    except Exception:
        pass
    return False


def _load_idx_klines():
    """读取上证指数日K（code='sh000001'）。为空/陈旧时尝试线上拉取一次（幂等落库）。"""
    global _idx_cache, _idx_cache_ts
    now = time.time()
    with _cache_lock:
        if _idx_cache and now - _idx_cache_ts < 300:
            return _idx_cache
    rows = []
    try:
        conn = sqlite3.connect(C.DB_FILE, timeout=15)
        try:
            rows = conn.execute(
                "SELECT date,open,high,low,close,volume,amount FROM kline "
                "WHERE code=? AND period='day' ORDER BY date", (_IDX_CODE,)).fetchall()
        finally:
            conn.close()
    except Exception:
        rows = []
    # ★ BUG 修复（2026-09-08）：指数日K陈旧自动补拉。
    #   原逻辑仅 len<120 才线上拉取 → 600 行永不触发 → 指数永远停在旧日期
    #   （实测停在 2026-08-18，3 周未更新，择时/面板用陈旧数据）。
    #   现加"最新日期陈旧 → 线上拉一次补最新"，_fetch_online 幂等落库。
    if len(rows) < 120 or _idx_stale(rows):
        online = _fetch_online()
        if online:
            rows = online
    kl = [{"date": r[0], "open": r[1], "high": r[2], "low": r[3],
           "close": r[4], "volume": r[5], "amount": r[6]} for r in rows
          if r[0] and r[4]]
    with _cache_lock:
        _idx_cache, _idx_cache_ts = kl, now
    return kl


def _fetch_online():
    """指数日K线上拉取（腾讯 fqkline），幂等写入 market.db"""
    import urllib.request
    try:
        url = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
               f"param={_IDX_CODE},day,,,600,qfq")
        req = urllib.request.Request(url, headers={"User-Agent":
                                                   "Mozilla/5.0 (Windows NT 10.0)"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        sd = (data.get("data") or {}).get(_IDX_CODE) or {}
        raw = sd.get("qfqday") or sd.get("day") or []
        out = []
        for e in raw:
            try:
                if len(e) < 6:
                    continue
                # ★ F1（2026-09-08）：腾讯 fqkline 数组序为
                #   [date, open, close, high, low, volume]（6 元素，无 amount）。
                #   旧代码按 (open,high,low,close) 位置解析 → 指数 high=收盘、low=最高、
                #   close=最低、amount 恒 0。现按真实顺序映射；该接口无成交额 → 显式写 0
                #   （指数成交额需另接源，如腾讯 rt qt.gtimg.cn 或东财）。
                out.append((_IDX_CODE, "day", e[0], float(e[1]), float(e[3]),
                            float(e[4]), float(e[2]), float(e[5]) * 100, 0.0))
            except (ValueError, IndexError, TypeError):
                continue
        out = _sane_rows(out)
        if out:
            conn = sqlite3.connect(C.DB_FILE, timeout=15)
            try:
                conn.executemany(
                    "INSERT OR REPLACE INTO kline(code,period,date,open,high,low,"
                    "close,volume,amount) VALUES(?,?,?,?,?,?,?,?,?)", out)
                conn.commit()
            finally:
                conn.close()
        # ★ P1 修复：返回与 DB SELECT 一致的 7 列布局 (date,open,high,low,close,volume,amount)。
        #   原直接返回 9 列存库元组 → _load_idx_klines 字段错位（date='sh000001'、close=high 值）
        return [(r[2], r[3], r[4], r[5], r[6], r[7], r[8]) for r in out]
    except Exception:
        return []


# ==================== 北向资金 5 日趋势 ====================
def _nb_factor(as_of):
    """北向资金 5 日趋势因子。缓存文件格式（data/northbound_flow.json）：
    {"dates": [{"date": "YYYY-MM-DD", "net": 净流入亿元}, ...]}（升序）
    官方日披露 2024-08 起停更 → 无缓存或数据不足一律中性 1.0。
    返回 (mult, reason)
    """
    path = os.path.join(C.DATA_DIR, "northbound_flow.json")
    try:
        if not os.path.exists(path):
            return 1.0, "北向日披露停更(2024-08起)/无缓存，中性"
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        dates = d.get("dates") or []
        if len(dates) < 5:
            return 1.0, "北向缓存数据不足，中性"
        # PIT：只用 <= as_of 的条目
        usable = [x for x in dates if x.get("date", "") <= (as_of or "9999-12-31")]
        if len(usable) < 5:
            return 1.0, "北向缓存覆盖不足，中性"
        last5 = usable[-5:]
        net = sum(float(x.get("net") or 0) for x in last5)
        if net > 0:
            return 1.0, f"北向5日净流入{net:.0f}亿"
        return 0.85, f"北向5日净流出{abs(net):.0f}亿"
    except Exception:
        return 1.0, "北向数据读取失败，中性"


# ==================== 主闸门 ====================
def _sma(series, period):
    if len(series) < period:
        return None
    s = sum(series[-period:])
    return s / period


def _vol20_series(closes):
    """滚动 20 日年化波动率序列（与前缀 None 对齐）"""
    n = len(closes)
    out = [None] * n
    for i in range(20, n):
        seg = closes[i - 19:i + 1]
        rets = [seg[j] / seg[j - 1] - 1 for j in range(1, 20) if seg[j - 1] > 0]
        if len(rets) >= 2:
            m = sum(rets) / len(rets)
            var = sum((r - m) ** 2 for r in rets) / (len(rets) - 1)
            out[i] = (var ** 0.5) * (244 ** 0.5)
    return out


def timing_multiplier(as_of=None):
    """指数择时闸门 → (仓位乘数, 理由列表)。
    as_of: 信号日（YYYY-MM-DD，回测 PIT）；None = 最新。
    返回乘数 ∈ [INDEX_TIMING_MIN, 1.0]（config 可调）。
    """
    reasons = []
    kl = _load_idx_klines()
    n = len(kl)
    if n < 80:
        return 1.0, ["指数历史不足，闸门中性"]
    closes = [k["close"] for k in kl]
    # 定位信号日索引（<=as_of 的最后一行）
    i = n - 1
    if as_of:
        for j in range(n - 1, -1, -1):
            if kl[j]["date"] <= as_of:
                i = j
                break
    if i < 60:
        return 1.0, ["指数历史不足(信号日)"] + reasons
    mult = 1.0

    # 1) MA20/MA60 多空
    ma20 = _sma(closes[:i + 1], 20)
    ma60 = _sma(closes[:i + 1], 60)
    if ma20 and ma60:
        gap = (ma20 - ma60) / ma60
        if gap > C.INDEX_TIMING_MA_FLAT:
            m = C.INDEX_TIMING_MULT_UP
            reasons.append(f"指数MA20({ma20:.0f})>MA60({ma60:.0f})多头")
        elif gap < -C.INDEX_TIMING_MA_FLAT:
            m = C.INDEX_TIMING_MULT_DOWN
            reasons.append(f"指数MA20({ma20:.0f})<MA60({ma60:.0f})空头")
        else:
            m = C.INDEX_TIMING_MULT_FLAT
            reasons.append(f"指数均线纠缠(偏离{gap:+.2%})")
        mult *= m
    else:
        reasons.append("指数均线数据不足")

    # 2) 20日波动率分位（近 250 日）
    vols = _vol20_series(closes)
    v_now = vols[i]
    if v_now:
        hist = [v for v in vols[max(60, i - C.INDEX_TIMING_VOL_PERCENTILE):i]
                if v is not None]
        if hist:
            below = sum(1 for v in hist if v <= v_now)
            pct = below / len(hist)
            if pct > 0.80:
                m = C.INDEX_TIMING_MULT_HIGHVOL
                reasons.append(f"指数高波动(20日波动分位{pct:.0%})降仓")
            elif pct > 0.50:
                m = C.INDEX_TIMING_MULT_MIDVOL
                reasons.append(f"指数波动偏高(分位{pct:.0%})")
            else:
                m = 1.0
            mult *= m
        else:
            reasons.append("指数波动分位历史不足")
    else:
        reasons.append("指数波动率数据不足")

    # 3) 北向 5 日趋势
    m3, r3 = _nb_factor(as_of)
    mult *= m3
    reasons.append(r3)

    mult = max(C.INDEX_TIMING_MIN, min(1.0, mult))
    return round(mult, 3), reasons