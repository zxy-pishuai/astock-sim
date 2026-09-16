# -*- coding: utf-8 -*-
"""F2（2026-09-13）进程级常驻线程池注册表。

背景：全项目 14 处 ThreadPoolExecutor(...) 每次调用新建销毁（仅 tdx.py:27 常驻），
09-11 停摆快照出现 ThreadPoolExecutor-417/418（单进程已建 418 个池）；profile 显示
_ssl.enum_certificates / load_verify_locations 68 次——池重建导致 SSL 上下文反复初始化。

本模块提供：
- get_pool(name, max_workers)：同名返回**同一个**常驻池实例（线程前缀 thread_name_prefix=name，
  便于日后看栈定位线程归属）。
- shutdown_all()：服务优雅关停时统一回收（atexit 自动注册）。
- _SHUTTING_DOWN 全局标志 + is_shutting_down()：解释器进入收尾后置位，所有循环在提交任务前
  检查，为真则静默退出循环——根治 "cannot schedule new futures after interpreter shutdown"
  （实测累计 208 条：高频监控 119 / 主循环 54 / 打板 27 / 后台扫描 8）。
"""
import atexit
import threading
from concurrent.futures import ThreadPoolExecutor

_POOLS = {}
_POOLS_LOCK = threading.Lock()
_SHUTTING_DOWN = False


def is_shutting_down():
    """解释器是否进入收尾（atexit 置位）。所有循环提交任务前检查，为真静默退出。"""
    return _SHUTTING_DOWN


def _set_shutting_down():
    global _SHUTTING_DOWN
    _SHUTTING_DOWN = True


def get_pool(name, max_workers):
    """返回同名常驻池实例（不存在则创建）。shutdown_all 后再次调用会重建
    （供 _ResilientPool 的"重建重试一次"语义复用）。"""
    with _POOLS_LOCK:
        p = _POOLS.get(name)
        if p is None:
            p = ThreadPoolExecutor(max_workers=max_workers,
                                   thread_name_prefix=name)
            _POOLS[name] = p
        return p


def shutdown_all():
    """优雅关停全部常驻池（服务退出时调用）。调用后提交会抛 RuntimeError，
    由各循环 try/except 与 _ResilientPool 兜底。"""
    with _POOLS_LOCK:
        pools = list(_POOLS.values())
        _POOLS.clear()
    for p in pools:
        try:
            p.shutdown(wait=False)
        except Exception:
            pass


# atexit 为 LIFO：后注册先执行 → 先置 _SHUTTING_DOWN 标志，再关池
atexit.register(shutdown_all)
atexit.register(_set_shutting_down)
