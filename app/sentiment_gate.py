# -*- coding: utf-8 -*-
"""★ Phase16 情绪周期仓位闸门：阶段 → 仓位乘数
把情绪阶段（冰点/发酵/高潮/退潮）变为仓位乘数，叠加在 position_pct 修复的 min() 之后、
指数择时乘数之后（只缩仓不扩仓，不超过 w_cap 预算）。

数据源：
- 回测：qg_zt_full 表（build_sentiment_history 重建的 2019 起逐日阶段,全市场口径）
- 实盘：sentiment.cached_sentiment() 实时阶段
- 乘数表：config.SENTIMENT_POS_MULT（由阶段收益统计推导，见 docs/reports/sentiment_pos_gate.md）

开关：config.SENTIMENT_POS_GATE（默认 False → 恒返回 1.0，零影响）
"""
import sqlite3
import threading
import time

from . import config as C

_lock = threading.Lock()
_mem = {}          # as_of -> (ts, mult)
_MEM_TTL = 60


def _ro_conn():
    return sqlite3.connect("file:%s?mode=ro" % C.DB_FILE, uri=True, timeout=10)


def _phase_from_table(as_of):
    """回测：按日期查历史阶段（qg_zt_full）。无该日数据返回 None"""
    try:
        conn = _ro_conn()
        try:
            row = conn.execute(
                "SELECT phase FROM qg_zt_full WHERE date<=? ORDER BY date DESC LIMIT 1",
                (as_of,)).fetchone()
            return row[0] if row else None
        finally:
            conn.close()
    except Exception:
        return None


def _phase_live():
    """实盘：实时情绪阶段"""
    try:
        from . import sentiment as senti
        s = senti.cached_sentiment(max_age=60)
        return (s or {}).get("phase")
    except Exception:
        return None


def phase_mult(as_of=None, use_live=False, force=False):
    """返回当前情绪阶段对应仓位乘数（浮点）。
    as_of: 回测用日期（YYYY-MM-DD，PIT）；use_live: 实盘用实时阶段。
    开关关闭 → 1.0（零影响）。未知阶段 → 1.0（保守不干预）。
    """
    if not C.SENTIMENT_POS_GATE:
        return 1.0
    now = time.time()
    key = as_of or ("live" if use_live else None)
    if key:
        with _lock:
            hit = _mem.get(key)
            if hit and not force and now - hit[0] < _MEM_TTL:
                return hit[1]
    try:
        if use_live:
            phase = _phase_live()
        else:
            phase = _phase_from_table(as_of)
        if not phase:
            return 1.0
        mult = C.SENTIMENT_POS_MULT.get(phase, 1.0)
    except Exception:
        mult = 1.0
    if key:
        with _lock:
            _mem[key] = (now, mult)
    return float(mult)


def phase_label(as_of=None, use_live=False):
    """返回 (阶段, 乘数, 置信) 供前端/日志"""
    if use_live:
        phase = _phase_live()
    else:
        phase = _phase_from_table(as_of)
    mult = phase_mult(as_of=as_of, use_live=use_live)
    low_conf = False
    if phase:
        try:
            conn = _ro_conn()
            try:
                row = conn.execute(
                    "SELECT payload FROM qg_sentiment_history "
                    "WHERE period LIKE '2019_%' ORDER BY period DESC LIMIT 1").fetchone()
                if row:
                    import json
                    stats = (json.loads(row[0]) or {}).get("phase_stats") or {}
                    st = stats.get(phase) or {}
                    low_conf = bool(st.get("low_confidence"))
            finally:
                conn.close()
        except Exception:
            pass
    return phase, mult, low_conf