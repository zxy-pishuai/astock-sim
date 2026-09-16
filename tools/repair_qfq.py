# -*- coding: utf-8 -*-
"""★ qfq 前复权缺陷修复 v4：mootdx 原始日K + 通达信 xdxr + 纯Python自实现前复权
数据源（本地可靠，已实测 300750/002837/300015 2019起跳变=0、现价与库一致）：
  - mootdx Quotes.bars 分页拉不复权原史
  - mootdx get_xdxr 通达信除权除息表
  - 自实现 baoli 前复权（NaN→0，纯标准库）
对缺陷清单每只票全量重写（INSERT OR REPLACE 只动 day 期），统一复权基准，
消除缺陷 + 修复此前腾讯源写入的污染。
amount: 库内保留原值；缺失用 volume×(OHLC均)/4 估算
断点续跑/重试2次/限速0.1s
用法: python tools/repair_qfq.py --list data/qfq_defect_2019.json [--codes] [--dry]
"""
import argparse
import json
import os
import sqlite3
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DB = os.path.join(BASE, "data", "market.db")
PROGRESS = os.path.join(BASE, "data", "qfq_repair_progress.json")
FAIL = os.path.join(BASE, "data", "qfq_repair_failed.json")
_SLEEP = 0.1

_tls = None


def fetch_raw(code):
    """mootdx 不复权日K分页全史。返回 [{date,open,close,high,low,vol,amount}] 升序"""
    from mootdx.quotes import Quotes
    q = Quotes.factory(market="std")
    out = []
    start = 0
    while True:
        df = q.bars(symbol=code, frequency=9, start=start, offset=800)
        if df is None or len(df) == 0:
            break
        rows = []
        for i in range(len(df)):
            r = df.iloc[i]
            try:
                rows.append((str(r["datetime"])[:10], float(r["open"]), float(r["close"]),
                             float(r["high"]), float(r["low"]), float(r.get("vol", 0) or 0),
                             float(r.get("amount", 0) or 0)))
            except Exception:
                continue
        if not rows:
            break
        out = rows + out
        if len(rows) < 800:
            break
        start += 800
        time.sleep(_SLEEP)
    return out


def make_qfq(rows, xdxr):
    """纯Python前复权（baoli 算法, NaN→0）。返回新 [{...}]"""
    evs = []
    for i in range(len(xdxr)):
        try:
            if int(xdxr.iloc[i].get("category", 1) or 1) != 1:
                continue
            d = str(xdxr.index[i])[:10]
            fh = float(xdxr.iloc[i].get("fenhong") or 0)
            pg = float(xdxr.iloc[i].get("peigu") or 0)
            pgj = float(xdxr.iloc[i].get("peigujia") or 0)
            szg = float(xdxr.iloc[i].get("songzhuangu") or 0)
            if fh or pg or szg:
                evs.append((d, fh, pg, pgj, szg))
        except Exception:
            continue
    evs.sort(key=lambda e: e[0])
    dates = [r[0] for r in rows]
    ohlc = {r[0]: [r[1], r[2], r[3], r[4]] for r in rows}
    for d, fh, pg, pgj, szg in evs:
        denom = 10 + pg + szg
        if denom <= 0:
            continue
        for dd in dates:
            if dd < d:
                cur = ohlc[dd]
                ohlc[dd] = [(cur[0] * 10 - fh + pg * pgj) / denom,
                            (cur[1] * 10 - fh + pg * pgj) / denom,
                            (cur[2] * 10 - fh + pg * pgj) / denom,
                            (cur[3] * 10 - fh + pg * pgj) / denom]
    out = []
    for r in rows:
        o, c, h, l = ohlc[r[0]]
        out.append({"date": r[0], "open": o, "close": c, "high": h,
                    "low": l, "vol": r[5], "amount": r[6]})
    return out


def _ro_count():
    conn = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=10)
    try:
        return conn.execute("SELECT COUNT(*) FROM kline WHERE period='day'").fetchone()[0]
    finally:
        conn.close()


def repair_one(code):
    """拉取+复权+写库。返回 (ok, msg, replaced, gaps_2019)"""
    from mootdx.utils.adjust import get_xdxr
    raw = fetch_raw(code)
    if len(raw) < 10:
        return False, "mootdx 无数据(%d)" % len(raw), 0, 0
    xdxr = get_xdxr(code)
    if xdxr is None or len(xdxr) == 0:
        return False, "无xdxr除权信息", 0, 0
    adj = make_qfq(raw, xdxr)
    if len(adj) < 10:
        return False, "复权输出异常", 0, 0
    # 2019 起连续性校验（剔除上市前5交易日：简单起见从第6个交易日开始统计）
    gaps = 0
    prev = None
    n = 0
    for r in adj:
        if r["date"] < "2019-01-01":
            prev = r
            continue
        n += 1
        if n > 5 and prev and prev["close"] and prev["close"] > 0:
            if abs((r["open"] - prev["close"]) / prev["close"]) > 0.21:
                gaps += 1
        prev = r
    # ★ v4.1 修复: 复权失败(仍有2019跳变) → 不写库, 记例外（避免把负价/异常复权写入）
    if gaps > 0:
        return False, "仍有%d处2019跳变(跳过不写库)" % gaps, 0, gaps
    # 写库（全量 INSERT OR REPLACE 覆盖）
    amap = {}
    try:
        ro = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=15)
        try:
            amap = {r[0]: r[1] for r in ro.execute(
                "SELECT date, amount FROM kline WHERE code=? AND period='day'", (code,)).fetchall()}
        finally:
            ro.close()
    except Exception:
        pass
    w = sqlite3.connect(DB, timeout=60)
    w.execute("PRAGMA journal_mode=WAL")
    amt_map = {}
    for r in adj:
        amt = amap.get(r["date"])
        if amt is None or amt <= 0:
            amt = r["vol"] * 100 * (r["open"] + r["close"] + r["high"] + r["low"]) / 4
        amt_map[r["date"]] = amt
    try:
        w.executemany(
            "INSERT OR REPLACE INTO kline(code,period,date,open,high,low,close,volume,amount) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            [(code, "day", r["date"], r["open"], r["high"], r["low"], r["close"],
              r["vol"] * 100, amt_map[r["date"]]) for r in adj])
        w.commit()
    finally:
        w.close()
    ok = gaps == 0
    return ok, ("修复成功" if ok else "仍有%d处2019跳变" % gaps), len(adj), gaps


def run(list_path=None, only_codes=None, dry=False):
    t0 = time.time()
    if not list_path:
        list_path = os.path.join(BASE, "data", "qfq_defect_2019.json")
    data = json.load(open(list_path, encoding="utf-8"))
    by_code = data.get("by_code") or {}
    if only_codes:
        by_code = {c: evs for c, evs in by_code.items() if c in only_codes}
    codes = sorted(by_code.keys())
    print("待修复票数:", len(codes), "(dry=%s)" % dry, flush=True)
    if not codes:
        return
    done = {}
    if os.path.exists(PROGRESS):
        try:
            with open(PROGRESS, "r", encoding="utf-8") as f:
                done = json.load(f)
        except Exception:
            done = {}
    todo = [c for c in codes if c not in done]
    print("已完成 %d，续跑 %d" % (len(done), len(todo)), flush=True)
    if dry:
        todo = todo[:3]
    fails = []
    stats = {"replaced": 0, "fixed": 0, "unfixed": 0}
    ro_before = _ro_count()
    for i, code in enumerate(todo):
        ok = False
        msg = ""
        for attempt in range(3):
            try:
                if dry:
                    raw = fetch_raw(code)
                    ok = len(raw) > 10
                    msg = "dry: mootdx %d 根" % len(raw)
                    stats["replaced"] += len(raw)
                    break
                ok, msg, replaced, gaps = repair_one(code)
                stats["replaced"] += replaced
                break
            except Exception as e:
                msg = str(e)
                time.sleep(1.0 * (attempt + 1))
        if not ok:
            fails.append({"code": code, "error": msg})
            stats["unfixed"] += 1
            done[code] = "跳过:" + msg[:50]
        else:
            stats["fixed"] += 1
            done[code] = msg[:50]
        if (i + 1) % 15 == 0:
            print("  %d/%d (fixed=%d unfixed=%d)" % (
                i + 1, len(todo), stats["fixed"], stats["unfixed"]), flush=True)
            with open(PROGRESS, "w", encoding="utf-8") as f:
                json.dump(done, f, ensure_ascii=False)
    with open(PROGRESS, "w", encoding="utf-8") as f:
        json.dump(done, f, ensure_ascii=False)
    with open(FAIL, "w", encoding="utf-8") as f:
        json.dump(fails, f, ensure_ascii=False)
    ro_after = _ro_count()
    print("\n完成，耗时 %.0fs | RO %d→%d | 修复 %d | 未修复 %d | 替换行 %d" %
          (time.time() - t0, ro_before, ro_after, stats["fixed"],
           stats["unfixed"], stats["replaced"]), flush=True)
    if fails:
        print("例外清单(前15):")
        for f in fails[:15]:
            print("  ", f)
    return stats


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", default=None)
    ap.add_argument("--codes", default="")
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    only = [c.strip() for c in a.codes.split(",") if c.strip()] if a.codes else None
    run(list_path=a.list, only_codes=only, dry=a.dry)