# -*- coding: utf-8 -*-
"""J4（2026-09-13）：engine 冒烟——迷你回测（5 票 × 120 日，临时只读库）。
全程离线：patch datafeed.fetch_quotes 为空、C.DB_FILE 指向临时库。
"""
import os
import sqlite3
import sys
from datetime import date, timedelta

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import pytest

from app import engine as eng

CODES = ["sh600000", "sz000001", "sz300750", "sh601318", "sz000002"]
N = 120
START = "2026-04-01"
END = "2026-08-31"


def _make_db(path):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE kline(code TEXT, period TEXT, date TEXT, "
                 "open REAL, high REAL, low REAL, close REAL, "
                 "volume REAL, amount REAL)")
    conn.execute("CREATE INDEX idx_kline_cp ON kline(code, period)")
    d0 = date(2026, 3, 1)
    rows = []
    for i, code in enumerate(CODES):
        base = 10.0 + i
        for j in range(N):
            d = d0 + timedelta(days=j)
            if d.weekday() >= 5:
                continue  # 跳过周末
            c = base * (1 + 0.001 * j)
            o = c * 0.998
            h = max(o, c) * 1.01
            l = min(o, c) * 0.99
            v = 2_000_000.0 + j * 1000
            rows.append((code, "day", d.isoformat(), o, h, l, c, v, v * c))
    conn.executemany("INSERT INTO kline VALUES (?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()


def test_mini_backtest_runs(tmp_path, monkeypatch):
    db = tmp_path / "engine_smoke.db"
    _make_db(str(db))
    # 切数据源到临时库 + 禁网（fetch_quotes 置空 → engine 走 amount*20 降级）
    monkeypatch.setattr("app.datafeed.fetch_quotes", lambda *a, **k: {})
    import app.config as C

    orig_db = C.DB_FILE
    orig_min5 = C.MIN5_DB_FILE
    C.DB_FILE = str(db)
    C.MIN5_DB_FILE = str(db)
    try:
        bt = eng.Backtest(CODES, {}, START, END, 1_000_000.0, "score",
                          {"buy_threshold": 20, "seed": 42})
        r = bt.run()
    finally:
        C.DB_FILE = orig_db
        C.MIN5_DB_FILE = orig_min5
    assert r is not None
    for key in ("total_return", "trade_count"):
        assert r.get(key) is not None, "回测结果缺 %s: %r" % (key, r)
    assert isinstance(r["trade_count"], int) and r["trade_count"] >= 0
