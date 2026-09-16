# -*- coding: utf-8 -*-
"""K1（2026-09-16）：指数日K收盘增量更新 —— J4 冒烟用例。
调用真实的 update_indices + 真实的 datafeed.fetch_index_kline（monkeypatch 假 HTTP：
替换 datafeed._http 返回腾讯 fqkline 假 JSON），断言：写库行数、字段关系
H>=max(O,C) 且 L<=min(O,C)（_sane_rows 真实执行）、幂等 upsert、
index_daily_updated 审计事件（n_codes/max_date/backfilled）、
坏 bar 拒绝 + 失败路径记 ERROR。全程离线。
"""
import json
import sqlite3
import sys

import pytest

sys.stdout.reconfigure(encoding="utf-8")

# 三指数 × 3 根（09-11 已预置 / 09-14、09-15 为缺口日；腾讯 fqkline 6 字段
# [date, open, close, high, low, volume] —— F1 映射）
FAKE_QFQ = {}
for i, code in enumerate(("sh000001", "sz399001", "sz399006")):
    base = 3000.0 + i * 500
    arr = []
    for j, d in enumerate(("2026-09-11", "2026-09-14", "2026-09-15")):
        o = base + j * 10
        c = base + j * 10 + 5
        h = max(o, c) + 8
        l = min(o, c) - 6
        v = 3_000_000_000 + j * 100_000_000
        arr.append([d, f"{o:.2f}", f"{c:.2f}", f"{h:.2f}", f"{l:.2f}", f"{v}"])
    FAKE_QFQ[code] = arr


def _fake_http(url, decode="utf-8", ref=None, timeout=None):
    """假腾讯 fqkline 响应（离线）。坏 bar 由注入 key 控制。"""
    for code in FAKE_QFQ:
        if code in url:
            data = {"code": 0, "msg": "", "data": {code: {"qfqday": FAKE_QFQ[code]}}}
            return json.dumps(data, ensure_ascii=False)
    return json.dumps({"code": 0, "data": {}})


@pytest.fixture()
def k1_env(tmp_path, monkeypatch):
    """临时库（预置 09-11 旧行）+ 假 HTTP + 审计捕获。"""
    from app import datafeed as df
    from app import updater
    from app import audit as _audit_mod

    db_file = str(tmp_path / "k1_test.db")
    conn = sqlite3.connect(db_file)
    conn.execute("CREATE TABLE kline(code TEXT, period TEXT, date TEXT, "
                 "open REAL, high REAL, low REAL, close REAL, "
                 "volume REAL, amount REAL, "
                 "PRIMARY KEY(code, period, date))")
    for i, code in enumerate(("sh000001", "sz399001", "sz399006")):
        base = 3000.0 + i * 500
        conn.execute("INSERT OR REPLACE INTO kline VALUES(?,?,?,?,?,?,?,?,?)",
                     (code, "day", "2026-09-11", base, base + 8, base - 6,
                      base + 5, 3_000_000_000.0, 0.0))
    conn.commit()
    conn.close()

    calls = []
    monkeypatch.setattr(df, "_http", _fake_http)
    monkeypatch.setattr(updater, "C", type("X", (), {"DB_FILE": db_file})())
    monkeypatch.setattr(_audit_mod, "record", lambda **kw: calls.append(kw))
    return {"db": db_file, "calls": calls, "updater": updater}


def test_k1_write_rows_and_field_sanity(k1_env):
    """写库 9 行（3×3）；每行 H>=max(O,C) 且 L<=min(O,C)；回填 6 根缺口。"""
    upd = k1_env["updater"]
    r = upd.update_indices(verbose=False)
    assert r["updated"] == 3 and r["failed"] == 0
    assert r["max_date"] == "2026-09-15"
    assert r["backfilled"] == 6          # 每指数 09-14/09-15 两缺口 × 3 = 6

    conn = sqlite3.connect(k1_env["db"])
    rows = conn.execute("SELECT code,date,open,high,low,close,volume FROM kline "
                        "WHERE period='day' ORDER BY code,date").fetchall()
    conn.close()
    assert len(rows) == 9
    for code, d, o, h, l, c, v in rows:
        assert h >= max(o, c), f"{code} {d}: high<max(open,close)"
        assert l <= min(o, c), f"{code} {d}: low>min(open,close)"
        assert v >= 0
    assert {r_[0] for r_ in rows} == {"sh000001", "sz399001", "sz399006"}


def test_k1_idempotent_upsert(k1_env):
    """重跑一次：行数不变、backfilled 归零（幂等）。"""
    upd = k1_env["updater"]
    upd.update_indices(verbose=False)
    r2 = upd.update_indices(verbose=False)
    assert r2["updated"] == 3 and r2["failed"] == 0
    assert r2["backfilled"] == 0
    conn = sqlite3.connect(k1_env["db"])
    n = conn.execute("SELECT COUNT(*) FROM kline WHERE period='day'").fetchone()[0]
    conn.close()
    assert n == 9


def test_k1_audit_event(k1_env):
    """index_daily_updated 含 n_codes/max_date/backfilled。"""
    upd = k1_env["updater"]
    upd.update_indices(verbose=False)
    evs = [c for c in k1_env["calls"] if c.get("event") == "index_daily_updated"]
    assert len(evs) == 1
    assert evs[0]["level"] == "INFO"
    assert evs[0]["n_codes"] == 3
    assert evs[0]["max_date"] == "2026-09-15"
    assert evs[0]["backfilled"] == 6


def test_k1_failure_records_error(k1_env, monkeypatch):
    """取数为空 → failed=3 + audit ERROR（禁止静默）。"""
    from app import datafeed as df
    upd = k1_env["updater"]
    monkeypatch.setattr(df, "_http",
                        lambda *a, **kw: json.dumps({"code": 0, "data": {}}))
    r = upd.update_indices(verbose=False)
    assert r["failed"] == 3 and r["updated"] == 0
    errs = [c for c in k1_env["calls"] if c.get("event") == "index_daily_update_failed"]
    assert len(errs) >= 3
    assert all(c.get("level") == "ERROR" for c in errs)


def test_k1_sane_rows_rejects_bad_bar(k1_env, monkeypatch):
    """_sane_rows 拒绝 H<L 的坏 bar（真实解析链路，拒写库）。"""
    from app import datafeed as df
    upd = k1_env["updater"]
    orig = dict(FAKE_QFQ)

    def _bad_http(url, decode="utf-8", ref=None, timeout=None):
        for code in orig:
            if code in url:
                arr = list(orig[code])
                arr.append(["2026-09-16", "3100.0", "3105.0", "3090.0", "3110.0",
                            "3000000000"])   # high(3090) < low(3110) 坏 bar
                return json.dumps({"code": 0, "data": {code: {"qfqday": arr}}},
                                  ensure_ascii=False)
        return json.dumps({"code": 0, "data": {}})

    monkeypatch.setattr(df, "_http", _bad_http)
    r = upd.update_indices(verbose=False)
    conn = sqlite3.connect(k1_env["db"])
    n = conn.execute("SELECT COUNT(*) FROM kline WHERE period='day'").fetchone()[0]
    n_bad = conn.execute("SELECT COUNT(*) FROM kline WHERE date='2026-09-16'"
                         ).fetchone()[0]
    conn.close()
    assert r["updated"] == 3 and r["failed"] == 0
    assert n == 9                      # 3 指数各 3 根正常行
    assert n_bad == 0                  # H<L 坏 bar 被 _sane_rows 拒写
