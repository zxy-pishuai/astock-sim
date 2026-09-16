# -*- coding: utf-8 -*-
"""scan_once 全环节计时（粗筛后），定位剩余热点。"""
import time

from app import consensus as cs
from app import datafeed as df
from app import minute_vol as mv
from app import portfolio
from app import risk as rk
from app import scoring as sc
from app import sentiment as senti
from app.trader import TradingEngine

rk.engine.check_buy = lambda *a, **k: (False, "bench")
_oreg = sc.get_regime
sc.get_regime = lambda b: (lambda r, m, t, p: (r, max(m, 6), t, p))(*_oreg(b))
_os = senti.cached_sentiment
senti.cached_sentiment = lambda *a, **k: {"open": True, "phase": "高潮", "score": 80, "max_days": 5, "yesterday": {}}

S = {}

def T(name):
    def deco(fn):
        def w(*a, **k):
            t0 = time.time()
            r = fn(*a, **k)
            S[name] = S.get(name, 0.0) + (time.time() - t0)
            return r
        return w
    return deco

df.fetch_all_stocks = T("行情")(df.fetch_all_stocks)
sc.calc_breadth = T("广度")(sc.calc_breadth)
sc.calc_sector_strength = T("板块强度")(sc.calc_sector_strength)
eng_method = TradingEngine
_orig_score_one = TradingEngine._score_one

def _w_score_one(self, c, q):
    t0 = time.time()
    r = _orig_score_one(self, c, q)
    S["评分"] = S.get("评分", 0.0) + (time.time() - t0)
    return r

TradingEngine._score_one = _w_score_one
sc.moneyflow_signals = T("资金面")(sc.moneyflow_signals)
mv.minute_vol_check = T("分时量")(mv.minute_vol_check)
cs.consensus = T("共识")(cs.consensus)
portfolio.compute_weights = T("组合权重")(portfolio.compute_weights)

eng = TradingEngine()
for i in range(2):
    with df._lock:
        df._mem_quotes.clear()
    for k in S:
        S[k] = 0.0
    t0 = time.time()
    eng.scan_once(verbose=False)
    t1 = time.time()
    print(f"--- scan #{i + 1}: {t1 - t0:.2f}s 候选={len(eng.last_scan)} ---")
    for k, v in sorted(S.items(), key=lambda x: -x[1]):
        print(f"   {k}: {v:.2f}s")

sc.get_regime = _oreg
senti.cached_sentiment = _os