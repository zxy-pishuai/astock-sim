# -*- coding: utf-8 -*-
"""v3.7 分库迁移脚本：kline_min5 → min5.db，VACUUM 瘦身 market.db"""
import sqlite3
import sys
import io
import os
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

BASE = r"C:\Users\26838\A股模拟盘\data"
market = os.path.join(BASE, "market.db")
min5db = os.path.join(BASE, "min5.db")

t0 = time.time()
mc = sqlite3.connect(market, timeout=120)
tables = [r[0] for r in mc.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
print("market.db 表:", tables)
sz_before = os.path.getsize(market)
print("market.db 大小(MB): %.1f" % (sz_before / 1024 / 1024))

if "kline_min5" in tables:
    cnt = mc.execute("SELECT COUNT(*) FROM kline_min5").fetchone()[0]
    print("待迁移 kline_min5 行数:", cnt)
    nc = sqlite3.connect(min5db, timeout=120)
    nc.execute("PRAGMA journal_mode=WAL")
    nc.execute("""CREATE TABLE IF NOT EXISTS kline_min5(
        code TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL,
        PRIMARY KEY(code, date))""")
    # nc.execute("CREATE INDEX IF NOT EXISTS idx_kmin5_c ON kline_min5(code)")
    if nc.execute("SELECT COUNT(*) FROM kline_min5").fetchone()[0] == 0:
        # 分块流式迁移（避免同库双连接锁）
        batch = 200000
        offset = 0
        while True:
            rows = mc.execute(
                "SELECT code,date,open,high,low,close,volume FROM kline_min5 "
                "ORDER BY code,date LIMIT ? OFFSET ?", (batch, offset)).fetchall()
            if not rows:
                break
            nc.executemany(
                "INSERT OR REPLACE INTO kline_min5(code,date,open,high,low,close,volume) "
                "VALUES(?,?,?,?,?,?,?)", rows)
            nc.commit()
            offset += batch
            print("  迁移进度: %d / %d (%.0f%%)" % (offset, cnt, offset / cnt * 100))
        print("min5.db 迁移后行数:", nc.execute("SELECT COUNT(*) FROM kline_min5").fetchone()[0])
    nc.close()
    mc.execute("DROP TABLE IF EXISTS kline_min5")
    mc.commit()
    print("已从 market.db 删除 kline_min5 表")
else:
    print("market.db 无 kline_min5 表（可能已迁移）")

mc.execute("VACUUM")
mc.close()
sz_after = os.path.getsize(market)
print("VACUUM 后 market.db (MB): %.1f -> %.1f (节省 %.1f MB)" % (
    sz_before / 1024 / 1024, sz_after / 1024 / 1024, (sz_before - sz_after) / 1024 / 1024))
print("min5.db 大小(MB): %.1f" % (os.path.getsize(min5db) / 1024 / 1024))
print("完成，耗时 %.0fs" % (time.time() - t0))
