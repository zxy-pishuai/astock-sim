# -*- coding: utf-8 -*-
"""P71-A2 比亚迪 002594 前复权接缝修复驱动（复用 tools/repair_qfq.py 流程）

跳变: 2025-07-16 → 2025-07-17 open +207.1%（前复权污染，P54/P71 列入排除清单）
流程（docs/reports/data_repair.md 任务1 先例）:
  0) mtime 守卫: 等待 market.db 静默窗口（90s 无变化才动库）
  1) 备份先行: kline(day) 全部行 → data/backups/kline_day_002594_before_byd_repair_<ts>.json
  2) mootdx 原始日K + get_xdxr → make_qfq 重建前复权（纯内存，不写库即可先校验）
  3) 校验: 2019 起无 >21% 接缝 && 最新 close 与库内一致 && 接缝处修复前后对照
  4) 单事务写库（WAL，INSERT OR REPLACE 只动 002594 day 行），修 qfq_repair_progress 过滤陷阱:
     --reset-progress 先从 progress 移除本票键
用法:
  python tools/repair_002594.py --check   # 只读校验+对照，不写库
  python tools/repair_002594.py --apply   # 备份→写库→复检
"""
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
CODE = "002594"
JUMP_PREV, JUMP_DATE = "2025-07-16", "2025-07-17"
BACKUP_DIR = os.path.join(BASE, "data", "backups")
PROGRESS = os.path.join(BASE, "data", "qfq_repair_progress.json")


def db_mtime():
    return os.path.getmtime(DB)


def wait_quiet(window_sec=90.0, max_wait=900.0):
    """mtime 守卫：等待 market.db 静默窗口再动库"""
    t0 = time.time()
    m0 = db_mtime()
    while time.time() - t0 < max_wait:
        time.sleep(10)
        m1 = db_mtime()
        if m1 == m0 and time.time() - t0 >= window_sec:
            print(f"[guard] 静默确认 {time.time()-t0:.0f}s (mtime {m0})")
            return True
        if m1 != m0:
            print(f"[guard] 库仍在写入(mtime 变更)，重置静默计时…")
            m0 = m1
            t0 = time.time()
    print("[guard] 超时未静默，放弃")
    return False


def fetch_db_rows(con, code=CODE):
    return con.execute(
        "SELECT date, open, high, low, close, volume, amount FROM kline "
        "WHERE code=? AND period='day' ORDER BY date", (code,)).fetchall()


def show_seam(rows, tag):
    print(f"--- 接缝对照 [{tag}] {JUMP_PREV} -> {JUMP_DATE} ---")
    for d0, d1 in [(JUMP_PREV, JUMP_DATE)]:
        r0 = next((r for r in rows if r[0] == d0), None)
        r1 = next((r for r in rows if r[0] == d1), None)
        for label, r in [("prev", r0), ("jump", r1)]:
            if r:
                print(f"  {label} {r[0]} O={r[1]:.4f} H={r[2]:.4f} L={r[3]:.4f} "
                      f"C={r[4]:.4f} V={r[5]/100:.0f}")
        if r0 and r1 and r0[4]:
            print(f"  open/prevC-1 = {(r1[1]/r0[4]-1)*100:+.2f}%")


def main(apply=False):
    # 依赖：mootdx 在系统 Python —— 本脚本用系统 Python 跑即有
    sys.path.insert(0, os.path.join(BASE, "tools"))
    from repair_qfq import fetch_raw, make_qfq          # 复用 P35 先例实现
    from mootdx.utils.adjust import get_xdxr

    print(f"[byd] DB mtime before: {db_mtime()}")
    ro = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=15)
    try:
        old_rows = fetch_db_rows(ro)
    finally:
        ro.close()
    print(f"[byd] 库内 day 行数: {len(old_rows)}")
    show_seam(old_rows, "修复前")
    last_old = old_rows[-1]
    print(f"[byd] 修复前最新行: {last_old[0]} C={last_old[4]}")

    # --- 拉源 ---
    raw = fetch_raw(CODE)
    if len(raw) < 10:
        print("[byd] FAIL: mootdx 无数据", len(raw)); return 1
    xdxr = get_xdxr(CODE)
    if xdxr is None or len(xdxr) == 0:
        print("[byd] FAIL: 无 xdxr 记录"); return 1
    adj = make_qfq(raw, xdxr)
    print(f"[byd] mootdx raw {len(raw)} 根 / xdxr {len(xdxr)} 条 -> qfq {len(adj)} 根")
    # fetch_raw 返回元组 (date, open, close, high, low, vol, amount)
    last_src_date = max(r[0] for r in raw)
    last_src_close_raw = [r for r in raw if r[0] == last_src_date][0][2]
    print(f"[byd] 源最新原始行: {last_src_date} C(不复权)={last_src_close_raw}")

    # --- 校验1: 2019 起连续性 ---
    gaps, prev, n = [], None, 0
    for r in adj:
        if r["date"] < "2019-01-01":
            prev = r; continue
        n += 1
        if n > 5 and prev and prev["close"] > 0:
            j = abs((r["open"] - prev["close"]) / prev["close"])
            if j > 0.21:
                gaps.append((prev["date"], r["date"], round(j*100, 2)))
        prev = r
    print(f"[byd] 2019起 >21% 残余接缝: {len(gaps)} {gaps[:5]}")

    # --- 校验2: 最新收盘对齐 ---
    new_map = {r["date"]: r for r in adj}
    latest_key = new_map.get(last_old[0]) or new_map.get(last_src["date"])
    ok_latest = latest_key and abs(latest_key["close"] - last_old[4]) <= max(0.02, last_old[4]*0.005)
    print(f"[byd] 最新收盘对齐: rebuilt {latest_key['date'] if latest_key else '?'} "
          f"C={latest_key['close'] if latest_key else '?'} vs db {last_old[0]} C={last_old[4]} -> {'OK' if ok_latest else 'FAIL'}")

    # --- 校验3: 接缝消除对照 ---
    seam_new = [new_map.get(d) for d in (JUMP_PREV, JUMP_DATE)]
    if all(seam_new):
        j_new = abs((seam_new[1]["open"] - seam_new[0]["close"]) / seam_new[0]["close"])
        print(f"[byd] 重建后接缝跳变: {j_new*100:+.2f}% (修前 +207.10%) -> {'OK' if j_new <= 0.11 else 'FAIL'}")
    if not apply:
        verdict = "PASS(可写库)" if (not gaps and ok_latest) else "FAIL(不写库)"
        print(f"[byd] CHECK 结论: {verdict}")
        return 0 if not gaps else 2

    # --- apply: 守卫→备份→单事务写库---
    if gaps or not ok_latest:
        print("[byd] APPLY 中止: 校验未全过，不写库（同 repair_qfq v4.1 保护）")
        return 2
    if not wait_quiet():
        return 3
    bak = os.path.join(BACKUP_DIR,
                       f"kline_day_{CODE}_before_byd_repair_{time.strftime('%Y%m%d_%H%M%S')}.json")
    os.makedirs(BACKUP_DIR, exist_ok=True)
    with open(bak, "w", encoding="utf-8", newline="\n") as f:
        json.dump({"code": CODE, "rows": len(old_rows),
                   "rows_data": old_rows}, f, ensure_ascii=False)
    print(f"[byd] 备份完成: {bak}")

    amap = {r[0]: r[6] for r in old_rows}
    w = sqlite3.connect(DB, timeout=60)
    try:
        w.execute("PRAGMA journal_mode=WAL")
        w.execute("BEGIN")
        w.executemany(
            "INSERT OR REPLACE INTO kline(code,period,date,open,high,low,close,volume,amount) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            [(CODE, "day", r["date"], r["open"], r["high"], r["low"], r["close"],
              r["vol"] * 100,
              (amap.get(r["date"]) if (amap.get(r["date"]) or 0) > 0
               else r["vol"] * 100 * (r["open"] + r["close"] + r["high"] + r["low"]) / 4))
             for r in adj])
        w.commit()
        print("[byd] 单事务写库完成:", len(adj), "行")
    except Exception:
        w.rollback()
        raise
    finally:
        w.close()

    # --- 写库后复检 ---
    ro2 = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=15)
    try:
        new_rows = fetch_db_rows(ro2)
    finally:
        ro2.close()
    show_seam(new_rows, "修复后")
    still = 0
    prev = None; n = 0
    for r in new_rows:
        if r[0] < "2019-01-01":
            prev = r; continue
        n += 1
        if n > 5 and prev and prev[4] > 0:
            if abs((r[1] - prev[4]) / prev[4]) > 0.21:
                still += 1
        prev = r
    print(f"[byd] 复检: high口径残余接缝(>21%) = {still} (目标归零)")
    # progress 陷阱治理: 记 done 以免 repair_qfq 未来批量时跳过/冲突语义混乱——此处不写入,
    # 因为 repair_qfq 仅按 defect 清单工作; 由验收方决定是否入 progress。
    print(f"[byd] DB mtime after: {db_mtime()}")
    print(f"[byd] 下一步: 哨兵复检 + 出《建议移出 DATA_EXCLUDE_CODES》报告 docs/reports/byd_repair_002594.md")
    return 0 if still == 0 else 4


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "--check"
    rc = main(apply=(mode == "--apply"))
    sys.exit(rc)
