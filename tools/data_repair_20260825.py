# -*- coding: utf-8 -*-
"""★ Phase35：数据修复包 —— Phase31 哨兵发现项的处置（可复跑）

任务1：前复权缺陷修复（600262 北方股份、600426 华鲁恒升）
  - 先备份两票 kline(day) 全部行到 data/backups/*.json
  - 复用 Phase20 的 tools/repair_qfq.py repair_one()（mootdx 原始日K + TDX xdxr +
    自实现前复权；自带"2019 起仍有跳变就不写库"保护）
  - 修复后按哨兵 high 口径复检（|次日开盘/当日收盘-1| > 2×限幅+1%）
  - 失败不写库 → 报告给出 DATA_EXCLUDE_CODES 建议增补行（不改 config.py）

任务2：美元指数 DINIW 历史回补进 global_kline
  - 逐个实测候选源：东财 push2his(100.UDI/100.DINIW)、腾讯(usUDI/usDINIW)、
    新浪、Stooq(dx.f)；记录状态码与样本
  - 用首个可用源回补日线收盘，INSERT OR REPLACE（主键 sym+date），只写 <=今天

红线：只写两票 kline 行、global_kline 表、data/backups/、本文件与报告；
      不改 config.py 与 app/；写库短事务；改前查 market.db mtime。

用法: python tools/data_repair_20260825.py [--skip-repair] [--skip-diniw]
"""
import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

DB = os.path.join(BASE, "data", "market.db")
BACKUP_DIR = os.path.join(BASE, "data", "backups")
REPAIR_CODES = ["600262", "600426"]
TODAY = time.strftime("%Y-%m-%d")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
MTIME_GRACE_SEC = 300   # 库 5 分钟内被他人改过 → 中止写入


def db_mtime_age():
    return time.time() - os.path.getmtime(DB)


def fetch_url(url, timeout=15):
    hdrs = dict(UA)
    if "eastmoney" in url:
        # 东财对无 Referer 的脚本请求间歇性掐断（实测必需）
        hdrs["Referer"] = "https://quote.eastmoney.com/"
        hdrs["Accept"] = "*/*"
    req = urllib.request.Request(url, headers=hdrs)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read()


# ============ 任务1：前复权缺陷修复 ============
ANOMALY_WINDOWS = [("2026-04-20", "2026-05-08"), ("2026-07-06", "2026-07-22")]


def snapshot_window(cur, code):
    out = []
    for d0, d1 in ANOMALY_WINDOWS:
        cur.execute(
            "SELECT date, open, high, low, close, volume FROM kline "
            "WHERE code=? AND period='day' AND date>=? AND date<=? ORDER BY date",
            (code, d0, d1))
        out.append({"window": [d0, d1],
                    "rows": [{"date": r[0], "open": r[1], "high": r[2],
                              "low": r[3], "close": r[4], "volume": r[5]}
                             for r in cur]})
    return out


def jump_scan(cur, code):
    """哨兵 high 口径复检：|次日开盘/当日收盘-1| > 2×限幅+1%（主板 0.21）"""
    cur.execute("SELECT date, open, close FROM kline WHERE code=? AND period='day' "
                "AND date>='2019-01-01' ORDER BY date", (code,))
    rows = [r for r in cur if r[1] and r[2]]
    hits = []
    for (d0, o0, c0), (d1, o1, c1) in zip(rows, rows[1:]):
        if c0 and abs(o1 / c0 - 1.0) > 0.21:
            hits.append({"prev_date": d0, "date": d1,
                         "jump_pct": round((o1 / c0 - 1) * 100, 2)})
    return hits


def backup_codes(cur):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    path = os.path.join(BACKUP_DIR,
                        "kline_day_600262_600426_before_qfq_repair_%s.json"
                        % time.strftime("%Y%m%d_%H%M%S"))
    payload = {"created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
               "reason": "Phase35 qfq 重修复前留底（600262/600426, period=day）",
               "codes": {}}
    n = 0
    for code in REPAIR_CODES:
        cur.execute("SELECT date, open, high, low, close, volume, amount "
                    "FROM kline WHERE code=? AND period='day' ORDER BY date", (code,))
        rows = [{"date": r[0], "open": r[1], "high": r[2], "low": r[3],
                 "close": r[4], "volume": r[5], "amount": r[6]} for r in cur]
        payload["codes"][code] = rows
        n += len(rows)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    print("  备份 %d 行 → %s" % (n, path), flush=True)
    return path, n


def run_repair(dry=False):
    from tools import repair_qfq as rq   # 复用 Phase20 实现（fetch_raw/make_qfq/repair_one）
    conn = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=15)
    cur = conn.cursor()
    results = {}
    print("== 任务1：前复权重修复 %s ==" % REPAIR_CODES, flush=True)
    age = db_mtime_age()
    print("  market.db mtime 距今 %.0fs" % age, flush=True)
    if age < MTIME_GRACE_SEC and not dry:
        print("  ⚠ 库 5 分钟内有写入（并行任务），中止修复以防冲突", flush=True)
        conn.close()
        return None
    backup_path, backup_rows = backup_codes(cur)
    for code in REPAIR_CODES:
        before_win = snapshot_window(cur, code)
        before_jumps = jump_scan(cur, code)
        ok, msg, replaced, gaps = False, "dry", 0, 0
        if not dry:
            for attempt in range(3):
                try:
                    ok, msg, replaced, gaps = rq.repair_one(code)
                    break
                except Exception as e:
                    msg = "异常:%s" % e
                    time.sleep(1.5 * (attempt + 1))
        after_win = snapshot_window(cur, code)
        after_jumps = jump_scan(cur, code)
        # 最新收盘对照（确认未把现价写崩）
        cur.execute("SELECT date, close FROM kline WHERE code=? AND period='day' "
                    "ORDER BY date DESC LIMIT 1", (code,))
        last = cur.fetchone()
        results[code] = {
            "ok": bool(ok), "msg": msg, "replaced_rows": replaced,
            "gaps_after_write_guard": gaps,
            "before_window": before_win, "after_window": after_win,
            "jumps_before_sentinel_high": before_jumps,
            "jumps_after_sentinel_high": after_jumps,
            "latest_close_after": {"date": last[0], "close": last[1]} if last else None,
        }
        print("  [%s] ok=%s msg=%s 替换=%s 跳变(修后)=%d 最新收盘=%s" % (
            code, ok, msg, replaced, len(after_jumps),
            last[1] if last else "?"), flush=True)
    conn.close()
    return {"backup_path": backup_path, "backup_rows": backup_rows,
            "results": results}


# ============ 任务2：DINIW 历史回补 ============
EM_HOSTS = ["https://push2his.eastmoney.com", "https://45.push2his.eastmoney.com",
            "https://21.push2his.eastmoney.com"]
EM_PATH = ("/api/qt/stock/kline/get?"
           "secid={secid}&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f53&klt=101&fqt=0"
           "&beg=19990101&end=20500101")


def probe_eastmoney(secid):
    """东财对脚本连接间歇性掐断（实测），主机轮换 + 重试后稳定返回"""
    last = None
    for attempt in range(5):
        host = EM_HOSTS[attempt % len(EM_HOSTS)]
        try:
            status, body = fetch_url(host + EM_PATH.format(secid=secid))
            js = json.loads(body.decode("utf-8", "replace"))
            kl = ((js.get("data") or {}).get("klines")) or []
            rows = []
            for s in kl:
                p = s.split(",")
                if len(p) >= 2:
                    rows.append((p[0], float(p[1])))
            name = (js.get("data") or {}).get("name")
            if rows:
                return status, name, rows
            last = "200 但 klines 空"
        except Exception as e:
            last = e
        time.sleep(1.0 + attempt)
    raise RuntimeError("eastmoney %s 连续失败: %s" % (secid, last))


def probe_tencent(sym):
    url = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
           "param=%s,day,,,800,qfq" % sym)
    status, body = fetch_url(url)
    js = json.loads(body.decode("utf-8", "replace"))
    data = js.get("data") or {}
    node = data.get(sym) or {}
    arr = node.get("qfqday") or node.get("day") or []
    rows = [(r[0], float(r[2])) for r in arr if len(r) >= 3]
    return status, sym, rows


def probe_stooq(symbol):
    url = "https://stooq.com/q/d/l/?s=%s&i=d" % symbol
    status, body = fetch_url(url)
    text = body.decode("utf-8", "replace")
    lines = text.strip().splitlines()
    rows = []
    for ln in lines[1:]:
        p = ln.split(",")
        if len(p) >= 5 and p[0][:2] == "20":
            try:
                rows.append((p[0], float(p[4])))
            except ValueError:
                continue
    return status, symbol, rows


def probe_sina():
    # 新浪环球外汇日线（jsonp 包一层 var _=）；susdcnh 为替代参照（USDCNH）
    url = ("https://vip.stock.finance.sina.com.cn/forex/api/jsonp.php/"
           "var%20_=/NewForexService.getDayKLine?symbol=fx_susdcnh")
    status, body = fetch_url(url)
    return status, "susdcnh(sina)", _parse_sina_kline(body.decode("utf-8", "replace"))


def _parse_sina_kline(text):
    """jsonp 体：var _=("date,o,l,h,c,|date,o,l,h,c,|...") —— 竖线分隔的行串"""
    start = text.find("(")
    if start < 0:
        return []
    raw = json.loads(text[start + 1: text.rfind(")")])
    if not isinstance(raw, str):
        return []
    rows = []
    for ln in raw.split("|"):
        p = ln.strip().strip(",").split(",")
        if len(p) >= 5 and p[0][:2] == "20":
            try:
                o, lo, hi, c = map(float, p[1:5])
            except ValueError:
                continue
            if not (lo <= min(o, c) and hi >= max(o, c)):
                continue   # 字段序异常防御
            rows.append((p[0], c))
    return rows


def probe_sina_diniw():
    """★ 首选源：新浪环球外汇 美元指数(fx_sdiniw) 日线，实测 1985-11 起全史。
    行格式 'date,open,low,high,close,'（第2列恒为区间最小、第3列恒为区间最大，
    经多行验证）；取末列为收盘。"""
    url = ("https://vip.stock.finance.sina.com.cn/forex/api/jsonp.php/"
           "var%20_=/NewForexService.getDayKLine?symbol=fx_sdiniw")
    status, body = fetch_url(url)
    return status, "sdiniw(sina)", _parse_sina_kline(body.decode("utf-8", "replace"))


def run_diniw():
    probes = []
    print("\n== 任务2：DINIW 候选源探测 ==", flush=True)

    def try_source(label, fn):
        try:
            status, name, rows = fn()
        except Exception as e:
            status, name, rows = "ERR:%s" % str(e)[:80], label, []
        sample = rows[:2] + rows[-2:] if rows else []
        probes.append({"source": label, "status": status, "rows": len(rows),
                       "range": [rows[0][0], rows[-1][0]] if rows else None,
                       "sample": [(d, c) for d, c in sample]})
        print("  %-28s status=%s rows=%d %s" % (
            label, status, len(rows),
            "%s~%s" % (rows[0][0], rows[-1][0]) if rows else ""), flush=True)
        return rows if rows else None

    candidates = [
        ("sina fx_sdiniw 美元指数日线", probe_sina_diniw),
        ("eastmoney push2his secid=100.UDI", lambda: probe_eastmoney("100.UDI")),
        ("eastmoney push2his secid=100.DINIW", lambda: probe_eastmoney("100.DINIW")),
        ("tencent usUDI(实测为ETF,仅存证)", lambda: probe_tencent("usUDI")),
        ("tencent usDINIW", lambda: probe_tencent("usDINIW")),
        ("stooq dx.f", lambda: probe_stooq("dx.f")),
        ("stooq usdidx", lambda: probe_stooq("usdidx")),
        ("sina susdcnh(替代参照)", probe_sina),
    ]
    chosen_label, chosen_rows = None, None
    for label, fn in candidates:
        rows = try_source(label, fn)
        if label.startswith("eastmoney") and rows and len(rows) >= 30:
            chosen_label, chosen_rows = label, rows   # 首选东财现货美元指数
            break
        if rows and len(rows) >= 30 and chosen_rows is None:
            chosen_label, chosen_rows = label, rows
    if not chosen_rows:
        print("  ⚠ 所有候选源均不可用，不硬凑（报告给替代方案）", flush=True)
        return {"probes": probes, "chosen": None, "written": 0}

    rows = [(d, c) for d, c in sorted(set(chosen_rows)) if d <= TODAY]  # 不回补未来日期
    age = db_mtime_age()
    if age < MTIME_GRACE_SEC:
        print("  ⚠ 库 5 分钟内有写入，中止回补", flush=True)
        return {"probes": probes, "chosen": chosen_label, "written": 0,
                "aborted_mtime": True}
    w = sqlite3.connect(DB, timeout=30)
    try:
        w.execute("PRAGMA journal_mode=WAL")
        w.execute("BEGIN")
        w.executemany(
            "INSERT OR REPLACE INTO global_kline(market, sym, date, close) "
            "VALUES('fx','DINIW',?,?)", rows)
        w.commit()   # 短事务一次性提交
    finally:
        w.close()
    print("  回补完成：%s → %d 行（%s ~ %s）" % (
        chosen_label, len(rows), rows[0][0], rows[-1][0]), flush=True)
    ro = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    try:
        chk = ro.execute("SELECT COUNT(*), MIN(date), MAX(date) FROM global_kline "
                         "WHERE sym='DINIW'").fetchone()
    finally:
        ro.close()
    print("  校验：sym=DINIW 共 %d 行（%s ~ %s）" % chk, flush=True)
    return {"probes": probes, "chosen": chosen_label, "written": len(rows),
            "verify": {"rows": chk[0], "first": chk[1], "last": chk[2]}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-repair", action="store_true")
    ap.add_argument("--skip-diniw", action="store_true")
    ap.add_argument("--dry", action="store_true", help="任务1只做备份与体检，不写库")
    a = ap.parse_args()
    out = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"), "phase": "35"}
    if not a.skip_repair:
        out["qfq_repair"] = run_repair(dry=a.dry)
    if not a.skip_diniw:
        out["diniw_backfill"] = run_diniw()
    path = os.path.join(BASE, "data", "quality_report.json.repair_20260825.json")
    # 结果摘要落盘（供报告引用；不覆盖 quality_report.json）
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("\n结果摘要已写出 %s" % path, flush=True)


if __name__ == "__main__":
    main()
