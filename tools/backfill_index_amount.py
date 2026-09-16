#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""K10（P1-2）：指数成交额回填（东财 push2his）。
回填 09-14/09-15（及后续任何 amount=0 的指数日）的指数成交额；
写库走 UPSERT（amount 仅新值>0 时覆盖，0 保护保留库内非 0 旧额）。

用法:
  python tools/backfill_index_amount.py            # 全量回填（amount=0 指数日）
  python tools/backfill_index_amount.py --days 30  # 只看最近 30 交易日窗口
  python tools/backfill_index_amount.py --dry      # 试跑不写库（出对拍与待回填清单）

判据（任务书）:
  ① 三指数最近 10 个交易日 amount>0 覆盖率 100%
  ② 与交易所公布的两市成交额抽样对拍误差 ≤1%（抽样=库内 A5 已填值 vs 东财同日）
  ③ 写库仍走 UPSERT（ON CONFLICT DO UPDATE，不得回退 INSERT OR REPLACE）
  ④ J4 冒烟用例：新值为 0 时不得覆盖库内非 0 amount（tests/test_j4_call_smoke.py）
"""
import os
import sys
import json
import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app import config as C            # noqa: E402
import app.db as _db                   # noqa: E402
from app import datafeed as df         # noqa: E402
from app import audit                  # noqa: E402

INDEX_CODES = df.INDEX_CODES          # sh000001/sz399001/sz399006


def _d2e(d):
    """YYYY-MM-DD -> YYYYMMDD"""
    return d.replace("-", "")


def find_zero_amount_days(conn, days_back=0):
    """三指数 amount=0 的交易日（按日期升序）。
    days_back>0 时只查最近 N 个交易日（从 MAX(date) 回溯）。"""
    zero = {}
    for code in INDEX_CODES:
        if days_back:
            rows = conn.execute(
                "SELECT date FROM kline WHERE code=? AND period='day' AND amount<=0 "
                "AND date >= (SELECT MAX(date) FROM kline WHERE code=? AND period='day') "
                "ORDER BY date", (code, code)).fetchall()
            # 上面按最新日起算，改为按窗口：直接取最近 N 个交易日集合
            all_d = conn.execute(
                "SELECT DISTINCT date FROM kline WHERE code=? AND period='day' "
                "ORDER BY date DESC LIMIT ?", (code, days_back)).fetchall()
            all_d = {r[0] for r in all_d}
            rows = conn.execute(
                "SELECT date FROM kline WHERE code=? AND period='day' AND amount<=0 "
                "ORDER BY date", (code,)).fetchall()
            zero[code] = sorted(d for d, in rows if d in all_d)
        else:
            rows = conn.execute(
                "SELECT date FROM kline WHERE code=? AND period='day' AND amount<=0 "
                "ORDER BY date", (code,)).fetchall()
            zero[code] = [r[0] for r in rows]
    return zero


def backfill(conn, dry=False):
    """对 amount=0 指数日做 UPSERT 回填。返回 (updated, rows_by_code)。"""
    zero = find_zero_amount_days(conn, days_back=0)
    updated = 0
    detail = {}
    for code in INDEX_CODES:
        # ★ 当日收盘修正：无条件尝试东财 f48（收盘后=收盘额），独立于 dates——
        #   覆盖同花顺当日行可能的盘中缓存值（如 sh000001 9/16 5528 亿 < 收盘 8711 亿）
        #   （含退避重试——push2 偶发限流，单测正常但进程内可能空）
        import time as _t
        t_amt = {}
        for _try in range(3):
            t_amt = df.fetch_index_amount_today_em(code)
            if t_amt:
                break
            _t.sleep(2)
        if t_amt and not dry:
            cur = conn.cursor()
            for _d, _v in t_amt.items():
                if _v > 0:
                    cur.execute(
                        "UPDATE kline SET amount=? WHERE code=? "
                        "AND period='day' AND date=? AND (?>0 OR amount<=0)",
                        (_v, code, _d, _v))
            conn.commit()
        dates = zero.get(code) or []
        if not dates:
            detail[code] = {"zero_days": 0, "updated": 0,
                            "today_fix": (t_amt.get(list(t_amt)[0]) if t_amt else None)}
            continue
        beg = _d2e(dates[0])
        end = _d2e(dates[-1])
        amt = df.fetch_index_amount_em(code, beg, end)
        src = "东财"
        if not amt:
            # 东财 push2his 时段性限流 → 同花顺备源（last20 覆盖窗口）
            amt = df.fetch_index_amount_ths(code, days=20)
            src = "同花顺(东财限流降级)"
        if not amt:
            # 双源历史均缺（如同日数据滞后）→ 东财实时 f48（收盘后=收盘额，仅当日）
            amt = df.fetch_index_amount_today_em(code)
            src = "东财实时f48(仅当日)"
        if not amt:
            print(f"  [WARN] {code} 双源额获取为空（{dates[0]}~{dates[-1]}），跳过")
            detail[code] = {"zero_days": len(dates), "updated": 0,
                            "note": "双源为空"}
            continue
        if t_amt:
            amt.update(t_amt)
            src += "+收盘f48修正"
        # 仅更新 amount 列（OHLCV 不动——回填只补额，不碰行情）
        n = 0
        if not dry:
            # 逐行精确更新（amount 仅新值>0 或库内<=0 时覆盖——0 保护）
            cur = conn.cursor()
            # ① 当日收盘修正直写（即使不在 dates——覆盖同花顺盘中缓存值，
            #    如 sh000001 9/16 5528 亿 → 收盘 8711 亿；0 保护：>0 才覆盖）
            if t_amt:
                for d, v in t_amt.items():
                    if v > 0:
                        cur.execute(
                            "UPDATE kline SET amount=? WHERE code=? "
                            "AND period='day' AND date=? AND (?>0 OR amount<=0)",
                            (v, code, d, v))
                        n += cur.rowcount
            # ② 常规回填（amount=0 的交易日）
            for d in dates:
                a = amt.get(d)
                if a is None:
                    continue
                cur.execute(
                    "UPDATE kline SET amount=? WHERE code=? AND period='day' AND date=? "
                    "AND (?>0 OR amount<=0)", (a, code, d, a))
                n += cur.rowcount
            conn.commit()
        else:
            n = sum(1 for d in dates if amt.get(d)) + (len(t_amt) if t_amt else 0)
        updated += n
        detail[code] = {"zero_days": len(dates), "updated": n,
                        "dates": dates, "amount_found": sum(1 for d in dates if amt.get(d)),
                        "src": src}
        print(f"  {code}: 待回填 {len(dates)} 日（{src}），东财命中 "
              f"{sum(1 for d in dates if amt.get(d))} 日，更新 {n} 行")
    return updated, detail


def verify_10d(conn):
    """判据①：三指数最近 10 个交易日 amount>0 覆盖率。返回 (覆盖率, 明细)。"""
    out = {}
    for code in INDEX_CODES:
        rows = conn.execute(
            "SELECT date, amount FROM kline WHERE code=? AND period='day' "
            "ORDER BY date DESC LIMIT 10", (code,)).fetchall()
        pos = sum(1 for _d, a in rows if a and a > 0)
        out[code] = {"days": len(rows), "positive": pos,
                     "ratio": pos / len(rows) if rows else 0.0,
                     "zero_dates": [d for d, a in rows if not a or a <= 0]}
    return out


def cross_check(conn, samples=("2026-09-03", "2026-09-08", "2026-09-11")):
    """判据②：抽样历史日——库内已填值（A5 源）vs 东财 push2his 同日，误差 ≤1%。
    返回逐日误差列表。"""
    rows = []
    for code in INDEX_CODES:
        beg = _d2e(samples[0])
        end = _d2e(samples[-1])
        em = df.fetch_index_amount_em(code, beg, end)
        src = "东财"
        if not em:
            em = df.fetch_index_amount_ths(code, days=20)
            src = "同花顺(东财限流)"
        for d in samples:
            cur = conn.execute(
                "SELECT amount FROM kline WHERE code=? AND period='day' AND date=?",
                (code, d)).fetchone()
            dbv = cur[0] if cur else None
            emv = em.get(d)
            if dbv is None or emv is None:
                rows.append({"code": code, "date": d, "db": dbv, "em": emv,
                             "err_pct": None, "src": src})
                continue
            err = abs(dbv - emv) / emv * 100 if emv else None
            rows.append({"code": code, "date": d, "db": dbv, "em": emv,
                         "err_pct": round(err, 4) if err is not None else None,
                         "src": src})
    return rows


def main():
    import argparse
    ap = argparse.ArgumentParser(description="指数成交额回填（东财）")
    ap.add_argument("--days", type=int, default=0, help="只看最近 N 个交易日（默认全部 amount=0 日）")
    ap.add_argument("--dry", action="store_true", help="试跑不写库（出对拍与待回填清单）")
    args = ap.parse_args()

    conn = _db.open_rw(C.DB_FILE)
    try:
        print("=== K10 指数成交额回填（东财 push2his）===")
        print("dry-run:", args.dry, "| 窗口:", args.days or "全部")
        # 判据②对拍先行（不依赖回填）
        print("\n[对拍] 抽样历史日 库内 vs 东财（判据②，目标 ≤1%）")
        for r in cross_check(conn):
            err = "%.4f%%" % r["err_pct"] if r["err_pct"] is not None else "N/A"
            db_s = f"{r['db']:,.0f}" if r["db"] is not None else "None"
            em_s = f"{r['em']:,.0f}" if r["em"] is not None else "None"
            print(f"  {r['code']} {r['date']}: 库内={db_s} 东财={em_s} 误差={err}")
        # 回填
        print("\n[回填]")
        updated, detail = backfill(conn, dry=args.dry)
        print(f"  合计更新 {updated} 行")
        # 判据①
        print("\n[判据①] 最近 10 交易日 amount>0 覆盖率")
        v = verify_10d(conn)
        all_ok = True
        for code, d in v.items():
            ok = d["ratio"] >= 1.0
            all_ok &= ok
            print(f"  {code}: {d['positive']}/{d['days']} = {d['ratio']*100:.1f}% "
                  f"{'PASS' if ok else 'FAIL'} zero={d['zero_dates']}")
        print("\n判据①:", "PASS" if all_ok else "FAIL")
        print("判据②: 见上（逐日误差 ≤1% 即 PASS）")
        print("判据③: 本脚本全部走 UPDATE（amount 新值>0 才覆盖），未用 INSERT OR REPLACE")
        print("判据④: 见 tests/test_j4_call_smoke.py（新值为 0 不覆盖库内非 0）")
        # 审计（非 dry）
        if not args.dry:
            try:
                audit.record(kind="daily", event="index_amount_backfill",
                             level="INFO", updated=updated,
                             per_code={c: d.get("updated", 0) for c, d in detail.items()},
                             dry=False)
            except Exception:
                pass
        # 输出 JSON 结果文件
        res = {"updated": updated, "detail": detail, "verify_10d": v,
               "dry": args.dry, "cross_check": cross_check(conn)}
        out = os.path.join(ROOT, "tmp", "k10")
        os.makedirs(out, exist_ok=True)
        with open(os.path.join(out, "backfill_result.json"), "w",
                  encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=1, default=str)
        print(f"\n结果: {os.path.join(out, 'backfill_result.json')}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
