# -*- coding: utf-8 -*-
"""★ D3：退市股宇宙构建与回填（免费源管线固化版）

数据源（均实测可用，2026-08-27）：
  1) baostock query_stock_basic —— 全 A 宇宙 5,549 只（type=1），含 ipoDate/outDate
     与 status(1上市/0退市)。退市 337 只，是 PIT 资格线的权威清单；
  2) baostock query_history_k_data_plus —— 对已退市股仍提供摘牌前全史日K
     （2005 起拉取；停牌期空量占位行按 (o>0,c>0,v>0,amt>0) 剔除）；
  3) akshare stock_info_sh_delist / stock_info_sz_delist —— 交易所披露口径交叉
     验证（SH 表列为"暂停上市日期"，SZ 为"终止上市日期"；本轮仅作对照不落库）。
金额单位与库内 mootdx 行一致（600519 同日 ratio=1.0000×19 天实测校准）。

写入策略：只 upsert 库内尚不存在 code 的退市股行（幂等：重跑零增量），
落地清单 data/delisted_written_manifest.json 记录每票行数/日期范围；
回滚模板见报告。跳过无有效K线的退市股并记录原因。

用法:
  python tools/build_delisted_universe.py --status   # 打印宇宙/库内覆盖现状（默认）
  python tools/build_delisted_universe.py --refresh  # 重拉宇宙清单+补拉缺失K线+入库
输出: data/delisted_universe.json / data/delisted_written_manifest.json
"""
import json
import os
import sqlite3
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

DB = os.path.join(BASE, "data", "market.db")
UNI = os.path.join(BASE, "data", "delisted_universe.json")
CACHE = os.path.join(BASE, "data", "d3_delisted_bars_cache.json")
MANI = os.path.join(BASE, "data", "delisted_written_manifest.json")
FETCH_FROM = "2005-01-01"


def _bs_stocks():
    import baostock as bs
    lg = bs.login()
    assert lg.error_code == "0", lg
    try:
        rs = bs.query_stock_basic()
        rows = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())
    finally:
        bs.logout()
    uni = []
    for r in rows:
        if len(r) < 6 or r[4] != "1" or "." not in r[0]:
            continue
        exch, n6 = r[0].split(".")
        if len(n6) != 6 or not n6.isdigit():
            continue
        if exch == "sh" and not n6.startswith("6"):
            continue
        if exch == "sz" and n6[0] not in "03":
            continue
        uni.append({"code": n6, "name": r[1], "ipo": r[2],
                    "out": r[3], "status": int(r[5])})
    return uni


def _fetch_bars(code6):
    import baostock as bs
    bcode = ("sh." if code6.startswith("6") else "sz.") + code6
    rs = bs.query_history_k_data_plus(
        bcode, "date,open,high,low,close,volume,amount",
        start_date=FETCH_FROM, frequency="d", adjustflag="2")
    rows = []
    while rs.error_code == "0" and rs.next():
        rows.append(rs.get_row_data())
    out = []
    for d, o, h, l, c, v, amt in rows:
        try:
            o, h, l, c, v, amt = float(o), float(h), float(l), float(c), \
                float(v), float(amt)
        except ValueError:
            continue
        if o > 0 and c > 0 and v > 0 and amt > 0:
            out.append((d, o, h, l, c, v, amt))
    seen = {r[0]: r for r in out}
    return [seen[d] for d in sorted(seen)]


def status():
    conn = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    have_total = conn.execute(
        "SELECT COUNT(DISTINCT code) FROM kline WHERE period='day' "
        "AND length(code)=6").fetchone()[0]
    dead_codes = [u["code"] for u in json.load(open(UNI, encoding="utf-8"))
                  ["stocks"] if u["status"] == 0] \
        if os.path.exists(UNI) else []
    ph = ",".join("?" * max(len(dead_codes), 1))
    have_dead = conn.execute(
        "SELECT COUNT(DISTINCT code) FROM kline WHERE period='day' "
        "AND length(code)=6 AND code IN (%s)" % ph,
        tuple(dead_codes or [""])).fetchone()[0]
    conn.close()
    print("宇宙文件: %s" % ("存在（A股 %d 只，退市 %d）" % (
        json.load(open(UNI, encoding="utf-8"))["count"], len(dead_codes))
        if os.path.exists(UNI) else "缺失"))
    print("kline 现有 code 总数: %d；其中退市宇宙已入库: %d/%d" % (
        have_total, have_dead, len(dead_codes)))


def refresh():
    t0 = time.time()
    print("1) 拉 baostock 全宇宙 ...", flush=True)
    uni = _bs_stocks()
    with open(UNI, "w", encoding="utf-8") as f:
        json.dump({"fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "source": "baostock query_stock_basic(type=1)",
                   "count": len(uni), "stocks": uni}, f,
                  ensure_ascii=False, indent=1)
    dead = sorted([u for u in uni if u["status"] == 0 and u["out"]],
                  key=lambda x: x["out"])
    print("   A股 %d 只，其中退市 %d → delisted_universe.json" % (
        len(uni), len(dead)), flush=True)

    cache = {"fetched_at": "", "source": "baostock qfq(daily)",
             "stocks": {}}
    if os.path.exists(CACHE):
        try:
            cache = json.load(open(CACHE, encoding="utf-8"))
        except Exception:
            pass
    conn = sqlite3.connect(DB, timeout=90)
    exist = {r[0] for r in conn.execute(
        "SELECT DISTINCT code FROM kline WHERE period='day' "
        "AND length(code)=6")}
    todo = [u for u in dead if u["code"] not in cache.get("stocks", {})
            and u["code"] not in exist]
    print("2) 补拉K线：%d 只（缓存已有 %d，库内已有 %d）..." % (
        len(todo), len(cache.get("stocks", {})),
        sum(1 for u in dead if u["code"] in exist)), flush=True)
    import baostock as bs
    lg = bs.login()
    assert lg.error_code == "0"
    got = empty = fail = 0
    for i, u in enumerate(todo):
        try:
            bars = _fetch_bars(u["code"])
            cache.setdefault("stocks", {})[u["code"]] = {
                "bcode": ("sh." if u["code"].startswith("6") else "sz.")
                + u["code"],
                "name": u["name"], "ipo": u["ipo"], "out": u["out"],
                "bars": [[x for x in r] for r in bars]}
            got += 1 if bars else 0
            empty += 0 if bars else 1
        except Exception as e:
            fail += 1
            print("   FAIL %s: %s" % (u["code"], str(e)[:60]), flush=True)
        if (i + 1) % 50 == 0:
            print("   %d/%d ..." % (i + 1, len(todo)), flush=True)
    bs.logout()
    cache["fetched_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    json.dump(cache, open(CACHE, "w", encoding="utf-8"), ensure_ascii=False)

    print("3) 入库（仅 kline 尚缺的退市 code；INSERT OR REPLACE 幂等）...",
          flush=True)
    written, skipped = [], []
    already = 0
    for u in dead:
        st = cache.get("stocks", {}).get(u["code"])
        if u["code"] in exist:
            already += 1
            continue
        if not st or not st["bars"]:
            skipped.append({"code": u["code"], "name": u["name"],
                            "out": u["out"],
                            "reason": "%s 后无有效K线" % FETCH_FROM})
            continue
        rows = [(u["code"], "day") + tuple(r) for r in st["bars"]]
        conn.executemany(
            "INSERT OR REPLACE INTO kline(code,period,date,open,high,low,"
            "close,volume,amount) VALUES(?,?,?,?,?,?,?,?,?)", rows)
        written.append({"code": u["code"], "name": u["name"],
                        "ipo": u["ipo"], "out": u["out"],
                        "rows": len(rows), "date_min": rows[0][2],
                        "date_max": rows[-1][2]})
    conn.commit()

    n_rows = conn.execute(
        "SELECT COUNT(*) FROM kline WHERE period='day'").fetchone()[0]
    conn.close()
    mani = {"written_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "source": "baostock（本表清单元数据 + d3_delisted_bars_cache K线）",
            "rollback": "DELETE FROM kline WHERE code IN (本清单codes)",
            "already_in_db": already,
            "written_count": len(written),
            "rows_written": sum(w["rows"] for w in written),
            "skipped_count": len(skipped),
            "written": written, "skipped": skipped}
    json.dump(mani, open(MANI, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("\n完成 %.0fs：新入库 %d 只 / %d 行（已在库 %d 只）；"
          "无数据跳过 %d；全表现在 period='day' 共 %d 行" % (
              time.time() - t0, len(written), mani["rows_written"],
              already, len(skipped), n_rows))


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="D3 退市股宇宙工具")
    ap.add_argument("--refresh", action="store_true",
                    help="重拉清单、补拉缺失退市股K线并入库")
    a = ap.parse_args()
    if a.refresh:
        refresh()
    else:
        status()
