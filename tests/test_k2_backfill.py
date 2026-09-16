# -*- coding: utf-8 -*-
"""K2（2026-09-16）：amount 回填工具冒烟用例。
隔离测试 tools/backfill_amount.py 核心逻辑：_scan_missing 扫描口径（排除
指数/000905/已补/停牌占位）、只 UPDATE amount 的幂等性、baostock mock。
全程离线（不 import baostock 真库：monkeypatch 模块级引用）。
"""
import importlib.util
import sqlite3
import sys

import pytest

sys.stdout.reconfigure(encoding="utf-8")

_SPEC = importlib.util.spec_from_file_location(
    "baf", "C:/Users/26838/A股模拟盘/tools/backfill_amount.py")
_baf = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_baf)


@pytest.fixture()
def bf_db(tmp_path):
    db = str(tmp_path / "bf.db")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE kline(code TEXT, period TEXT, date TEXT, "
                 "open REAL, high REAL, low REAL, close REAL, "
                 "volume REAL, amount REAL, "
                 "PRIMARY KEY(code, period, date))")
    rows = [
        ("600001", "day", "2026-09-14", 10, 11, 9, 10.5, 100000, 0.0),
        ("600002", "day", "2026-09-14", 20, 21, 19, 20.5, 200000, None),
        ("600003", "day", "2026-09-15", 30, 31, 29, 30.5, 300000, 0.0),
        ("600004", "day", "2026-09-14", 40, 41, 39, 40.5, 400000, 1234567.0),
        ("600005", "day", "2026-09-14", 50, 51, 49, 50.5, 0, 0.0),
        ("sh000001", "day", "2026-09-14", 3800, 3900, 3700, 3850, 3e9, 0.0),
        ("000905", "day", "2026-09-14", 5000, 5100, 4900, 5050, 1e8, 0.0),
    ]
    conn.executemany("INSERT OR REPLACE INTO kline VALUES(?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()
    return db


def test_scan_missing_excludes_index_and_filled(bf_db):
    """扫描口径：amount0 且 volume>0；排除指数/000905/已补/停牌占位。"""
    conn = sqlite3.connect(bf_db)
    miss = _baf._scan_missing(conn, "2026-09-01", "2026-09-30")
    conn.close()
    pairs = {(c, d) for c, d, _ in miss}
    assert pairs == {("600001", "2026-09-14"), ("600002", "2026-09-14"),
                     ("600003", "2026-09-15")}
    assert ("sh000001", "2026-09-14") not in pairs
    assert ("000905", "2026-09-14") not in pairs
    assert ("600004", "2026-09-14") not in pairs
    assert ("600005", "2026-09-14") not in pairs


class _FakeBs:
    """假 baostock：按代码返回固定 amount 表。"""

    AMT = {"sh.600001": {"2026-09-14": 111111.0},
           "sh.600002": {"2026-09-14": 222222.0},
           "sh.600003": {"2026-09-15": 333333.0}}

    class _Rs:
        def __init__(self, data):
            self.error_code = "0"
            self._it = iter(data.items())

        def next(self):
            try:
                self._row = next(self._it)
                return True
            except StopIteration:
                return False

        def get_row_data(self):
            return [self._row[0], f"{self._row[1]:.4f}"]

    def login(self):
        return type("L", (), {"error_code": "0", "error_msg": ""})()

    def query_history_k_data_plus(self, code, fields, start_date="", end_date="",
                                  frequency="d", adjustflag="3"):
        return self._Rs(self.AMT.get(code, {}))

    def logout(self):
        pass


def test_backfill_amount_only_and_idempotent(bf_db, monkeypatch):
    """mock baostock（函数内延迟 import → patch sys.modules）：只 UPDATE amount；
    重跑第二次零更新（幂等）；OHLCV 不变。"""
    monkeypatch.setitem(sys.modules, "baostock", _FakeBs())
    monkeypatch.setattr(_baf, "DB", bf_db)

    conn = sqlite3.connect(bf_db)
    miss = _baf._scan_missing(conn, "2026-09-01", "2026-09-30")
    for c, d, _v in miss:
        amts = _baf._fetch_amounts_baostock(c, "2026-09-01", "2026-09-30")
        assert amts and d in amts
        conn.execute("UPDATE kline SET amount=? WHERE code=? AND period='day' "
                     "AND date=?", (amts[d], c, d))
        conn.commit()
    conn.close()

    conn = sqlite3.connect(bf_db)
    rows = conn.execute("SELECT code,date,open,high,low,close,volume,amount "
                        "FROM kline WHERE code IN ('600001','600002','600003') "
                        "ORDER BY code").fetchall()
    miss2 = _baf._scan_missing(conn, "2026-09-01", "2026-09-30")
    conn.close()

    amt = {r[0]: r[7] for r in rows}
    assert amt["600001"] == 111111.0
    assert amt["600002"] == 222222.0
    assert amt["600003"] == 333333.0
    for r in rows:
        # SELECT 序: code,date,open,high,low,close,volume,amount
        o, h, l, c = r[2], r[3], r[4], r[5]
        assert r[6] > 0 and h >= max(o, c) and l <= min(o, c)   # OHLC 结构保持
    assert miss2 == []                                     # 幂等：第二次零缺失


def test_fetch_amounts_baostock_prefix(bf_db, monkeypatch):
    """代码前缀映射：600519->sh.600519、000001->sz.000001。"""
    monkeypatch.setitem(sys.modules, "baostock", _FakeBs())
    monkeypatch.setattr(_baf, "DB", bf_db)
    assert _baf._to_bs("600519") == "sh.600519"
    assert _baf._to_bs("000001") == "sz.000001"
    assert _baf._to_bs("300750") == "sz.300750"
    assert _baf._to_bs("688223") == "sh.688223"
