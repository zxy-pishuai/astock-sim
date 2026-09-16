# -*- coding: utf-8 -*-
"""★ K2（2026-09-16）：成交额（amount）缺失定向回填工具。
扫描 kline(period='day') 中 amount IS NULL OR amount=0 且 volume>0 的行，
主源 baostock（TCP 协议，历史日 amount 完整；东财 push2his 当前网络不可达、
tdx 不可用、腾讯 fqkline 无额——2026-09-16 归因实测结论），备源腾讯 qt.gtimg
（仅最近交易日）。
★ 只 UPDATE amount 列，绝不动 OHLCV/volume；不改前复权锚定（amount 为真实
成交额，与复权无关，baostock adjustflag=3 不复权仅取 amount 字段）。
★ 幂等：扫描条件天然排除已补行（amount>0），重跑结果逐位一致（天然断点续跑）。
用法：
  py -3.13 tools/backfill_amount.py --start 2026-09-07 --end 2026-09-15 --dry-run
  py -3.13 tools/backfill_amount.py --start 2026-09-07 --end 2026-09-15
  py -3.13 tools/backfill_amount.py --start 2026-09-07 --end 2026-09-15 --codes 600519,000001
"""
import argparse
import sqlite3
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

DB = "C:/Users/26838/A股模拟盘/data/market.db"
_BS_CODE = {"6": "sh.", "5": "sh.", "9": "sh.", "0": "sz.", "3": "sz."}


def _to_bs(code):
    """600519 -> sh.600519；000001 -> sz.000001（库内个股无前缀）。"""
    return _BS_CODE.get(code[:1], "") + code


def _scan_missing(conn, start, end, codes=None, limit=None):
    """扫描缺失行。返回 [(code, date, volume)]。排除指数（sh/sz 前缀 + 000905）。"""
    sql = ("SELECT code, date, volume FROM kline WHERE period='day' "
           "AND date>=? AND date<=? AND volume>0 "
           "AND (amount IS NULL OR amount=0) "
           "AND code NOT LIKE 'sh%' AND code NOT LIKE 'sz%' AND code != '000905'")
    args = [start, end]
    if codes:
        ph = ",".join("?" * len(codes))
        sql += f" AND code IN ({ph})"
        args += list(codes)
    sql += " ORDER BY date, volume DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql, args).fetchall()


def _fetch_amounts_baostock(code, start, end):
    """baostock 历史日 amount（元）。返回 {date: amount} 或 None（失败）。"""
    import baostock as bs
    rs = bs.query_history_k_data_plus(
        _to_bs(code),
        "date,amount",
        start_date=start, end_date=end,
        frequency="d", adjustflag="3")
    if rs.error_code != "0":
        return None
    out = {}
    while rs.next():
        row = rs.get_row_data()
        if len(row) >= 2:
            try:
                a = float(row[1])
                if a > 0:
                    out[row[0]] = a
            except (ValueError, IndexError):
                pass
    return out or None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-09-01")
    ap.add_argument("--end", default="2026-09-15")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--codes", default="")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--sleep", type=float, default=0.05)
    args = ap.parse_args()

    conn = sqlite3.connect(DB, timeout=30)
    codes = [c.strip() for c in args.codes.split(",") if c.strip()] or None
    missing = _scan_missing(conn, args.start, args.end, codes, args.limit)
    if not missing:
        print(f"[{args.start}~{args.end}] 无缺失行（已完整）")
        conn.close()
        return

    by_day = {}
    for c, d, v in missing:
        by_day.setdefault(d, []).append((c, v))
    print(f"[{args.start}~{args.end}] 扫描到缺失 {len(missing)} 行，"
          f"覆盖 {len(by_day)} 个交易日：")
    for d in sorted(by_day):
        print(f"  {d}: {len(by_day[d])} 行")

    if args.dry_run:
        print("\n[dry-run] 未写库。")
        conn.close()
        return

    # 主源 baostock（登录一次，会话内多查）
    import baostock as bs
    lg = bs.login()
    if lg.error_code != "0":
        print(f"[FATAL] baostock 登录失败: {lg.error_msg}")
        conn.close()
        sys.exit(2)

    ok = failed = skipped = 0
    t0 = time.time()
    try:
        for d in sorted(by_day):
            day_ok = 0
            for code, _vol in by_day[d]:
                amts = _fetch_amounts_baostock(code, args.start, args.end)
                if not amts or d not in amts:
                    failed += 1
                    continue
                cur = conn.execute(
                    "SELECT amount FROM kline WHERE code=? AND period='day' AND date=?",
                    (code, d)).fetchone()
                if cur and cur[0] and cur[0] > 0:
                    skipped += 1        # 已被并发写（幂等防线）
                    continue
                conn.execute(
                    "UPDATE kline SET amount=? WHERE code=? AND period='day' AND date=?",
                    (amts[d], code, d))
                conn.commit()
                ok += 1
                day_ok += 1
                time.sleep(args.sleep)
            print(f"  {d}: 补 {day_ok}/{len(by_day[d])}")
    finally:
        bs.logout()
    conn.close()
    el = time.time() - t0
    print(f"\n完成：补 {ok} 行 / 失败 {failed} / 跳过 {skipped}，耗时 {el:.1f}s")


if __name__ == "__main__":
    main()
