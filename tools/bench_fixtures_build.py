# -*- coding: utf-8 -*-
"""H4 基准集生成：从快照库只读导出 3 只标准股票 × 固定 1250 根日K → data/bench_fixtures.npz。
离线基准（bench_score/bench_backtest 均不再碰行情源）。可重复跑（幂等重建）。
选票：600000（大盘/主板）、002001（中小盘）、300750（创业板/深市）。
"""
import json
import os
import sqlite3
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import numpy as np

SNAP_DB = os.path.join(BASE, "data", "snapshots", "2026-09-11", "market.db")
OUT = os.path.join(BASE, "data", "bench_fixtures.npz")
CODES = [("600000", "浦发银行（大盘/主板）"),
         ("002001", "新和成（中小盘/深市）"),
         ("300750", "宁德时代（创业板/深市）")]
N_BARS = 1250


def main():
    assert os.path.exists(SNAP_DB), "快照库不存在: %s" % SNAP_DB
    dtype = np.dtype([("date", "U10"), ("open", "f8"), ("high", "f8"),
                      ("low", "f8"), ("close", "f8"), ("volume", "f8"),
                      ("amount", "f8")])
    conn = sqlite3.connect("file:%s?mode=ro&immutable=1" % SNAP_DB, uri=True, timeout=30)
    arrs, meta = {}, {}
    t0 = time.time()
    for code, label in CODES:
        rows = conn.execute(
            "SELECT date,open,high,low,close,volume,amount FROM kline "
            "WHERE code=? AND period='day' ORDER BY date DESC LIMIT ?",
            (code, N_BARS)).fetchall()
        rows = rows[::-1]  # 升序
        if len(rows) < N_BARS:
            print("[WARN] %s 只有 %d 根（< %d），用全部" % (code, len(rows), N_BARS))
        a = np.zeros(len(rows), dtype=dtype)
        for i, r in enumerate(rows):
            a[i] = (r[0], r[1] or 0.0, r[2] or 0.0, r[3] or 0.0,
                    r[4] or 0.0, r[5] or 0.0, r[6] or 0.0)
        arrs[code] = a
        meta[code] = {"label": label, "bars": len(rows),
                      "first": rows[0][0], "last": rows[-1][0]}
        print("[%s] %s bars=%d %s~%s" % (code, label, len(rows), rows[0][0], rows[-1][0]))
    conn.close()
    meta_arr = np.array([(c, meta[c]["label"], meta[c]["bars"]) for c, _ in CODES],
                        dtype=[("code", "U10"), ("label", "U40"), ("bars", "i8")])
    np.savez(OUT, **arrs, meta=meta_arr)
    print("[H4] fixtures saved %s (%.2fs)" % (OUT, time.time() - t0))
    print("[H4] meta: %s" % json.dumps(meta, ensure_ascii=False))


if __name__ == "__main__":
    main()
