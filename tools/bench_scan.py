# -*- coding: utf-8 -*-
"""安全端到端 scan_once 计时：拦截真实买入（check_buy 恒拒绝），
对比 TDX 开/关 + moneyflow 熔断效果。"""
import sys
import time

from app import config as C
from app import risk as rk
from app import tdx as T
from app.trader import TradingEngine

TDX_ON = "--tdx" not in sys.argv or sys.argv[sys.argv.index("--tdx") + 1] != "off"


def main():
    # 拦截买入：风控恒拒绝（只读压力测试）
    _orig_check = rk.engine.check_buy

    def _no_buy(*a, **k):
        return False, "benchmark-no-buy"

    rk.engine.check_buy = _no_buy
    if not TDX_ON:
        T.available = lambda: False

    # 强制可开仓模式（max_pos=6），确保走完整 400 只评分循环
    from app import scoring as sc
    from app import sentiment as _senti
    _orig_regime = sc.get_regime
    _orig_senti = _senti.cached_sentiment

    def _force_regime(breadth):
        r, mp, th, pp = _orig_regime(breadth)
        return r, max(mp, 6), th, pp

    def _force_senti(*a, **k):
        return {"open": True, "phase": "高潮", "score": 80, "max_days": 5, "yesterday": {}}

    sc.get_regime = _force_regime
    _senti.cached_sentiment = _force_senti

    eng = TradingEngine()
    # 清行情缓存（模拟全新一轮）
    from app import datafeed as df
    with df._lock:
        df._mem_quotes.clear()

    t0 = time.time()
    eng.scan_once(verbose=False)
    t1 = time.time()
    rk.engine.check_buy = _orig_check
    sc.get_regime = _orig_regime
    _senti.cached_sentiment = _orig_senti

    mode = "TDX" if TDX_ON else "HTTP"
    print(f"[{mode}] scan_once 完整耗时: {t1 - t0:.2f}s")
    print(f"  msg: {eng.last_scan_msg}")
    print(f"  候选数: {len(eng.last_scan)}")


if __name__ == "__main__":
    main()