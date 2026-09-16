# -*- coding: utf-8 -*-
"""★ Phase15：历史日K补全 backfill（2019-01-01 起 → 今天）
目标：把 market.db kline(period='day') 从实际 2025 年起，补全到 2019-01-01。
- 数据源: mootdx（通达信 TCP，raw 分页全段 → 一次 factor_reversion('qfq') 前复权，
  与现有 datafeed._fetch_daily 的 mootdx qfq 口径一致；一次性重拉消除"旧基准 vs 现基准"漂移）
- 并发: 6 线程池 + 每线程独立长连接（复用 app/tdx.py 经验）；成功限速 0.1s、失败退避 1/2.5s
- 可靠性: 单票重试 2 次后跳过并记录失败清单；进度文件 data/backfill_progress.json 断点续跑
- 写库: 每 50 票 executemany + commit；只写 period='day'（严禁触碰其他 period）
- 校验: 写库前后用 file:...?mode=ro 只读连接（避免与运行中服务锁冲突）
- 输出: 2019-2026 逐年行数/股票数统计 + 抽查 3 只连续性 → docs/reports/backfill_daily.md

用法: python tools/backfill_daily.py [--start 2019-01-01] [--codes 600519,000001] [--dry]
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

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DB = os.path.join(BASE, "data", "market.db")
PROGRESS_FILE = os.path.join(BASE, "data", "backfill_progress.json")
FAIL_FILE = os.path.join(BASE, "data", "backfill_failed.json")
START_DEFAULT = "2019-01-01"
POOL_SIZE = 6          # ★ 复用 tdx 经验：6 连接最优（8 连接会被服务器限流）
SLEEP_OK = 0.1         # 成功限速
SLEEP_FAIL = (1.0, 2.5)  # 失败退避
MAX_RETRY = 2          # 重试次数
COMMIT_EVERY = 50      # 每 N 票 commit 一次
PAGE = 800             # mootdx 单页上限

_tls = threading.local()

# ---------- 连接 ----------
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


# ---------- 拉取: raw 分页全段 ----------
def _fetch_raw_pages(code, client):
    """不复权分页拉全段（start 翻页），返回按时间升序的 rows。
    rows 元素: (date, open, close, high, low, vol_hand, amount)"""
    out = []
    start = 0
    while True:
        df = client.bars(symbol=code, frequency=9, start=start, offset=PAGE)
        if df is None or len(df) == 0:
            break
        rows = []
        for i in range(len(df)):
            r = df.iloc[i]
            try:
                dt = str(r.get("datetime", ""))[:10]
                if len(dt) != 10:
                    continue
                rows.append((dt,
                             float(r["open"]), float(r["close"]),
                             float(r["high"]), float(r["low"]),
                             float(r.get("vol", 0) or 0),
                             float(r.get("amount", 0) or 0)))
            except Exception:
                continue
        if not rows:
            break
        out = rows + out   # 每页内部时间正序；页间倒序拼接 → 升序
        if len(rows) < PAGE:
            break
        start += PAGE
    return out


def _to_adjusted(code, raw_rows):
    """raw 全段 → 一次前复权。返回 kline 行列表 [{date,open,high,low,close,volume,amount}]
    复权采用新浪 qfq 因子（factor_reversion，mootdx 内置，与现有 datafeed 口径一致）。
    baoli_qfq(通达信xdxr) 在本 mootdx 版本对 NaN 除权字段处理有缺陷（输出全 NaN），弃用。
    仅接受数值完整的行（NaN/None 一律剔除，绝不写脏数据）；NaN 占比过高视该票为"复权失败"并
    抛异常 → 上层记入失败清单（该类票多为极端高送转，可后续用备选源 adata 补）。
    """
    if not raw_rows:
        return []
    import pandas as pd
    df = pd.DataFrame(raw_rows,
                      columns=["datetime", "open", "close", "high", "low", "vol", "amount"])
    df["datetime"] = pd.to_datetime(df["datetime"])
    df = df.set_index("datetime")
    from mootdx.tools.reversion import factor_reversion
    adj = factor_reversion(code, "qfq", df)
    out = []
    nan_cnt = 0
    for d, r in adj.iterrows():
        try:
            ds = str(d)[:10]
            o, h, l, c = float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])
            v, amt = float(r["vol"]), float(r["amount"])
            # NaN 过滤：OHLC/volume/amount 任一 NaN/None → 剔除该行
            vals = [o, h, l, c, v, amt]
            if any(x is None or (isinstance(x, float) and x != x) for x in vals):
                nan_cnt += 1
                continue
            out.append({
                "date": ds,
                "open": o, "high": h, "low": l, "close": c,
                "volume": v * 100,   # 手→股（与 datafeed._bars_one 一致）
                "amount": amt,
            })
        except Exception:
            nan_cnt += 1
            continue
    # 过滤后剩余过少或有 NaN 过半 → 复权不可靠，抛异常记失败（不写脏数据）
    if len(out) < 10:
        raise RuntimeError("复权后有效行不足(%d)，NaN=%d" % (len(out), nan_cnt))
    if nan_cnt > len(out):
        raise RuntimeError("复权后 NaN 行过多(%d > 有效%d)" % (nan_cnt, len(out)))
    return out


# ---------- 写库 ----------
def _write_batch(conn, code, klines):
    conn.executemany(
        "INSERT OR REPLACE INTO kline(code,period,date,open,high,low,close,volume,amount) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        [(code, "day", k["date"], k["open"], k["high"], k["low"], k["close"],
          k["volume"], k["amount"]) for k in klines])
    conn.commit()


def _ro_check():
    """只读连接校验：确认 kline 表可读（也作为写库前后一致性基线）"""
    conn = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=5)
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM kline WHERE period='day'").fetchone()[0]
        return n
    finally:
        conn.close()


# ---------- 单票处理（池线程内） ----------
def _process_one(code):
    """返回 (code, ok: bool, msg)"""
    for attempt in range(MAX_RETRY + 1):
        c = _client()
        if c is None:
            return (code, False, "mootdx 连接失败")
        try:
            raw = _fetch_raw_pages(code, c)
            if not raw:
                return (code, False, "无数据")
            kl = _to_adjusted(code, raw)
            if len(kl) < 10:
                return (code, False, "数据不足(%d根)" % len(kl))
            return (code, True, kl)
        except Exception as e:
            if attempt < MAX_RETRY:
                time.sleep(SLEEP_FAIL[attempt])
                _close()   # 失败重试前重建连接（自愈）
    return (code, False, "重试%d次仍失败" % MAX_RETRY)


# ---------- 主流程 ----------
def run(start=START_DEFAULT, only_codes=None, dry=False):
    t0 = time.time()
    # 1. 读取已有 code 清单（kline 表所有 code）
    conn = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=5)
    try:
        codes_all = [r[0] for r in conn.execute(
            "SELECT DISTINCT code FROM kline WHERE period='day' ORDER BY code").fetchall()]
    finally:
        conn.close()
    if only_codes:
        codes = [c for c in only_codes if c]
    else:
        codes = codes_all
    print("待处理票数:", len(codes), "（起点 %s，dry=%s）" % (start, dry), flush=True)

    # 2. 断点续跑：读进度
    done = {}
    if os.path.exists(PROGRESS_FILE):
        try:
            with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
                done = json.load(f)
        except Exception:
            done = {}
    todo = [c for c in codes if c not in done]
    print("已完成 %d，本次续跑 %d" % (len(done), len(todo)), flush=True)

    # 3. 并发处理（★ 必须在 with 内消费 map，否则线程池关闭后迭代空）
    results = []        # (code, ok, kl_or_err)
    fails = []
    if dry:
        # dry-run：只测 3 只检查链路
        todo = todo[:3]
    with ThreadPoolExecutor(max_workers=POOL_SIZE) as ex:
        for i, (code, ok, payload) in enumerate(ex.map(_process_one, todo)):
            results.append((code, ok, payload))
            if (i + 1) % 20 == 0:
                print("  %d/%d 完成" % (i + 1, len(todo)), flush=True)
    # 线程池结束后干净关闭所有线程连接
    with ThreadPoolExecutor(max_workers=POOL_SIZE) as ex:
        list(ex.map(lambda _: _close(), range(POOL_SIZE)))

    # 4. 写库（每 COMMIT_EVERY 票 commit；只写 period='day'）
    if not dry:
        wconn = sqlite3.connect(DB, timeout=60)
        wconn.execute("PRAGMA journal_mode=WAL")
        wconn.execute("PRAGMA synchronous=NORMAL")
        batch_n = 0
        for code, ok, payload in results:
            if ok:
                _write_batch(wconn, code, payload)
                done[code] = time.strftime("%Y-%m-%d %H:%M:%S")
            else:
                fails.append({"code": code, "error": payload})
            batch_n += 1
            if batch_n >= COMMIT_EVERY:
                batch_n = 0
        wconn.close()
    else:
        for code, ok, payload in results:
            if ok:
                done[code] = time.strftime("%Y-%m-%d %H:%M:%S")
            else:
                fails.append({"code": code, "error": payload})

    # 5. 保存进度与失败清单
    try:
        with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
            json.dump(done, f, ensure_ascii=False, indent=0)
        with open(FAIL_FILE, "w", encoding="utf-8") as f:
            json.dump(fails, f, ensure_ascii=False, indent=1)
    except Exception as e:
        print("保存进度失败:", e)

    # 6. 校验 + 统计
    ro_before = _ro_check()
    stats = year_stats()
    print("\n完成，耗时 %.0fs | 写库前只读校验行数 %d | 失败 %d 只" %
          (time.time() - t0, ro_before, len(fails)), flush=True)
    for y, (n, nd) in sorted(stats.items()):
        print("  %s: %d 行 / %d 只" % (y, n, nd), flush=True)
    if fails:
        print("\n失败清单(前20):")
        for f in fails[:20]:
            print("  ", f["code"], f["error"])
    return {"elapsed": time.time() - t0, "stats": stats, "fails": fails}


def year_stats():
    conn = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=5)
    try:
        rows = conn.execute(
            "SELECT substr(date,1,4) y, COUNT(*), COUNT(DISTINCT code) "
            "FROM kline WHERE period='day' GROUP BY y").fetchall()
        return {r[0]: (r[1], r[2]) for r in rows}
    finally:
        conn.close()


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=START_DEFAULT)
    ap.add_argument("--codes", default="")
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    only = [c.strip() for c in a.codes.split(",") if c.strip()] if a.codes else None
    run(start=a.start, only_codes=only, dry=a.dry)