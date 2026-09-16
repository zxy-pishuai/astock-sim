# -*- coding: utf-8 -*-
"""A4 全球薄源回补工具 —— GC/USDCNH/DJIA 主备链回补 global_kline 并哨兵级复验

用法:
  python tools/global_thin_backfill.py --check        # 只读: 现状 + 首选源实测
  python tools/global_thin_backfill.py --apply        # 备份 -> update_global() -> 行数复验
源链定义与主备切换逻辑在 app/updater.py 全球段（Phase63/A4 独占区），本工具只驱动与验证。
"""
import argparse
import json
import os
import pathlib
import sqlite3
import sys
import time

BASE = pathlib.Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DB = os.path.join(BASE, "data", "market.db")
TARGETS = {"GC": 1000, "USDCNH": 1000, "DJIA": 1000}   # sym -> 最低行数验收线


def db_mtime():
    return os.path.getmtime(DB)


def snapshot_counts(con):
    out = {}
    for mkt, sym, n, dmin, dmax in con.execute(
            "SELECT market, sym, COUNT(*), MIN(date), MAX(date) FROM global_kline "
            "GROUP BY market, sym"):
        out[sym] = {"market": mkt, "rows": n, "min": dmin, "max": dmax}
    return out


def wait_quiet(window_sec=90.0, max_wait=600.0):
    t0 = time.time()
    m0 = db_mtime()
    while time.time() - t0 < max_wait:
        time.sleep(10)
        m1 = db_mtime()
        if m1 == m0 and time.time() - t0 >= window_sec:
            print(f"[guard] market.db 静默 {time.time()-t0:.0f}s，可写")
            return True
        if m1 != m0:
            print("[guard] 库在写入，重置静默计时…")
            m0 = m1
            t0 = time.time()
    print("[guard] 超时未静默")
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    from app import updater as U
    chains = {sym: [lbl for _, lbl in chain] for sym, _, chain in U._global_source_chains()}

    con = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=15)
    try:
        before = snapshot_counts(con)
    finally:
        con.close()
    print("=== 回补前 ===")
    for sym in sorted(set(list(TARGETS) + list(before))):
        b = before.get(sym)
        if b:
            mark = " <== 薄" if sym in TARGETS and b["rows"] < TARGETS[sym] else ""
            print(f"  {sym:<8} {b['market']:<4} {b['rows']:>6} 行  {b['min']}..{b['max']}{mark}")

    if a.check or not a.apply:
        print("\n--check 不写库。如需回补: --apply")
        return 0

    # ---- 守卫 + 备份 ----
    if not wait_quiet():
        return 3
    bak_dir = os.path.join(BASE, "data", "backups")
    os.makedirs(bak_dir, exist_ok=True)
    bak = os.path.join(bak_dir, "global_kline_thin_before_%s.json"
                       % time.strftime("%Y%m%d_%H%M%S"))
    con = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=15)
    try:
        dump = {}
        for sym in TARGETS:
            rows = con.execute(
                "SELECT market, date, close FROM global_kline WHERE sym=? ORDER BY date",
                (sym,)).fetchall()
            dump[sym] = rows
    finally:
        con.close()
    with open(bak, "w", encoding="utf-8", newline="\n") as f:
        json.dump(dump, f, ensure_ascii=False)
    print(f"[backup] 已导出目标序列现有行 -> {bak} "
          f"({{k: len(v) for k, v in dump.items()}})".replace(
              "{{k: len(v) for k, v in dump.items()}}",
              str({k: len(v) for k, v in dump.items()})))

    # ---- 执行回补（复用 updater 主备链）----
    print("\n=== update_global() ===")
    res = U.update_global(verbose=True)

    # ---- 复验 ----
    time.sleep(1)
    con = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=15)
    try:
        after = snapshot_counts(con)
    finally:
        con.close()
    ok_all = True
    summary = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
               "tool": "tools/global_thin_backfill.py",
               "chains": chains, "before": before,
               "update_results": res, "after": after, "targets": TARGETS,
               "verdict": {}}
    print("\n=== 回补后复验 ===")
    for sym, need in TARGETS.items():
        r = after.get(sym) or {}
        got = r.get("rows", 0)
        ok = got >= need
        ok_all &= ok
        src = (res.get(sym) or {}).get("source")
        print(f"  {sym:<8} {got:>6}/{need:<6} {'OK ' if ok else 'FAIL'} "
              f"{r.get('min','')}..{r.get('max','')}  src={src}")
        summary["verdict"][sym] = {"rows": got, "need": need, "ok": ok}
    outp = os.path.join(BASE, "data", "global_thin_backfill.json")
    with open(outp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"\n{'PASS' if ok_all else 'FAIL'} 摘要 -> {outp}")
    return 0 if ok_all else 2


if __name__ == "__main__":
    sys.exit(main())
