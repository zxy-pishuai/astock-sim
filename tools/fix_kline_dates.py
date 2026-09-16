# -*- coding: utf-8 -*-
"""★ P0-1 一次性清理：修复 kline 表 period='day' 的日期格式分裂 + 清除非日期垃圾行
问题1：旧版 tdx._bars_one 对日K也走了"'YYYY-MM-DD HH:MM' → 补秒"逻辑，
把日K落库为 "YYYY-MM-DD HH:MM:00"，与腾讯日K的 "YYYY-MM-DD" 混存
→ 同一交易日出现两条、新鲜度判定/回测重复统计被污染。
问题2：mootdx 个别行 datetime 为 NaN → str() 得 "nan" 被原样落库
（OHLCV 全 NULL 的占位垃圾行）；'nan' 字典序大于 'YYYY-MM-DD'，
MAX(date) 被 'nan' 顶到最大 → 新鲜度判定恒真，这些股票永不触发补库。

本脚本：
  1. 备份 data/market.db → data/market.db.bak
  2. 规范化 period='day' 且 length(date)>10 的行：date=substr(date,1,10)
  3. 对规范化后同 (code,period,date) 的重复组去重（保留 rowid 较小一条）
  4. 清除非 'YYYY-MM-DD' 的垃圾行（date='nan'/''/NULL 等，整体删除）
  5. 输出清理统计 + 验证 SQL

用法：python -m tools.fix_kline_dates
"""
import os
import shutil
import sqlite3
import time

from app import config as C

DB = C.DB_FILE
BAK = DB + ".bak"


def main():
    if not os.path.exists(DB):
        print(f"数据库不存在: {DB}")
        return
    # 1) 备份
    if os.path.exists(BAK):
        ts = time.strftime("%Y%m%d_%H%M%S")
        BAK2 = f"{DB}.bak.{ts}"
        print(f"已存在 {BAK}，另存为 {BAK2}")
        shutil.copy2(DB, BAK2)
    else:
        shutil.copy2(DB, BAK)
        print(f"已备份: {BAK}")

    conn = sqlite3.connect(DB, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    cur = conn.cursor()

    # 2) 统计待修复行
    bad = cur.execute(
        "SELECT COUNT(*) FROM kline WHERE period='day' AND length(date)>10"
    ).fetchone()[0]
    print(f"[统计] period='day' 且 length(date)>10 的行数: {bad}")

    # ★ P0-1b：非 'YYYY-MM-DD' 垃圾行（'nan'/'NaN'/''/NULL 等）—— 不满足 length>10，
    #   旧修复逻辑覆盖不到，单独统计与清理
    JUNK_COND = ("(date IS NULL OR date NOT GLOB "
                 "'[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]')")
    junk = cur.execute(
        "SELECT COUNT(*) FROM kline WHERE period='day' AND " + JUNK_COND
    ).fetchone()[0]
    print(f"[统计] period='day' 非日期垃圾行(如 nan/空): {junk}")

    if bad == 0 and junk == 0:
        conn.close()
        print("无需清理")
        return

    # 3) 先删重复组（规范化后同 code+period+date 只能留一条，保留 rowid 较小者）
    #    注意：表 PK 是 (code,period,date)，date 截断后可能与已在库的 10 位行撞 PK，
    #    必须先删掉会被撞的旧行，再 UPDATE，否则 UNIQUE 冲突
    rows = cur.execute(
        "SELECT rowid, code, substr(date,1,10) AS d10 FROM kline "
        "WHERE period='day' AND length(date)>10"
    ).fetchall()
    # 找出与 10 位已存在行撞 PK 的，以及自身组内重复的
    dup_del = 0
    from collections import defaultdict
    groups = defaultdict(list)
    for rid, code, d10 in rows:
        groups[(code, "day", d10)].append(rid)
    # 与库内已存在的 10 位行冲突检查
    for (code, _p, d10), rids in groups.items():
        exists = cur.execute(
            "SELECT 1 FROM kline WHERE code=? AND period='day' AND date=? LIMIT 1",
            (code, d10)).fetchone()
        if exists:
            # 该组全部删除（10 位那行保留）
            for rid in rids:
                cur.execute("DELETE FROM kline WHERE rowid=?", (rid,))
                dup_del += 1
        elif len(rids) > 1:
            # 组内多行：保留 rowid 最小者
            keep = min(rids)
            for rid in rids:
                if rid != keep:
                    cur.execute("DELETE FROM kline WHERE rowid=?", (rid,))
                    dup_del += 1
    print(f"[清理] 去重重叠行: {dup_del}")

    # 4) 规范化剩余坏行
    upd = cur.execute(
        "UPDATE kline SET date=substr(date,1,10) "
        "WHERE period='day' AND length(date)>10"
    ).rowcount
    print(f"[清理] 规范化日期: {upd}")

    # ★ P0-1b：清除非日期垃圾行（含 'nan' 等，整体删除；此类行为占位空数据无保留价值）
    if junk:
        cur.execute("DELETE FROM kline WHERE period='day' AND " + JUNK_COND)
    print(f"[清理] 清除非日期垃圾行: {junk}")

    conn.commit()

    # 5) 验证
    v1 = cur.execute(
        "SELECT COUNT(*) FROM kline WHERE period='day' AND length(date)>10"
    ).fetchone()[0]
    v2 = cur.execute(
        "SELECT COUNT(*) FROM ("
        "  SELECT code, date, COUNT(*) c FROM kline WHERE period='day' "
        "  GROUP BY code, date HAVING c>1)"
    ).fetchone()[0]
    v3 = cur.execute(
        "SELECT COUNT(*) FROM kline WHERE period='day' AND " + JUNK_COND
    ).fetchone()[0]
    print(f"[验证] 残留 length(date)>10: {v1} (应为0)")
    print(f"[验证] 同(code,date)重复组: {v2} (应为0)")
    print(f"[验证] 残留非日期垃圾行: {v3} (应为0)")
    print(f"[验证] 清理后 MAX(date): "
          + str(cur.execute(
              "SELECT MAX(date) FROM kline WHERE period='day'").fetchone()[0])
          + " 行数: "
          + str(cur.execute(
              "SELECT COUNT(*) FROM kline WHERE period='day'").fetchone()[0]))

    conn.close()
    print("清理完成")


if __name__ == "__main__":
    main()