# -*- coding: utf-8 -*-
"""带调用计时的 scan_once 压力测试：统计 moneyflow/consensus 等热点调用耗时。"""
import time

from app import config as C
from app import consensus as cs
from app import datafeed as df
from app import risk as rk
from app import scoring as sc
from app import sentiment as senti
from app import tdx as T
from app.trader import TradingEngine

# 拦截买入
rk.engine.check_buy = lambda *a, **k: (False, "bench")
# 强制可开仓
_orig_regime = sc.get_regime
sc.get_regime = lambda b: (lambda r, m, t, p: (r, max(m, 6), t, p))(*_orig_regime(b))
_orig_senti = senti.cached_sentiment
senti.cached_sentiment = lambda *a, **k: {"open": True, "phase": "高潮", "score": 80, "max_days": 5, "yesterday": {}}

# 调用计时
_stats = {"mf": 0.0, "mf_n": 0, "con": 0.0, "con_n": 0}
_omf = sc.moneyflow_signals

def _mf(code, name=""):
    t0 = time.time()
    r = _omf(code, name)
    _stats["mf"] += time.time() - t0
    _stats["mf_n"] += 1
    return r

sc.moneyflow_signals = _mf
_ocon = cs.consensus

def _con(code, klines=None, quote=None, name=""):
    t0 = time.time()
    r = _ocon(code, klines, quote, name)
    _stats["con"] += time.time() - t0
    _stats["con_n"] += 1
    return r

cs.consensus = _con

eng = TradingEngine()
for i in range(2):
    with df._lock:
        df._mem_quotes.clear()
    _stats["mf"] = 0.0
    _stats["con"] = 0.0
    t0 = time.time()
    eng.scan_once(verbose=False)
    t1 = time.time()
    print(f"[scan #{i + 1}] 总耗时: {t1 - t0:.2f}s 候选={len(eng.last_scan)}")
    print(f"[stats #{i + 1}] moneyflow: {_stats['mf_n']}次 共{_stats['mf']:.1f}s | consensus: {_stats['con_n']}次 共{_stats['con']:.1f}s")

sc.moneyflow_signals = _omf
cs.consensus = _ocon
rk.engine.check_buy = lambda *a, **k: None
sc.get_regime = _orig_regime
senti.cached_sentiment = _orig_senti