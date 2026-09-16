# -*- coding: utf-8 -*-
"""★ J3（2026-09-13）：统一 SQLite 连接工厂。

背景（实测）：market.db 1.84GB / min5.db 0.74GB，全项目 13+ 处 sqlite3.connect
无一处设置 mmap_size / cache_size；datafeed._conn 每次调用重发 PRAGMA journal_mode=WAL
（写属性操作取锁）；earnings._conn 每次跑 DDL。

本模块：
  - open_ro()/open_rw()：集中 PRAGMA（mmap_size 512MB / cache_size 128MB /
    temp_store=MEMORY；读连接 mode=ro URI + query_only=ON；写连接 synchronous=NORMAL
    + busy_timeout 分级：读 5s / 写 30s）
  - journal_mode=WAL 只初始化一次（进程内 set 去重），不在每次连接时执行
  - threading.local 连接复用：消除"每查询新建连接"开销（计数见 _CONNECT_COUNT，
    验收判据：每线程 1 次而非每查询 1 次）
"""
import sqlite3
import threading
from pathlib import Path

# ---- 常量 ----
MMAP_SIZE = 536870912      # 512MB（64 位安全；读文件映射免用户态拷贝）
CACHE_SIZE = -131072       # 128MB 页缓存（负值 = KiB；对 1.84GB 库顺序扫描显著受益）
READ_BUSY = 5000           # 读连接 busy_timeout 5s
WRITE_BUSY = 30000         # 写连接 busy_timeout 30s

_READ_PRAGMAS = {
    "mmap_size": MMAP_SIZE,
    "cache_size": CACHE_SIZE,
    "temp_store": 2,       # MEMORY
    "query_only": 1,       # 只读双保险（配合 mode=ro URI）
}
_WRITE_PRAGMAS = {
    "mmap_size": MMAP_SIZE,
    "cache_size": CACHE_SIZE,
    "temp_store": 2,
    "synchronous": 1,      # NORMAL（WAL 下崩溃安全，fsync 只发生在 checkpoint）
}

_local = threading.local()
_init_lock = threading.Lock()
_init_done = set()         # path -> WAL 已初始化（进程内一次）
_CONNECT_COUNT = {"n": 0}  # sqlite3.connect 实际调用计数（验收用）
_COUNT_LOCK = threading.Lock()


def _uri(path):
    """绝对路径 → file:// URI（中文路径安全）。"""
    return Path(path).as_uri()


def _bump_count():
    with _COUNT_LOCK:
        _CONNECT_COUNT["n"] += 1


def _init_journal(path):
    """PRAGMA journal_mode=WAL 只在首次连接该库时执行一次（写属性操作会取锁，
    每次连接重发是浪费）。"""
    with _init_lock:
        if path in _init_done:
            return
        con = sqlite3.connect(path, timeout=WRITE_BUSY)
        try:
            con.execute("PRAGMA journal_mode=WAL")
        finally:
            con.close()
        _init_done.add(path)


def open_ro(path, busy_timeout=READ_BUSY):
    """只读连接（mode=ro URI + query_only=ON），threading.local 复用。

    复用连接由本模块持有，调用方无需（也不应）close——线程退出时随
    threading.local 一起回收；进程长跑期内连接常驻（复用是目标）。
    """
    _init_journal(path)
    cons = getattr(_local, "cons", None)
    if cons is None:
        cons = _local.cons = {}
    con = cons.get(("ro", path))
    if con is None:
        _bump_count()
        con = sqlite3.connect("%s?mode=ro" % _uri(path), uri=True,
                              timeout=busy_timeout)
        for k, v in _READ_PRAGMAS.items():
            con.execute("PRAGMA %s=%s" % (k, v))
        cons[("ro", path)] = con
    else:
        # 调用方若 close 过复用连接（改造未删干净的历史路径），下次自动重建——
        # 语义退化为"每调用新建"（正确），而非返回 closed 连接（崩溃）。
        try:
            con.execute("SELECT 1")
        except sqlite3.Error:
            _bump_count()
            con = sqlite3.connect("%s?mode=ro" % _uri(path), uri=True,
                                  timeout=busy_timeout)
            for k, v in _READ_PRAGMAS.items():
                con.execute("PRAGMA %s=%s" % (k, v))
            cons[("ro", path)] = con
    return con


def open_rw(path, busy_timeout=WRITE_BUSY):
    """读写连接（synchronous=NORMAL + busy_timeout 30s）。

    写连接每次新建（不缓存）：写路径（updater/earnings 等）低频，且保持
    "写后 close" 语义——复用缓存会把未 commit 的事务悬挂到下次调用。
    PRAGMA 集中应用（WAL 初始化只做一次）。
    """
    _init_journal(path)
    _bump_count()
    con = sqlite3.connect(path, timeout=busy_timeout)
    for k, v in _WRITE_PRAGMAS.items():
        con.execute("PRAGMA %s=%s" % (k, v))
    return con


def connect_count():
    """验收用：已发生的 sqlite3.connect 调用次数（应为 线程数 × 库数 × 2，而非查询数）。"""
    return _CONNECT_COUNT["n"]
