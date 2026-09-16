# -*- coding: utf-8 -*-
"""A2（2026-09-13）：回补 amount=0 且 volume>0 的日K成交额。

背景：tdx 主源熔断降级腾讯/新浪（两源均无 amount）→ 09-09 起新日期 bar amount 全 0。
补额源：腾讯 proxy.finance.qq.com newfqkline（历史任意日期、第 9 字段=成交额万元）——
  2026-09-13 实测 09-09/09-10/09-11 与东财 push2his 值完全一致（46754.99万=467,549,947元）。
  东财 push2his 在本环境高频批量后被 WAF 封（Remote end closed connection/timed out），
  腾讯接口未见封禁（作为唯一补额源）。

用法：
  python tools\\backfill_amount_recent.py                 # 默认补 09-09/09-10/09-11 三天
  python tools\\backfill_amount_recent.py --date 2026-09-11   # 补指定一天（收盘后补当日用）
  python tools\\backfill_amount_recent.py --check-only   # 只统计不写库
  python tools\\backfill_amount_recent.py --limit 100    # 仅前 N 只票（探测/分批用）

只 UPDATE amount<=0 或 NULL 且 volume>0 的行（不覆盖已有正常值，幂等可重跑）。
"""
import sqlite3, json, time, sys, random, argparse
from concurrent.futures import ThreadPoolExecutor, as_completed

DB = r"C:\Users\26838\A股模拟盘\data\market.db"
DEFAULT_DATES = ["2026-09-09", "2026-09-10", "2026-09-11"]
TX_HOSTS = ["https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get",
            "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"]
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
      "Referer": "https://gu.qq.com/", "Connection": "close"}


def tx_kline(code, lmt=20, tries=4):
    """腾讯 newfqkline（qfq）：返回 {date: amount元} 或 None。主机轮换 + 退避重试。
    行结构 [date,open,close,high,low,volume,{},pct,amount,'] → amount 单位：
    - 688 票：百万元（实证 2026-09-13：688004 09-11 row[8]=6483.36 百万=64.8亿，
      与 close×volume 吻合）→ ×1,000,000
    - 其余：万元 → ×10,000
    """
    sym = ("sh" if code.startswith(("6", "5", "9")) else "sz") + code
    mul = 1_000_000.0 if code.startswith("688") else 10_000.0
    last = None
    for a in range(tries):
        url = TX_HOSTS[0] if a % 2 == 0 else TX_HOSTS[1]
        url = url + "?param=%s,day,,,%d,qfq" % (sym, lmt)
        try:
            req = __import__("urllib.request", fromlist=["Request"]).Request(url, headers=UA)
            raw = __import__("urllib.request", fromlist=["urlopen"]).urlopen(req, timeout=15).read()
            d = json.loads(raw.decode("utf-8"))
            node = (d.get("data") or {}).get(sym) or {}
            arr = node.get("qfqday") or node.get("day") or []
            out = {}
            for row in arr:
                try:
                    if len(row) > 8 and float(row[8]) > 0:
                        out[row[0]] = float(row[8]) * mul
                except (IndexError, ValueError, TypeError):
                    pass
            if out:
                return out
            last = RuntimeError("no amount rows")
        except Exception as e:
            last = e
        time.sleep(0.8 + a * 1.2)
    print("  TX fail %s: %s" % (code, last), file=sys.stderr)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="补指定日期（可多次）")
    ap.add_argument("--check-only", action="store_true", help="只统计不写库")
    ap.add_argument("--limit", type=int, default=0, help="仅处理前 N 只票（探测用）")
    args = ap.parse_args()
    dates = [args.date] if args.date else DEFAULT_DATES

    con = sqlite3.connect(DB)
    con.execute("PRAGMA busy_timeout=30000")
    ph = ",".join("?" * len(dates))
    rows = con.execute(
        "SELECT code, date, volume FROM kline WHERE period='day' AND date IN (%s) "
        "AND (amount IS NULL OR amount=0) AND volume>0 ORDER BY date" % ph, dates).fetchall()
    codes = sorted({r[0] for r in rows})
    if args.limit:
        codes = codes[:args.limit]
        rows = [r for r in rows if r[0] in set(codes)]
    print("待补行数=%d 涉及票数=%d 日期=%s" % (len(rows), len(codes), dates), flush=True)
    if args.check_only or not rows:
        con.close()
        return

    t0 = time.time()
    got = {}
    # 并发 4：东财 WAF 对高并发批量连接会 RemoteDisconnected（2026-09-13 实测 8 并发被封）；
    # 腾讯接口较宽松，仍保持 4 并发 + 重试自律
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(tx_kline, c): c for c in codes}
        for i, fut in enumerate(as_completed(futs)):
            c = futs[fut]
            try:
                m = fut.result()
                if m:
                    got[c] = m
            except Exception:
                pass
            if (i + 1) % 500 == 0:
                print("  进度 %d/%d 已得 %d 票 用时%ds" % (i + 1, len(codes), len(got), int(time.time() - t0)))

    upd = 0
    retry = []
    cur = con.cursor()
    for i, (code, date, vol) in enumerate(rows):
        amt = (got.get(code) or {}).get(date)
        if not amt or amt <= 0:
            continue
        ok = False
        for a in range(4):          # 写锁竞争（A1 补行并发）→ 退避重试
            try:
                cur.execute(
                    "UPDATE kline SET amount=? WHERE period='day' AND code=? AND date=? "
                    "AND (amount IS NULL OR amount=0) AND volume>0", (amt, code, date))
                ok = True
                break
            except sqlite3.OperationalError:
                time.sleep(0.5 * (a + 1))
        if ok:
            upd += 1
            if upd % 100 == 0:
                con.commit()
        else:
            retry.append((code, date, amt))
    con.commit()
    # 尾重试：仍失败的票单独再试
    if retry:
        for code, date, amt in retry:
            try:
                cur.execute(
                    "UPDATE kline SET amount=? WHERE period='day' AND code=? AND date=? "
                    "AND (amount IS NULL OR amount=0) AND volume>0", (amt, code, date))
            except sqlite3.OperationalError:
                pass
        con.commit()
    print("已更新 %d 行（重试失败 %d），用时 %ds" % (upd, len(retry), int(time.time() - t0)))

    # 补后校验：每天 amount0 余量 + 一致性抽检
    for d in dates:
        z = con.execute(
            "SELECT COUNT(*) FROM kline WHERE period='day' AND date=? "
            "AND (amount IS NULL OR amount=0) AND volume>0", (d,)).fetchone()[0]
        t = con.execute("SELECT COUNT(*) FROM kline WHERE period='day' AND date=?", (d,)).fetchone()[0]
        print("  %s amount0_vol>0=%d/%d" % (d, z, t))
    samp = con.execute(
        "SELECT close, volume, amount FROM kline WHERE period='day' AND date IN (%s) "
        "AND amount>0 AND volume>0 ORDER BY RANDOM() LIMIT 200" % ph, dates).fetchall()
    if samp:
        ratios = [a / (c * v) for c, v, a in samp if c > 0 and v > 0]
        if ratios:
            print("抽检 %d 行 amount/(close*volume): min=%.3f max=%.3f "
                  "在[0.7,1.4]占比=%.1f%%" % (
                      len(ratios), min(ratios), max(ratios),
                      100.0 * sum(1 for r in ratios if 0.7 <= r <= 1.4) / len(ratios)))
    con.close()


if __name__ == "__main__":
    main()
