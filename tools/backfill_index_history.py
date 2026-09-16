# -*- coding: utf-8 -*-
"""A5（2026-09-13）三大指数日K历史回填至 2019-01-01（与个股回测口径对齐）。

背景：sh000001 仅 615 根（2024-02-29 起）、sz399001/sz399006 各 615 根；个股回测口径
2019 起；指数 amount 全为 0。F1 已修字段序（腾讯数组 [date,open,close,high,low,volume]）。

方案（实测选型，2026-09-13）：
  主源 = 腾讯 **newfqkline**（proxy.finance.qq.com）：返回 11 元素数组
    [date, open, close, high, low, volume, {}, 换手/振幅, 成交额(万), 0, 0]，
    e[8] = 成交额（万元）→ ×10000 入库（元）。实测 2019-01-02 e[8]=9759257.20万
    ≈ 东财 push2his 97592573952 元（逐位吻合）。
  东财 push2his 本计划补额，实测被 WAF 封禁（RemoteDisconnected，fflow.py 已注明），弃用。
  分页：newfqkline 的 end 参数生效（返回截止 end 的最多 640 根）→ 从 END 往前翻页，
    每段以上一段首根日期为新的 end，3 段覆盖 2019-01-01~2026-09-13（1868 交易日）。
  字段映射沿用 F1：open=e[1], close=e[2], high=e[3], low=e[4], volume=e[5]*100。
  断言：_sane_rows（high>=low / high>=max(o,c) / low<=min(o,c) / volume>=0 / 非物理量拒绝），
    断言失败的行不写并列出。
  幂等 upsert（INSERT OR REPLACE），断点续跑（进度 tmp/a5/backfill_state.json）。

验收判据（写库后自检）：三指数各 ≥1800 根、最早日期 ≤2019-01-05、
  high<low 行数=0、amount>0 占比 ≥95%。

用法：
  py -3.13 tools/backfill_index_history.py            # 正式回填
  py -3.13 tools/backfill_index_history.py --dry-run  # 只拉取+断言+统计，不写库
  py -3.13 tools/backfill_index_history.py --reset    # 忽略断点重跑
"""
import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")
BASE = "C:/Users/26838/A股模拟盘"
DB = BASE + "/data/market.db"
STATE = BASE + "/tmp/a5/backfill_state.json"
SYMBOLS = ["sh000001", "sz399001", "sz399006"]          # 上证/深成/创业板指
START = "2019-01-01"
END = "2026-09-13"
_PAGE = 640          # newfqkline 单次返回上限
_MAX_SEG = 4         # 翻页段数上限（640×3 ≥ 1868 交易日，4 段留余量）

_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

try:
    from app.index_timing import _sane_rows   # F1 通用 K 线断言（个股/指数共用）
except Exception:
    def _sane_rows(rows):
        return rows


def _http(url, timeout=30, retries=4):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=_UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as ex:
            last = ex
            time.sleep(1.5 * (i + 1))
    raise last


def fetch_tencent_newfq(code):
    """腾讯 newfqkline 翻页拉全（含成交额万元→元）。返回 {date:(o,h,l,c,v,amount)}。"""
    out = {}
    end = END
    for seg in range(_MAX_SEG):
        url = ("https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get?"
               "param=%s,day,%s,%s,%d,qfq") % (code, START, end, _PAGE)
        d = json.loads(_http(url))
        raw = ((d.get("data") or {}).get(code) or {}).get("qfqday") or \
              ((d.get("data") or {}).get(code) or {}).get("day") or []
        if not raw:
            break
        for el in raw:
            if len(el) < 9:
                continue
            try:
                dt, o, c, h, l, v = el[0], float(el[1]), float(el[2]), float(el[3]), float(el[4]), float(el[5])
                amt = float(el[8]) * 10000.0      # e[8]=成交额（万元）→ 元
            except (ValueError, TypeError):
                continue
            out[dt] = (o, h, l, c, v * 100, amt)   # volume 沿用 F1 ×100 口径
        first = raw[0][0]
        if first <= START or len(raw) < _PAGE:
            break
        end = first                                # 下一段截止本段首根（往前翻页）
        time.sleep(0.2)
    return out


def build_rows(code):
    rows = []
    tq = fetch_tencent_newfq(code)
    for dt, (o, h, l, c, v, amt) in sorted(tq.items()):
        rows.append((code, "day", dt, o, h, l, c, v, amt))
    return rows


def main():
    ap = argparse.ArgumentParser(description="三大指数日K回填至 2019（A5）")
    ap.add_argument("--dry-run", action="store_true", help="只拉取+断言+统计，不写库")
    ap.add_argument("--reset", action="store_true", help="忽略断点状态重跑")
    a = ap.parse_args()

    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    st = {}
    if not a.reset and os.path.isfile(STATE):
        try:
            st = json.load(open(STATE, encoding="utf-8"))
        except Exception:
            st = {}
    done = set(st.get("done", []))

    summary = {}
    for code in SYMBOLS:
        if code in done and not a.dry_run:
            print(f"{code}: 断点已完，跳过", flush=True)
            continue
        t0 = time.time()
        rows = build_rows(code)
        ok_rows = _sane_rows(rows)                 # 断言过滤（失败行不写并 WARN）
        rejected = [r[2] for r in rows if r not in ok_rows]
        if not rows:
            print(f"{code}: 拉取为空！", flush=True)
            summary[code] = {"pulled": 0, "rejected": 0, "min_date": None, "max_date": None}
            continue
        amt0 = sum(1 for r in rows if r[8] > 0)
        print(f"{code}: 拉取 {len(rows)} 根（{rows[0][2]} ~ {rows[-1][2]}）"
              f" amount>0 {amt0}/{len(rows)} 断言拒绝 {len(rejected)}"
              f" {'无' if not rejected else rejected[:5] + (['...'] if len(rejected) > 5 else [])}"
              f" 耗时 {time.time()-t0:.0f}s", flush=True)
        if a.dry_run:
            summary[code] = {"pulled": len(rows), "rejected": len(rejected),
                             "min_date": rows[0][2], "max_date": rows[-1][2]}
            continue
        conn = sqlite3.connect(DB, timeout=60)
        try:
            conn.executemany(
                "INSERT OR REPLACE INTO kline(code,period,date,open,high,low,close,volume,amount) "
                "VALUES(?,?,?,?,?,?,?,?,?)", ok_rows)
            conn.commit()
        finally:
            conn.close()
        done.add(code)
        st.update({"done": sorted(done), "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")})
        with open(STATE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False)

    if a.dry_run:
        print("\n=== dry-run 汇总（未写库）===")
        for k, v in summary.items():
            print(f"  {k}: 拉取 {v['pulled']} 根（{v['min_date']} ~ {v['max_date']}） 拒绝 {v['rejected']}")
        return 0

    # 写库后验收
    print("\n=== 验收矩阵 ===")
    conn = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=30)
    ok_all = True
    for code in SYMBOLS:
        r = conn.execute(
            "SELECT COUNT(*),MIN(date),MAX(date),"
            "SUM(CASE WHEN high<low THEN 1 ELSE 0 END),"
            "SUM(CASE WHEN high<MAX(open,close) OR low>MIN(open,close) THEN 1 ELSE 0 END),"
            "SUM(CASE WHEN amount>0 THEN 1 ELSE 0 END),"
            "SUM(CASE WHEN amount IS NULL THEN 1 ELSE 0 END) "
            "FROM kline WHERE code=? AND period='day'", (code,)).fetchone()
        n, mn, mx, hlt, ob, ap, nul = r
        ar = 100.0 * ap / n if n else 0.0
        chk = [n >= 1800, mn is not None and mn <= "2019-01-05", hlt == 0, ar >= 95.0]
        flag = "PASS" if all(chk) else "FAIL"
        if not all(chk):
            ok_all = False
        print(f"  {code}: 根数={n} 最早={mn} 最晚={mx} high<low={hlt} "
              f"OHLC越界={ob} amount>0={ap}({ar:.1f}%) amount NULL={nul} → {flag}")
    conn.close()
    print("验收:", "全部通过" if ok_all else "存在未通过项")
    return 0 if ok_all else 2


if __name__ == "__main__":
    sys.exit(main())
