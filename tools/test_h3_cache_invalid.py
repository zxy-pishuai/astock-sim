# -*- coding: utf-8 -*-
"""H3 缓存失效验证（任务书判据 J2）：改动库内数据（测试副本）→ 指纹变化 → 缓存失效重算。
步骤：
  1. 复制快照 2026-09-11/market.db → tmp/h3/cachetest.db（副本可写）
  2. 副本原样 Backtest._cache_key() → key1；并跑一次（命中真库已有缓存 or 首写）
  3. 副本 kline 表插入 2 只新票的 1 根新 bar（MAX(date) 变、COUNT 变）
  4. 再算 _cache_key() → key2；断言 key1 != key2（指纹含新鲜度）
  5. 用副本跑 Backtest → 断言不命中旧缓存（新增缓存文件或结果含新数据）
"""
import json
import os
import shutil
import sqlite3
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

SRC_DB = os.path.join(BASE, "data", "snapshots", "2026-09-11", "market.db")
TST_DB = os.path.join(BASE, "tmp", "h3", "cachetest.db")


def main():
    os.makedirs(os.path.dirname(TST_DB), exist_ok=True)
    if os.path.exists(TST_DB):
        os.remove(TST_DB)
    shutil.copy2(SRC_DB, TST_DB)
    from app import config as C
    from app import engine as eng
    C.DB_FILE = TST_DB
    with open(os.path.join(C.DATA_DIR, "bt_pool.json"), encoding="utf-8") as f:
        pool = json.load(f)
    codes = list(pool["codes"])[:50]
    names = {c: c for c in codes}
    bt = eng.Backtest(codes, names, "2025-01-01", "2026-08-18", 100000.0, "score",
                      {"buy_threshold": 25, "max_positions": 3,
                       "position_pct": 0.3, "slippage": 0.001,
                       "zt_eco_gate": False, "dd_gate": False})
    key1 = bt._cache_key()
    r1 = bt.run()
    cdir = os.path.join(C.DATA_DIR, "bt_cache")
    before = set(os.listdir(cdir)) if os.path.isdir(cdir) else set()
    print("[T] key1=%s  run1: tr=%.4f trades=%d" % (key1, r1.get("total_return"), r1.get("trade_count")), flush=True)

    # 改副本：给 2 只票各插 1 根新 bar（新交易日 2026-08-19 → MAX(date) 与 COUNT 都变）
    conn = sqlite3.connect(TST_DB)
    c = conn.cursor()
    for code in codes[:2]:
        row = c.execute("SELECT MAX(date), close FROM kline WHERE code=? AND period='day'",
                        (code,)).fetchone()
        from datetime import datetime, timedelta
        nd = (datetime.strptime(row[0], "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        px = row[1] or 10.0
        c.execute("INSERT OR REPLACE INTO kline(code,date,open,high,low,close,volume,amount,period) "
                  "VALUES(?,?,?,?,?,?,?,?,'day')",
                  (code, nd, px, px, px, px, 1000000, 10000000))
    conn.commit()
    conn.close()
    print("[T] 副本已插入新 bar（2 票 @ MAX(date)+1）", flush=True)

    bt2 = eng.Backtest(codes, names, "2025-01-01", "2026-08-18", 100000.0, "score",
                       {"buy_threshold": 25, "max_positions": 3,
                        "position_pct": 0.3, "slippage": 0.001,
                        "zt_eco_gate": False, "dd_gate": False})
    key2 = bt2._cache_key()
    r2 = bt2.run()
    after = set(os.listdir(cdir)) if os.path.isdir(cdir) else set()
    new = after - before
    print("[T] key2=%s  key1!=key2 -> %s" % (key2, key1 != key2), flush=True)
    print("[T] 新缓存文件（失效重算）: %d %s" % (len(new), ",".join(sorted(new)[:2])), flush=True)
    print("[T] run2: tr=%.4f trades=%d" % (r2.get("total_return"), r2.get("trade_count")), flush=True)
    ok = (key1 != key2) and (len(new) >= 1)
    print("[T] RESULT %s" % ("PASS" if ok else "FAIL"), flush=True)
    sys.exit(0 if ok else 2)


if __name__ == "__main__":
    main()
