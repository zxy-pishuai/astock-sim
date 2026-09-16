# -*- coding: utf-8 -*-
"""F2（2026-09-13）常驻线程池收敛压测（10 分钟）。

验收判据（预注册）：
A. 压测结束时 threading.enumerate() 中 ThreadPoolExecutor-* 线程数 <= 常驻池 worker 总数 + 5
   （改动前会随请求数线性增长——418 个池的根因）。
B. 池编号 ThreadPoolExecutor-<N>_ 在整个压测期间 N 不增长（改动前每次调用 +1）。
C. stderr 中 "cannot schedule new futures" 出现次数 = 0。
D. 功能回归：fetch_quotes 输出逐字段一致（由 tools/test_f1_singleflight.py 判据3 另行验证；
   sentiment_series / multistrategy 未改（道级红线），结构天然一致，此处仅验证 import 可用）。

运行：python tools/test_f2_pool_churn.py [分钟数，默认 10]
"""
import io
import sys
import threading
import time

sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(
    __import__("os").path.abspath(__file__))))

from app import datafeed as df
from app import pools
from app.trader import _ResilientPool


class _StderrCap:
    """线程安全 stderr 包装：计数 cannot schedule 出现次数。"""
    def __init__(self):
        self.n = 0
        self.lock = threading.Lock()

    def write(self, s):
        if "cannot schedule new futures" in s:
            with self.lock:
                self.n += 1
        sys.__stderr__.write(s)

    def flush(self):
        sys.__stderr__.flush()


def _noop(x):
    return x


def _round(sel):
    """模拟一轮混合负载：交易/通用取数 + 4 个 trader 池提交。"""
    try:
        df.fetch_quotes_trading(CODES[:10], enrich=False)
    except Exception:
        pass
    try:
        df.fetch_quotes(CODES[10:20], enrich=True)
    except Exception:
        pass
    for name, n in (("scan", 6), ("board", 4), ("mf", 3), ("twothirty", 3)):
        try:
            with _ResilientPool(name, n) as ex:
                if pools.is_shutting_down():
                    return
                futs = [ex.submit(_noop, i) for i in range(5)]
                for f in futs:
                    if f is None:
                        continue
                    f.result()
        except Exception:
            pass


CODES = ["600000", "000001", "600519", "000858", "601318", "600036", "000002",
         "601899", "600900", "000333", "601166", "600030", "000651", "601988",
         "600028", "000725", "601288", "600887", "000063", "600276"]


def main():
    minutes = float(sys.argv[1]) if len(sys.argv) > 1 else 10.0
    cap = _StderrCap()
    sys.stderr = cap

    t0 = time.time()
    end = t0 + minutes * 60
    pool_epochs = set()          # 压测期间见过的 ThreadPoolExecutor-<N> 编号
    rounds = 0
    while time.time() < end:
        _round(rounds)
        for t in threading.enumerate():
            nm = t.name
            if nm.startswith("ThreadPoolExecutor-"):
                try:
                    epoch = int(nm.split("-")[1].split("_")[0])
                    pool_epochs.add(epoch)
                except Exception:
                    pass
        rounds += 1
        time.sleep(1.2)

    # 统计
    live = [t.name for t in threading.enumerate()]
    tpe_threads = [n for n in live if n.startswith("ThreadPoolExecutor-")]
    pool_threads = [n for n in live
                    if any(n.startswith(p) for p in
                           ("scan_", "board_", "mf_", "twothirty_", "trading_",
                            "display_", "http_", "sina_", "enrich_",
                            "all_stocks_", "tdx_"))]
    reg = list(pools._POOLS.keys())
    total_workers = sum(p._max_workers for p in pools._POOLS.values()) \
        if hasattr(next(iter(pools._POOLS.values()), None), "_max_workers") \
        else "?"   # ThreadPoolExecutor 无公开 _max_workers，退化为 None
    # 手工统计 worker 上限（从 get_pool 调用处已知：6+2+4+4+4+16+4+4+4+4+6）
    known = {"trading": 6, "display": 2, "http": 4, "sina": 4, "enrich": 4,
             "all_stocks": 16, "scan": 4, "board": 4, "mf": 4, "twothirty": 4,
             "tdx": 6}
    total_known = sum(known.get(k, 0) for k in reg)
    print("=== F2 压测结果（%.1f 分钟，%d 轮）===" % (minutes, rounds))
    print("常驻池注册表:", sorted(reg))
    print("常驻池 worker 上限合计:", total_known)
    print("压测期间出现过的 ThreadPoolExecutor-<N> 编号数: %d（%s）"
          % (len(pool_epochs), sorted(pool_epochs)[:10]))
    print("压测结束 ThreadPoolExecutor-* 存活线程数: %d" % len(tpe_threads))
    print("  判定A: ThreadPoolExecutor-* <= 常驻 worker 总数+5 ?",
          "通过 ✓" if len(tpe_threads) <= total_known + 5 else "不通过 ✗")
    print("  判定B: 池编号 N 不增长（应为 0 或极小）?",
          "通过 ✓" if len(pool_epochs) <= 1 else "不通过 ✗（%d 个编号）" % len(pool_epochs))
    print("stderr 'cannot schedule new futures' 次数:", cap.n)
    print("  判定C: = 0 ?", "通过 ✓" if cap.n == 0 else "不通过 ✗")
    print("常驻池线程（存活）:", sorted(set(pool_threads))[:25])
    # 判定D：fetch_quotes 结构抽查
    r = df.fetch_quotes(CODES[:5], force=True)
    ok = all(q.get("price") and q.get("name") for q in r.values())
    print("  判定D: fetch_quotes 结构抽查（price+name）:", "通过 ✓" if ok else "不通过 ✗")
    sys.exit(0 if (len(tpe_threads) <= total_known + 5 and len(pool_epochs) <= 1
                   and cap.n == 0 and ok) else 1)


if __name__ == "__main__":
    main()
