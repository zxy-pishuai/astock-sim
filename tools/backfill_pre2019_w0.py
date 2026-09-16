# -*- coding: utf-8 -*-
"""★ D2：2019 前日K补缺（backfill_daily 的窄写扩展版）

背景：Phase15 报告称"含 2019 前的全历史一并补齐"，但 D2 核验发现库内 2019-01-02
即为止（2018 全年仅 12 只零星票、共 2,665 行）——该表述未落在 market.db 中；
且 P64 时 w0 PIT 池因缺窗口前历史而以降级伪上市日构建 → 资格数=0。

本工具把 backfill_daily.py 的成熟管线（mootdx raw 分页全段 → 新浪 qfq 一次复权
→ NaN 过滤）只做一处收窄：**仅写 date < '2019-01-05' 的行**（w0=2019-20 窗口的
前置 60 个交易日排序窗 + 春节缓冲；绝不触碰已入库的 2019+ 行），其余纪律照旧：
  - 数据源: mootdx TCP，raw 全段分页后整体一次 factor_reversion('qfq')
    （保证与既有 2019+ 行同一复权基准，接缝无缝）
  - 并发: 6 线程池独立长连接；成功限速 0.1s、失败退避 1/2.5s；单票重试 2 次
  - 进度: data/backfill_pre2019_progress.json 断点续跑（与 Phase15 进度文件隔离）
  - 写库: 每 50 票 commit；只写 period='day' 的 pre-2019 行（INSERT OR REPLACE）
  - 失败清单: data/backfill_pre2019_failed.json

用法: python tools/backfill_pre2019_w0.py [--codes ...] [--dry]
输出: 2015~2018 逐年行数/股票数统计（校验补缺效果）
"""
import json
import os
import sqlite3
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

DB = os.path.join(BASE, "data", "market.db")
PROGRESS_FILE = os.path.join(BASE, "data", "backfill_pre2019_progress.json")
FAIL_FILE = os.path.join(BASE, "data", "backfill_pre2019_failed.json")
CUTOFF = "2019-01-05"      # 只写早于此日期的行（w0 前置窗 + 缓冲）
POOL_SIZE = 6
SLEEP_OK = 0.1
SLEEP_FAIL = (1.0, 2.5)
MAX_RETRY = 2
COMMIT_EVERY = 50

_tls = threading.local()


def _client():
    c = getattr(_tls, "c", None)
    if c is not None:
        return c
    try:
        from mootdx.quotes import Quotes
        c = Quotes.factory(market="std")
        _tls.c = c
        return c
    except Exception:
        return None


def _close():
    c = getattr(_tls, "c", None)
    if c is not None:
        try:
            c.close()
        except Exception:
            pass
        _tls.c = None


# 复用 backfill_daily 的拉取与复权实现（管线与 Phase15 逐字节一致）
from backfill_daily import _fetch_raw_pages, _to_adjusted   # noqa: E402


def _process_one(code):
    """返回 (code, ok, kl_or_err)；kl 仅含 CUTOFF 之前的行"""
    for attempt in range(MAX_RETRY + 1):
        c = _client()
        if c is None:
            return (code, False, "mootdx 连接失败")
        try:
            raw = _fetch_raw_pages(code, c)
            if not raw:
                return (code, False, "无数据")
            kl = [k for k in _to_adjusted(code, raw) if k["date"] < CUTOFF]
            if not kl:
                return (code, True, [])     # 2019 后上市的票：无前置行属正常
            return (code, True, kl)
        except Exception as e:
            if attempt < MAX_RETRY:
                time.sleep(SLEEP_FAIL[attempt])
                _close()
    return (code, False, "重试%d次仍失败" % MAX_RETRY)


def _write_pre(wconn, code, klines):
    """仅写 period='day' 且 date<CUTOFF 的行（INSERT OR REPLACE）"""
    wconn.executemany(
        "INSERT OR REPLACE INTO kline(code,period,date,open,high,low,close,"
        "volume,amount) VALUES(?,?,?,?,?,?,?,?,?)",
        [(code, "day", k["date"], k["open"], k["high"], k["low"], k["close"],
          k["volume"], k["amount"]) for k in klines])


def run(only_codes=None, dry=False):
    t0 = time.time()
    conn = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=10)
    try:
        n_pre = conn.execute(
            "SELECT COUNT(*) FROM kline WHERE period='day' AND date<?",
            (CUTOFF,)).fetchone()[0]
        codes_all = [r[0] for r in conn.execute(
            "SELECT DISTINCT code FROM kline WHERE period='day' "
            "AND length(code)=6 ORDER BY code").fetchall()]
    finally:
        conn.close()
    codes = only_codes or codes_all
    print("库内 pre-%s 现有行数: %d；待处理票数: %d" % (CUTOFF, n_pre, len(codes)),
          flush=True)

    done = {}
    if os.path.exists(PROGRESS_FILE):
        try:
            with open(PROGRESS_FILE, encoding="utf-8") as f:
                done = json.load(f)
        except Exception:
            done = {}
    todo = [c for c in codes if c not in done]
    if dry:
        todo = todo[:3]
    print("已完成 %d，本次处理 %d" % (len(done), len(todo)), flush=True)

    results = []
    with ThreadPoolExecutor(max_workers=POOL_SIZE) as ex:
        for i, (code, ok, payload) in enumerate(ex.map(_process_one, todo)):
            results.append((code, ok, payload))
            if (i + 1) % 50 == 0:
                print("  %d/%d 拉取完成" % (i + 1, len(todo)), flush=True)
    with ThreadPoolExecutor(max_workers=POOL_SIZE) as ex:
        list(ex.map(lambda _: _close(), range(POOL_SIZE)))

    fails = []
    added = 0
    if not dry:
        wconn = sqlite3.connect(DB, timeout=60)
        wconn.execute("PRAGMA journal_mode=WAL")
        wconn.execute("PRAGMA synchronous=NORMAL")
        batch_n = 0
        for code, ok, payload in results:
            if ok:
                if payload:
                    _write_pre(wconn, code, payload)
                    added += len(payload)
                done[code] = time.strftime("%Y-%m-%d %H:%M:%S")
            else:
                fails.append({"code": code, "error": str(payload)[:80]})
            batch_n += 1
            if batch_n >= COMMIT_EVERY:
                wconn.commit()
                batch_n = 0
        wconn.commit()
        wconn.close()
    else:
        for code, ok, payload in results:
            if ok:
                done[code] = "dry"
                if payload:
                    added += len(payload)
            else:
                fails.append({"code": code, "error": str(payload)[:80]})

    try:
        with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
            json.dump(done, f, ensure_ascii=False, indent=0)
        with open(FAIL_FILE, "w", encoding="utf-8") as f:
            json.dump(fails, f, ensure_ascii=False, indent=1)
    except Exception as e:
        print("保存进度失败:", e)

    print("\n完成 %.0fs | 本次新增 pre-%s 行 %d | 失败 %d" %
          (time.time() - t0, CUTOFF, added, len(fails)), flush=True)
    conn = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=10)
    try:
        for y in ("2014", "2015", "2016", "2017", "2018"):
            r = conn.execute(
                "SELECT COUNT(*), COUNT(DISTINCT code), COUNT(DISTINCT date) "
                "FROM kline WHERE period='day' AND substr(date,1,4)=?",
                (y,)).fetchone()
            print("  %s: %d 行 / %d 只 / %d 个交易日" % ((y,) + tuple(r)),
                  flush=True)
        # w0 排序线就绪度：2018Q4 有 >=30 根K线的票数
        n30 = conn.execute(
            "SELECT COUNT(*) FROM (SELECT code FROM kline WHERE period='day' "
            "AND date>='2018-10-01' AND date<'2019-01-01' GROUP BY code "
            "HAVING COUNT(*)>=30)").fetchone()[0]
        print("  2018Q4 >=30根K 的票数（w0 排序候选）: %d" % n30, flush=True)
        conn.close()
    finally:
        pass
    return {"added": added, "fails": fails}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", default="")
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    only = ([c.strip() for c in a.codes.split(",") if c.strip()]
            if a.codes else None)
    run(only_codes=only, dry=a.dry)
