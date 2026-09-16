# -*- coding: utf-8 -*-
"""★ F1（2026-09-08）一次性修复：指数日K 三值轮换（high/low/close 错位）。
腾讯 fqkline 数组序 [date,open,close,high,low,volume] 被旧代码按
(open,high,low,close) 位置解析落库 → 指数行 high=真实收盘、low=真实最高、
close=真实最低。本脚本对三个指数全部错位行做：
    new_high = old_low ; new_low = old_close ; new_close = old_high
open/volume/amount/date 不动。
幂等：仅处理"错位或越界"行（high<low 或 high<max(open,close) 或
low>min(open,close)）；修复后正确行三者皆不满足，重复执行无动作。
全程单事务；先 SELECT 打印待修行数，内存算好新值，再按 rowid 批量 UPDATE。
用法：python -m tools.repair_index_kline_fields
"""
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8")

BASE = "C:/Users/26838/A股模拟盘"
DB = BASE + "/data/market.db"
CODES = ("sh000001", "sz399001", "sz399006")


def main():
    conn = sqlite3.connect(DB, timeout=60)
    # 1) 先打印待修统计（含高/低关系分布）
    for code in CODES:
        r = conn.execute(
            "SELECT COUNT(*), SUM(high<low), SUM(high>=low) "
            "FROM kline WHERE period='day' AND code=?", (code,)).fetchone()
        print(f"[待修统计] {code}: 总 {r[0]} 行 | high<low(错位) {r[1]} | high>=low {r[2]}")
    # 2) 取全部错位/越界行，内存算好新值
    #    错位特征：腾讯 6 字段按 7 字段解析后
    #      high=src_close、low=src_high、close=src_low。
    #    src 正常时 high<low；src 当日 high==close（收盘=最高）时 high==low，
    #    需用越界条件（low>min(open,close)）兜住。正确行三者皆不满足 → 幂等。
    bad = conn.execute(
        "SELECT rowid, date, open, high, low, close FROM kline "
        "WHERE period='day' AND code IN ('sh000001','sz399001','sz399006') "
        "AND (high<low OR high<max(open,close) OR low>min(open,close))"
    ).fetchall()
    print(f"[待修] 共 {len(bad)} 行（high<low）")
    if not bad:
        print("无需修复，退出。")
        conn.close()
        return
    new_rows = []
    for rowid, d, o, h, l, c in bad:
        # 三值轮换：new_high=old_low, new_low=old_close, new_close=old_high
        new_rows.append((l, c, h, rowid))
    # 3) 单事务批量 UPDATE
    cur = conn.cursor()
    try:
        cur.executemany(
            "UPDATE kline SET high=?, low=?, close=? WHERE rowid=?", new_rows)
        conn.commit()
        print(f"[写入] 已轮换修复 {len(new_rows)} 行")
    except Exception as e:
        conn.rollback()
        print(f"[失败] 事务回滚: {e!r}")
        conn.close()
        sys.exit(1)
    # 4) 立即校验
    for code in CODES:
        r = conn.execute(
            "SELECT COUNT(*), SUM(high<low), "
            "SUM(CASE WHEN high<max(open,close) OR low>min(open,close) THEN 1 ELSE 0 END) "
            "FROM kline WHERE period='day' AND code=?", (code,)).fetchone()
        print(f"[校验] {code}: 总 {r[0]} | high<low {r[1]} | 越界 {r[2]}")
    conn.close()


if __name__ == "__main__":
    main()
