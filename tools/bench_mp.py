# -*- coding: utf-8 -*-
"""实测多进程评分 vs 多线程评分（Windows spawn + pickle 传参成本全计入）。
评估后再施行：若进程收益 < 0.5s 或启动成本 > 收益，倾向评分结果缓存方案。"""
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor

from app import config as C
from app import datafeed as df
from app import scoring as sc


def _work(job):
    """顶层可 pickle 的 worker：纯计算（不传 code → 无网络/磁盘调用）"""
    klines, quote = job
    from app import scoring as _sc
    try:
        s, sig = _sc.score_stock(klines, quote)
        return s
    except Exception:
        return 0


def main():
    # 准备 130 份真实日K（磁盘缓存读取，模拟粗筛后的评分池）
    lst = df.get_stock_list()
    codes = [c for c, _, _ in lst[:400]]
    jobs = []
    for c in codes:
        k = df.fetch_kline(c, "day", 250)
        if len(k) >= 60:
            jobs.append((k, {}))
        if len(jobs) >= 130:
            break
    print(f"评分池: {len(jobs)} 只")

    # ---- 多线程 8（现状）----
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=C.PARALLEL_WORKERS) as ex:
        list(ex.map(_work, jobs))
    t1 = time.time()
    print(f"[线程8] 计算: {t1 - t0:.2f}s")

    # ---- pickle 打包成本 ----
    t0 = time.time()
    blob = [__import__("pickle").dumps(j) for j in jobs]
    t1 = time.time()
    print(f"[pickle] 130 份打包: {t1 - t0:.2f}s ({sum(len(b) for b in blob)/1e6:.1f}MB)")

    # ---- 多进程 4（创建成本单独计时）----
    t0 = time.time()
    ex = ProcessPoolExecutor(max_workers=4)
    t1 = time.time()
    print(f"[进程池] 创建4进程(含spawn import): {t1 - t0:.2f}s")

    t0 = time.time()
    list(ex.map(_work, jobs))
    t1 = time.time()
    print(f"[进程4] 计算(池已创建): {t1 - t0:.2f}s")
    ex.shutdown()


if __name__ == "__main__":
    main()