# -*- coding: utf-8 -*-
"""A1 补数（2026-09-13）：定点补齐 09-09/09-10/09-11 缺失日K行。

只补缺失 (code,date) 行（INSERT OR IGNORE），不重写已有行。
复用 app.updater._fetch_daily_one（tdx→腾讯→新浪 三源降级）并发拉取，
支持 --dry-run 预检与断点续跑（tmp/a1/backfill_progress.json）。

用法：
  py -3.13 tools/backfill_missing_days.py --dry-run
  py -3.13 tools/backfill_missing_days.py [--batch 100] [--limit N]
"""
import sys, os, json, time, sqlite3, argparse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "market.db")
PROG = os.path.join(ROOT, "tmp", "a1", "backfill_progress.json")
DATES = ("2026-09-09", "2026-09-10", "2026-09-11")

from app import updater as U


def load_all_codes():
    conn = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    try:
        return [r[0] for r in conn.execute(
            "SELECT DISTINCT code FROM kline WHERE period='day'").fetchall()]
    finally:
        conn.close()


def missing_rows(conn, code):
    have = set(r[0] for r in conn.execute(
        "SELECT date FROM kline WHERE code=? AND period='day' AND date IN (?,?,?)",
        (code,) + DATES).fetchall())
    return [d for d in DATES if d not in have]


def main():
    ap = argparse.ArgumentParser(description="A1 补数：09-09/10/11 缺失日K")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--batch", type=int, default=100)
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()

    codes = load_all_codes()
    if a.limit:
        codes = codes[:a.limit]
    print("全库 day K 票数: %d | 目标日期: %s" % (len(codes), "/".join(DATES)))

    conn = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    for d in DATES:
        n = conn.execute("SELECT count(*) FROM kline WHERE period='day' AND date=?",
                         (d,)).fetchone()[0]
        print("  已有 %s: %d 行" % (d, n))

    if a.dry_run:
        miss = {d: 0 for d in DATES}
        n_with_missing = 0
        for code in codes:
            m = missing_rows(conn, code)
            if m:
                n_with_missing += 1
                for d in m:
                    miss[d] += 1
        conn.close()
        print("DRY-RUN 缺失统计: 有缺失的票=%d" % n_with_missing)
        for d in DATES:
            print("  %s 缺 %d 行" % (d, miss[d]))
        return

    conn.close()
    # 实跑：先筛出有缺失的票（避免对全库 5328 只跑，只处理 2926 只有缺失者）
    conn = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    codes_with_missing = [c for c in codes if missing_rows(conn, c)]
    conn.close()
    print("有缺失的票: %d（只处理这些）" % len(codes_with_missing))
    codes = codes_with_missing
    prog = {}
    if os.path.isfile(PROG):
        try:
            with open(PROG, encoding="utf-8") as f:
                prog = json.load(f)
        except Exception:
            prog = {}
    done_codes = set(prog.get("done", []))
    from concurrent.futures import ThreadPoolExecutor, as_completed
    t0 = time.time()
    inserted = 0
    failed_codes = []
    n_batch = 0
    for i in range(0, len(codes), a.batch):
        batch = codes[i:i + a.batch]
        todo = [c for c in batch if c not in done_codes]
        if not todo:
            continue
        rows_by_code = {}
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = {ex.submit(U._fetch_daily_one, c, 60): c for c in todo}
            for f in as_completed(futs):
                c = futs[f]
                try:
                    r = f.result()
                    # ★ A2（2026-09-13）：_fetch_daily_one 返回 (k, src) tuple
                    k = r[0] if isinstance(r, tuple) else r
                    if k:
                        rows_by_code[c] = k
                except Exception:
                    failed_codes.append(c)
        conn = sqlite3.connect(DB, timeout=30)
        try:
            for c, k in rows_by_code.items():
                have = set(r[0] for r in conn.execute(
                    "SELECT date FROM kline WHERE code=? AND period='day' AND date IN (?,?,?)",
                    (c,) + DATES).fetchall())
                for row in k:
                    d = row.get("date")
                    if d in DATES and d not in have:
                        conn.execute(
                            "INSERT OR IGNORE INTO kline(code,period,date,open,high,low,"
                            "close,volume,amount) VALUES(?,?,?,?,?,?,?,?,?)",
                            (c, "day", d, row.get("open"), row.get("high"), row.get("low"),
                             row.get("close"), row.get("volume"), row.get("amount", 0)))
                        inserted += 1
            conn.commit()
        finally:
            conn.close()
        done_codes.update(c for c in todo if c not in failed_codes)
        prog["done"] = sorted(done_codes)
        os.makedirs(os.path.dirname(PROG), exist_ok=True)
        with open(PROG, "w", encoding="utf-8", newline="\n") as f:
            json.dump(prog, f, ensure_ascii=False)
        n_batch += 1
        print("批%d: 处理%d 插入%d 失败%d 累计%.0fs" %
              (n_batch, len(todo), inserted, len(failed_codes), time.time() - t0))
    print("完成: 新增 %d 行, 失败票 %d" % (inserted, len(failed_codes)))
    if failed_codes:
        print("失败样例:", failed_codes[:10])


if __name__ == "__main__":
    main()
